"""Real SQLAlchemy-session regressions for concurrent metadata field updates."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.retrieval.vector_retriever import VectorRetriever
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _SqlAlchemyStorage:
    def __init__(self, engine):
        self.engine = engine
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.before_write = None

    async def get_documents(self, metadata_filters, ids=None, limit=1):
        async with self.sessions() as session:
            result = await session.execute(text("SELECT id,metadata FROM documents WHERE id=:id"), {"id": ids[0]})
            row = result.first()
        return [] if row is None else [{"id": row.id, "metadata": json.loads(row.metadata)}]

    @asynccontextmanager
    async def get_session(self):
        if self.before_write is not None:
            await self.before_write()
        async with self.sessions() as session:
            yield session

    async def metadata(self):
        async with self.sessions() as session:
            result = await session.execute(text("SELECT metadata FROM documents WHERE id=1"))
            row = result.first()
        return json.loads(row[0]) if row else None


@pytest_asyncio.fixture
async def storage(tmp_path):
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(tmp_path / "metadata.db")))
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE documents(id INTEGER PRIMARY KEY,metadata TEXT)"))
        await connection.execute(
            text("INSERT INTO documents VALUES(1,:metadata)"),
            {"metadata": json.dumps({"status": "active", "access_count": 1, "session_id": "old", "importance": .2})},
        )
    yield _SqlAlchemyStorage(engine)
    await engine.dispose()


def _retriever(storage):
    return VectorRetriever(SimpleNamespace(document_storage=storage))


@pytest.mark.asyncio
async def test_field_edit_preserves_concurrent_access_archive_and_scope_update(storage):
    async def concurrent_writer():
        async with storage.engine.begin() as connection:
            await connection.execute(text(
                "UPDATE documents SET metadata=json_set(metadata,'$.access_count',9,"
                "'$.status','archived','$.session_id','moved') WHERE id=1"
            ))

    storage.before_write = concurrent_writer
    assert await _retriever(storage).update_metadata(1, {"importance": .8})
    assert await storage.metadata() == {
        "importance": .8, "access_count": 9, "status": "archived", "session_id": "moved",
    }


@pytest.mark.asyncio
async def test_two_simultaneous_disjoint_field_edits_both_survive(storage):
    barrier = asyncio.Barrier(2)
    storage.before_write = barrier.wait
    first, second = await asyncio.gather(
        _retriever(storage).update_metadata(1, {"first_field": "first"}),
        _retriever(storage).update_metadata(1, {"second_field": "second"}),
    )
    assert first and second
    metadata = await storage.metadata()
    assert metadata["first_field"] == "first"
    assert metadata["second_field"] == "second"


@pytest.mark.asyncio
async def test_explicit_null_and_json_value_types_are_preserved(storage):
    patch = {
        "session_id": None, "new_null": None, "enabled": True, "disabled": False,
        "nested": {"nullable": None, "items": ["text", 2, False]},
        "literal.dot": "literal value", "number": .25,
    }
    assert await _retriever(storage).update_metadata(1, patch)
    metadata = await storage.metadata()
    assert all(key in metadata and metadata[key] == value for key, value in patch.items())
    assert metadata["status"] == "active" and metadata["access_count"] == 1


@pytest.mark.asyncio
async def test_parent_deleted_at_write_boundary_reports_failure(storage):
    async def concurrent_delete():
        async with storage.engine.begin() as connection:
            await connection.execute(text("DELETE FROM documents WHERE id=1"))

    storage.before_write = concurrent_delete
    assert not await _retriever(storage).update_metadata(1, {"importance": .8})
    assert await storage.metadata() is None


@pytest.mark.asyncio
async def test_large_patch_keeps_all_fields_and_nulls(storage):
    patch = {f"field_{i}": None if i % 3 == 0 else {"value": i} for i in range(130)}
    assert await _retriever(storage).update_metadata(1, patch)
    metadata = await storage.metadata()
    assert all(key in metadata and metadata[key] == value for key, value in patch.items())


@pytest.mark.asyncio
async def test_failed_large_patch_rolls_back_all_prior_field_updates(storage):
    before = await storage.metadata()
    async with storage.engine.begin() as connection:
        await connection.execute(text(
            "CREATE TRIGGER reject_patch BEFORE UPDATE ON documents "
            "WHEN json_type(NEW.metadata,'$.field_75') IS NOT NULL "
            "BEGIN SELECT RAISE(ABORT,'injected update failure'); END"
        ))
    assert not await _retriever(storage).update_metadata(1, {f"field_{i}": i for i in range(130)})
    assert await storage.metadata() == before


@pytest.mark.asyncio
async def test_empty_patch_checks_document_existence(storage):
    before = await storage.metadata()
    assert await _retriever(storage).update_metadata(1, {})
    assert await storage.metadata() == before
    assert not await _retriever(storage).update_metadata(2, {})
