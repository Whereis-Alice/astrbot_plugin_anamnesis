"""Interrupted writes must not undo newer lifecycle and scope decisions."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom

from tests.test_archive_access_integration import make_engine


async def repair_engine(path):
    engine = await make_engine(path)
    await engine.atom_lifecycle_manager.stop()
    return engine


@pytest.mark.asyncio
async def test_failed_delete_repair_retains_source_and_retry_state(tmp_path):
    engine = await repair_engine(tmp_path / "delete.db")
    try:
        source = [{"content": "retained evidence"}]
        await engine.save_memory_source(1, source)
        engine.hybrid_retriever.delete_memory = AsyncMock(return_value=False)
        engine.graph_memory_manager = SimpleNamespace(delete_memory=AsyncMock())
        op_id = await engine._start_write_op("delete", {"memory_id": 1}, memory_id=1)
        assert await engine._repair_delete_write_op(op_id, 1) is False
        assert await engine.get_memory(1) is not None
        assert await engine.get_memory_source(1) == source
        engine.graph_memory_manager.delete_memory.assert_not_awaited()
        row = await (await engine.db_connection.execute(
            "SELECT status,retry_count FROM memory_write_ops WHERE id=?", (op_id,),
        )).fetchone()
        assert row["status"] == "needs_repair" and row["retry_count"] == 1
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["archived", "deleted"])
async def test_add_repair_does_not_reindex_inactive_parent(tmp_path, state):
    engine = await repair_engine(tmp_path / "inactive.db")
    try:
        await engine.db_connection.execute(
            "UPDATE documents SET metadata=json_set(metadata,'$.status',?) WHERE id=1", (state,),
        )
        await engine.db_connection.commit()
        engine.graph_memory_manager = SimpleNamespace(index_memory=AsyncMock())
        payload = {"failed_atoms": [engine._serialize_atom_for_repair(MemoryAtom(parent_memory_id=1, content="lost fact"))]}
        op_id = await engine._start_write_op("add", payload, memory_id=1)
        assert await engine._repair_add_write_op(op_id, 1, payload)
        assert await engine.atom_store.get_by_parent(1) == []
        engine.graph_memory_manager.index_memory.assert_not_awaited()
        assert (await engine.get_memory(1))["metadata"]["status"] == state
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["new-scope", None])
async def test_add_repair_uses_current_parent_scope_including_explicit_null(tmp_path, scope):
    engine = await repair_engine(tmp_path / "scope.db")
    try:
        await engine.db_connection.execute(
            "UPDATE documents SET metadata=json_set(metadata,'$.session_id',?,'$.persona_id',?) WHERE id=1",
            (scope, scope),
        )
        await engine.db_connection.commit()
        atom = MemoryAtom(parent_memory_id=1, content="lost fact", session_id="old-scope", persona_id="old-persona")
        payload = {"session_id": "old-scope", "persona_id": "old-persona",
                   "failed_atoms": [engine._serialize_atom_for_repair(atom)]}
        op_id = await engine._start_write_op("add", payload, memory_id=1)
        assert await engine._repair_add_write_op(op_id, 1, payload)
        stored = await engine.atom_store.get_by_parent(1)
        assert len(stored) == 1
        assert stored[0].session_id == scope and stored[0].persona_id == scope
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_add_repair_preserves_original_expiry_instead_of_renewing_ttl(tmp_path):
    engine = await repair_engine(tmp_path / "expiry.db")
    try:
        atom = MemoryAtom(parent_memory_id=1, content="lost fact", created_at=123, last_accessed_at=456,
                          expires_at=time.time() - 3600, ttl_days=7)
        payload = {"failed_atoms": [engine._serialize_atom_for_repair(atom)]}
        op_id = await engine._start_write_op("add", payload, memory_id=1)
        assert await engine._repair_add_write_op(op_id, 1, payload)
        stored = await engine.atom_store.get_by_parent(1)
        assert len(stored) == 1
        assert stored[0].created_at == 123 and stored[0].last_accessed_at == 456
        assert stored[0].expires_at == atom.expires_at
        assert await engine.atom_store.search_fts("lost") == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_source_flag_repair_preserves_newer_metadata_and_archive(tmp_path):
    engine = await repair_engine(tmp_path / "source-flags.db")
    try:
        await engine.db_connection.execute(
            "UPDATE documents SET metadata=json_set(metadata,'$.has_source',1,'$.source_message_count',2) WHERE id=1",
        )
        await engine.db_connection.commit()

        async def update_after_concurrent_change(memory_id, patch):
            current = (await engine.get_memory(memory_id))["metadata"]
            current.update(status="archived", user_note="new note", access_count=9)
            current.update(patch)
            await engine.db_connection.execute(
                "UPDATE documents SET metadata=? WHERE id=?", (json.dumps(current), memory_id),
            )
            await engine.db_connection.commit()
            return True

        engine.hybrid_retriever.update_metadata = update_after_concurrent_change
        engine.graph_memory_manager = SimpleNamespace(index_memory=AsyncMock())
        op_id = await engine._start_write_op("add", {}, memory_id=1)
        assert await engine._repair_add_write_op(op_id, 1, {})
        metadata = (await engine.get_memory(1))["metadata"]
        assert metadata["status"] == "archived" and metadata["access_count"] == 9
        assert metadata["user_note"] == "new note"
        assert metadata["has_source"] is False and metadata["source_message_count"] == 0
        engine.graph_memory_manager.index_memory.assert_not_awaited()
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("vector unavailable")])
async def test_batch_repair_vector_failure_persistently_requests_rebuild(tmp_path, failure):
    engine = await repair_engine(tmp_path / "vector.db")
    try:
        engine.faiss_db.delete = AsyncMock(
            side_effect=failure if isinstance(failure, Exception) else None, return_value=False,
        )
        await engine._delete_document_indexes_for_batch([1])
        assert await engine.get_memory(1) is None
        row = await (await engine.db_connection.execute(
            "SELECT value FROM migration_status WHERE key='document_index_rebuild_required'",
        )).fetchone()
        assert row is not None and row[0] == "true"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_batch_repair_releases_bm25_writer_before_external_document_delete(tmp_path):
    engine = await repair_engine(tmp_path / "external.db")
    deleted_externally = False
    try:
        async def vector_delete(uuid):
            nonlocal deleted_externally
            # The host vector adapter also deletes its document using a
            # separate connection to this same SQLite database.
            async with aiosqlite.connect(engine.db_path, timeout=0.05) as db:
                await db.execute("DELETE FROM documents WHERE doc_id=?", (uuid,))
                await db.commit()
                deleted_externally = True

        engine.faiss_db.delete = vector_delete
        await engine._delete_document_indexes_for_batch([1])
        assert deleted_externally
        assert await engine.get_memory(1) is None
    finally:
        await engine.close()
