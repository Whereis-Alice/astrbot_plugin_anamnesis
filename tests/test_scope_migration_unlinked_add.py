"""Migration must not split documents from an add's later derived records."""

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from astrbot_plugin_anamnesis.core.managers.scope_migrator import ScopeMigrator
from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridRetriever

from tests.test_memory_transfer_runtime_safety import _engine

SOURCE = "qq:GroupMessage:1"
TARGET = "qq:GroupMessage:2"
OTHER = "qq:GroupMessage:3"
CONTENT = "coffee live fact"


@asynccontextmanager
async def paused_add(
    engine, monkeypatch, *, source=SOURCE, persona=None, metadata=None
):
    indexed, release = asyncio.Event(), asyncio.Event()
    add = engine.hybrid_retriever.add_memory

    async def pause_after_document(*args, **kwargs):
        memory_id = await add(*args, **kwargs)
        indexed.set()
        await release.wait()
        return memory_id

    monkeypatch.setattr(engine.hybrid_retriever, "add_memory", pause_after_document)
    task = asyncio.create_task(
        engine.add_memory(
            CONTENT,
            session_id=source,
            persona_id=persona,
            metadata=metadata,
            atoms=[
                MemoryAtom(
                    parent_memory_id=0, content=CONTENT, expires_at=time.time() + 86400
                )
            ],
        )
    )
    try:
        await asyncio.wait_for(indexed.wait(), 5)
        async with engine.db_connection.execute(
            "SELECT memory_id,status,step FROM memory_write_ops WHERE status='pending' AND op_type='add'"
        ) as cursor:
            rows = await cursor.fetchall()
        assert any(tuple(row) == (None, "pending", "started") for row in rows)
        yield task, release
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        monkeypatch.setattr(engine.hybrid_retriever, "add_memory", add)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["apply", "backup"])
async def test_migration_rejects_unlinked_add_then_accepts_completed_write(
    tmp_path, monkeypatch, operation
):
    engine = await _engine(tmp_path / "inflight-add.db")
    engine.atom_enabled = True
    try:
        async with paused_add(engine, monkeypatch) as (adding, release):
            migrator = ScopeMigrator(engine, tmp_path / "backups")
            preview = await migrator.preview(SOURCE, TARGET)
            assert preview["document_count"] == 1 and preview["atom_count"] == 0
            with pytest.raises(ValueError, match="memory write"):
                await asyncio.wait_for(
                    getattr(migrator, operation)(
                        SOURCE, TARGET, preview["fingerprint"]
                    ),
                    5,
                )
            assert not migrator.backup_dir.exists()
            release.set()
            memory_id = await asyncio.wait_for(adding, 5)
        with pytest.raises(ValueError, match="stale"):
            await getattr(migrator, operation)(SOURCE, TARGET, preview["fingerprint"])
        fresh = await migrator.preview(SOURCE, TARGET)
        moved = await getattr(migrator, operation)(SOURCE, TARGET, fresh["fingerprint"])
        assert moved["applied"] is (operation == "apply")
        assert len(moved["backup_paths"]) == 1
        document = await engine.get_memory(memory_id)
        atoms = await engine.atom_store.get_by_parent(memory_id)
        expected = TARGET if operation == "apply" else SOURCE
        assert document["metadata"]["session_id"] == atoms[0].session_id == expected
        assert await engine.resolve_live_facts(
            memory_id, document["text"], document["metadata"]
        ) == [CONTENT]
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unrelated", ["scope", "persona", "source_anchor"])
async def test_unlinked_add_outside_selected_migration_does_not_block(
    tmp_path, monkeypatch, unrelated
):
    engine = await _engine(tmp_path / "unrelated-add.db")
    engine.atom_enabled = True
    source = "123" if unrelated == "source_anchor" else SOURCE
    selection = "selected" if unrelated == "persona" else None
    metadata = {"source_session_id": TARGET} if unrelated == "source_anchor" else None
    try:
        selected_id = await engine.add_memory(
            CONTENT, session_id=source, persona_id=selection, metadata=metadata
        )
        outside_source = OTHER if unrelated == "scope" else source
        outside_persona = "other" if unrelated == "persona" else None
        outside_metadata = (
            {"source_session_id": OTHER} if unrelated == "source_anchor" else None
        )
        async with paused_add(
            engine,
            monkeypatch,
            source=outside_source,
            persona=outside_persona,
            metadata=outside_metadata,
        ) as (adding, release):
            migrator = ScopeMigrator(
                engine,
                tmp_path / "backups",
                source_anchor=TARGET if unrelated == "source_anchor" else None,
            )
            preview = await migrator.preview(source, TARGET, selection)
            moved = await asyncio.wait_for(
                migrator.apply(source, TARGET, preview["fingerprint"], selection), 5
            )
            assert moved["applied"]
            release.set()
            outside_id = await asyncio.wait_for(adding, 5)
        assert (await engine.get_memory(selected_id))["metadata"][
            "session_id"
        ] == TARGET
        assert (await engine.get_memory(outside_id))["metadata"][
            "session_id"
        ] == outside_source
        assert (await engine.atom_store.get_by_parent(outside_id))[
            0
        ].session_id == outside_source
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_migration_receipt_guard_keeps_ordinary_adds_concurrent(
    tmp_path, monkeypatch
):
    engine = await _engine(tmp_path / "parallel-adds.db")
    try:
        async with paused_add(engine, monkeypatch) as (first, release):
            # Pause only the existing call; a second ordinary write should be
            # admitted while the first still owns its shared add admission.
            monkeypatch.setattr(
                engine.hybrid_retriever,
                "add_memory",
                HybridRetriever.add_memory.__get__(engine.hybrid_retriever),
            )
            second_id = await asyncio.wait_for(
                engine.add_memory("tea", session_id=SOURCE), 5
            )
            assert await engine.get_memory(second_id) is not None
            assert not first.done()
            release.set()
            assert await asyncio.wait_for(first, 5) != second_id
    finally:
        await engine.close()
