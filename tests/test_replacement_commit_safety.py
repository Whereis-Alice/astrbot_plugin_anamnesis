"""Replacement must keep a recoverable copy and the facts' original lifetime."""

import asyncio
import json
import time

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_anamnesis.core.processors.atom_classifier import classify_atoms
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.test_memory_engine import _FakeFaissDB


async def make_engine(tmp_path):
    faiss = _FakeFaissDB()
    engine = MemoryEngine(
        db_path=str(tmp_path / "replace.db"), faiss_db=faiss,
        config={"atom_enabled": True, "graph_memory_enabled": False},
    )
    faiss._sqlite_path = engine.db_path
    await engine.initialize()
    await engine.atom_lifecycle_manager.stop()
    sql_engine = create_async_engine(URL.create("sqlite+aiosqlite", database=engine.db_path))
    faiss.document_storage.get_session = async_sessionmaker(sql_engine)

    async def get_documents(metadata_filters, ids=None, limit=50, offset=0):
        async with aiosqlite.connect(engine.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM documents ORDER BY id") as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
        for row in rows:
            row["metadata"] = json.loads(row["metadata"])
        return [row for row in rows if (ids is None or row["id"] in ids) and all(
            row["metadata"].get(key) == value for key, value in metadata_filters.items()
        )][offset:offset + limit]

    faiss.document_storage.get_documents = get_documents
    close = engine.close

    async def close_with_sessions():
        await close()
        await sql_engine.dispose()

    engine.close = close_with_sessions
    return engine


async def add_original(engine):
    atoms = classify_atoms(key_facts=["Alice likes coffee"], topics=[], participants=[],
                           parent_importance=0.5, session_id="s1", persona_id="p1")
    return await engine.add_memory(
        "old summary", session_id="s1", persona_id="p1",
        metadata={"key_facts": ["Alice likes coffee"]}, atoms=atoms,
        source_messages=[{"id": 1, "content": "original statement"}],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_failure_after_old_primary_deleted_keeps_new_copy(tmp_path, failure):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)
        original_delete = engine.delete_memory
        deleted = asyncio.Event()

        async def delete_then_fail(memory_id):
            result = await original_delete(memory_id)
            if memory_id == old:
                deleted.set()
                if failure == "error":
                    raise RuntimeError("cleanup failed after primary commit")
                await asyncio.Future()
            return result

        engine.delete_memory = delete_then_fail
        task = asyncio.create_task(engine.replace_memory(
            old, content="new summary", metadata={}, importance=0.5,
        ))
        await asyncio.wait_for(deleted.wait(), 2)
        if failure == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await asyncio.wait_for(task, 2)
        assert await engine.get_memory(old) is None
        candidate = await engine.get_memory(old + 1)
        assert candidate and candidate["text"] == "new summary"
        assert await engine.get_memory_source(old + 1) == [{"id": 1, "content": "original statement"}]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_replace_rejects_changed_primary_after_embedding(tmp_path):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)
        original_add = engine.add_memory

        async def change_source_during_embedding(**kwargs):
            candidate = await original_add(**kwargs)
            await engine.db_connection.execute(
                "UPDATE documents SET metadata=json_set(metadata,'$.status','archived') WHERE id=?", (old,),
            )
            await engine.db_connection.commit()
            return candidate

        engine.add_memory = change_source_during_embedding
        with pytest.raises(RuntimeError, match="修改"):
            await engine.replace_memory(old, content="new summary", metadata={}, importance=0.5)
        assert (await engine.get_memory(old))["metadata"]["status"] == "archived"
        assert await engine.get_memory(old + 1) is None
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["expired", "forgotten", "superseded"])
async def test_edit_preserves_unchanged_atom_lifecycle(tmp_path, status):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)
        await engine.db_connection.execute(
            "UPDATE memory_atoms SET status=?,expires_at=?,created_at=100,last_accessed_at=110,"
            "last_reinforced_at=120,reinforcement_count=3 WHERE parent_memory_id=?",
            (status, time.time() - 86400, old),
        )
        await engine.db_connection.commit()
        snapshot = (await engine.snapshot_archived_atom_lifecycle(old))[0]
        new = await engine.replace_memory(old, content="edited summary", metadata={"topics": ["coffee"]}, importance=0.6)
        atoms = await engine.atom_store.get_by_parent(new)
        assert len(atoms) == 1
        assert atoms[0].status.value == status
        for key in ("expires_at", "created_at", "last_accessed_at", "last_reinforced_at", "reinforcement_count"):
            assert getattr(atoms[0], key) == snapshot[key]
        assert atoms[0].entities == ["coffee"]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_archived_edit_snapshot_contains_only_new_facts(tmp_path):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)
        await engine.archive_memories([old])
        new = await engine.replace_memory(
            old, content="Alice likes tea", metadata={"key_facts": ["Alice likes tea"]}, importance=0.5,
        )
        metadata = (await engine.get_memory(new))["metadata"]
        assert metadata["status"] == "archived"
        assert [row["content"] for row in metadata["archived_atom_lifecycle"]] == ["Alice likes tea"]
        assert await engine.atom_store.get_by_parent(new) == []
        # Restoring the persisted snapshot cannot bring the replaced coffee fact back.
        await engine.restore_archived_atom_lifecycle(new, metadata["archived_atom_lifecycle"])
        assert [row.content for row in await engine.atom_store.get_by_parent(new)] == ["Alice likes tea"]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_replace_rejects_stale_editor_snapshot(tmp_path):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)
        expected = await engine.get_memory(old)
        await engine.db_connection.execute(
            "UPDATE documents SET metadata=json_set(metadata,'$.session_id','other') WHERE id=?", (old,),
        )
        await engine.db_connection.commit()
        with pytest.raises(RuntimeError, match="修改"):
            await engine.replace_memory(old, content="stale summary", metadata=expected["metadata"],
                                        importance=0.5, expected_memory=expected)
        assert (await engine.get_memory(old))["metadata"]["session_id"] == "other"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_add_and_rolls_back_safely(tmp_path):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)
        original_add = engine.add_memory
        created = asyncio.Event()
        release = asyncio.Event()

        async def blocked_add(**kwargs):
            candidate = await original_add(**kwargs)
            created.set()
            await release.wait()
            return candidate

        engine.add_memory = blocked_add
        task = asyncio.create_task(engine.replace_memory(old, content="new summary", metadata={}, importance=0.5))
        await asyncio.wait_for(created.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert await engine.get_memory(old)
        assert await engine.get_memory(old + 1) is None
        assert await engine.get_memory_source(old + 1) == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_replacement_with_incomplete_derived_write_keeps_original(tmp_path, monkeypatch):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)

        async def failed_insert(*args, **kwargs):
            raise RuntimeError("atom storage unavailable")

        monkeypatch.setattr(engine.atom_store, "insert", failed_insert)
        monkeypatch.setattr(engine.atom_store, "insert_many", failed_insert)
        with pytest.raises(RuntimeError, match="完整写入"):
            await engine.replace_memory(old, content="edited summary", metadata={}, importance=0.5)
        assert await engine.get_memory(old)
        assert await engine.get_memory(old + 1) is None
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_new_facts_get_initial_ttl_while_existing_facts_keep_old_expiry(tmp_path):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)
        await engine.db_connection.execute("UPDATE memory_atoms SET expires_at=100,status='expired' WHERE parent_memory_id=?", (old,))
        await engine.db_connection.commit()
        new = await engine.replace_memory(
            old, content="updated summary", importance=0.5,
            metadata={"key_facts": ["Alice likes coffee", "Alice works in Tokyo"]},
        )
        atoms = {atom.content: atom for atom in await engine.atom_store.get_by_parent(new)}
        assert atoms["Alice likes coffee"].expires_at == 100
        assert atoms["Alice likes coffee"].status.value == "expired"
        assert atoms["Alice works in Tokyo"].expires_at > time.time()
        assert atoms["Alice works in Tokyo"].status.value == "active"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_edit_does_not_reactivate_an_unchanged_purged_fact(tmp_path):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)
        await engine.atom_store.delete_by_parent(old)
        new = await engine.replace_memory(
            old, content="updated summary", importance=0.5,
            metadata={"key_facts": ["Alice likes coffee", "Alice works in Tokyo"]},
        )
        atoms = {atom.content: atom for atom in await engine.atom_store.get_by_parent(new)}
        assert atoms["Alice likes coffee"].status.value == "forgotten"
        assert atoms["Alice likes coffee"].expires_at <= time.time()
        assert atoms["Alice works in Tokyo"].status.value == "active"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_source_edit_waits_for_replacement_and_cannot_leave_an_orphan(tmp_path):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)
        original_add = engine.add_memory
        created = asyncio.Event()
        release = asyncio.Event()

        async def blocked_add(**kwargs):
            candidate = await original_add(**kwargs)
            created.set()
            await release.wait()
            return candidate

        engine.add_memory = blocked_add
        replacement = asyncio.create_task(engine.replace_memory(old, content="new summary", metadata={}, importance=0.5))
        await asyncio.wait_for(created.wait(), 2)
        source_edit = asyncio.create_task(engine.save_memory_source(old, [{"content": "later edit"}]))
        await asyncio.sleep(0)
        assert not source_edit.done()
        release.set()
        new = await asyncio.wait_for(replacement, 2)
        with pytest.raises(RuntimeError, match="已删除"):
            await asyncio.wait_for(source_edit, 2)
        assert await engine.get_memory_source(old) == []
        assert await engine.get_memory_source(new) == [{"id": 1, "content": "original statement"}]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_shutdown_during_replacement_add_leaves_only_complete_original(tmp_path, monkeypatch):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)
        original_insert = engine.atom_store.insert_many
        staged = asyncio.Event()
        release = asyncio.Event()

        async def block_atom_write(*args, **kwargs):
            staged.set()
            await release.wait()
            return await original_insert(*args, **kwargs)

        monkeypatch.setattr(engine.atom_store, "insert_many", block_atom_write)
        replacement = asyncio.create_task(engine.replace_memory(old, content="new summary", metadata={}, importance=0.5))
        await asyncio.wait_for(staged.wait(), 2)
        closing = asyncio.create_task(engine.close())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(closing, 2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(replacement, 2)
        async with aiosqlite.connect(engine.db_path) as db:
            async with db.execute("SELECT id,text FROM documents ORDER BY id") as cursor:
                assert await cursor.fetchall() == [(old, "old summary")]
            async with db.execute("SELECT memory_id FROM memory_sources ORDER BY memory_id") as cursor:
                assert await cursor.fetchall() == [(old,)]
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", [False, True, 0, -1, "2", None])
async def test_invalid_add_id_never_deletes_original(tmp_path, invalid_id, monkeypatch):
    engine = await make_engine(tmp_path)
    try:
        old = await add_original(engine)

        async def invalid_add(**kwargs):
            return invalid_id

        monkeypatch.setattr(engine, "add_memory", invalid_add)
        with pytest.raises(RuntimeError, match="创建失败"):
            await engine.replace_memory(old, content="new summary", metadata={}, importance=0.5)
        assert await engine.get_memory(old)
        assert await engine.get_memory_source(old) == [{"id": 1, "content": "original statement"}]
    finally:
        await engine.close()
