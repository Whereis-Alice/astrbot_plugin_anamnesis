"""Projection intents survive old completions, cancellation and graph rebuilds."""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.managers.memory_mutation import (
    index_projection_guard,
)
from astrbot_plugin_anamnesis.core.models.memory_atom import (
    AtomStatus,
    AtomType,
    MemoryAtom,
)
from astrbot_plugin_anamnesis.core.validators.index_validator import IndexValidator
from astrbot_plugin_anamnesis.storage.projection_outbox import ProjectionOutbox


@pytest_asyncio.fixture
async def projection(tmp_path):
    path = str(tmp_path / "projection.db")
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY,text TEXT,metadata TEXT)")
        await db.execute("CREATE TABLE memory_write_ops(memory_id INTEGER,status TEXT)")
        await db.commit()
    async def snapshot(memory_id):
        async with aiosqlite.connect(path) as db:
            row = await (await db.execute("SELECT text,metadata FROM documents WHERE id=?", (memory_id,))).fetchone()
            return {"text": row[0], "metadata": json.loads(row[1]), "source_messages": []} if row else None
    engine = SimpleNamespace(
        db_path=path, config={}, _replacement_snapshot=snapshot,
        _replacement_snapshot_matches=lambda left, right: left == right,
        _invalidate_search_cache=Mock(), _set_index_rebuild_required=AsyncMock(),
        faiss_db=SimpleNamespace(embedding_storage=SimpleNamespace(delete=AsyncMock(), insert=AsyncMock()),
                                embedding_provider=SimpleNamespace(get_embedding_with_retry=AsyncMock(return_value=[.1, .2]))),
        bm25_retriever=SimpleNamespace(add_document=AsyncMock(), delete_document=AsyncMock()),
        graph_memory_manager=SimpleNamespace(_rebuild_active=False, index_memory=AsyncMock(), delete_memory=AsyncMock()),
        atom_enabled=True, atom_store=SimpleNamespace(get_by_parent=AsyncMock(return_value=[])))
    result = ProjectionOutbox(engine)
    await result.initialize()
    return result


async def write(projection, sql, parameters=()):
    async with projection.store.connection(write=True) as db:
        await db.execute(sql, parameters)


async def add(projection, metadata=None):
    await write(projection, "INSERT INTO documents VALUES(1,'summary',?)", (json.dumps(metadata or {"status": "active"}),))
    return (await projection.pending())[0]


@pytest.mark.asyncio
async def test_aba_old_completion_cannot_clear_intent_after_completed_cycle(projection):
    first = await add(projection)
    assert await projection.complete(1, first["revision"])
    await write(projection, "UPDATE documents SET text='new' WHERE id=1")
    second = (await projection.pending())[0]
    assert second["revision"] > first["revision"]
    assert not await projection.complete(1, first["revision"])
    assert (await projection.pending())[0]["revision"] == second["revision"]


@pytest.mark.asyncio
async def test_primary_rollback_rolls_back_intent_and_monotonic_clock(projection):
    first = await add(projection)
    async with projection.store.connection(write=True) as db:
        await db.execute("UPDATE documents SET text='uncommitted' WHERE id=1")
        await db.rollback()
    assert (await projection.pending())[0]["revision"] == first["revision"]
    await write(projection, "UPDATE documents SET text='committed' WHERE id=1")
    assert (await projection.pending())[0]["revision"] == first["revision"] + 1


@pytest.mark.asyncio
async def test_reinitialization_does_not_reset_completed_clock(projection):
    first = await add(projection)
    await projection.complete(1, first["revision"])
    await projection.initialize()
    await write(projection, "UPDATE documents SET text='new' WHERE id=1")
    assert (await projection.pending())[0]["revision"] > first["revision"]


@pytest.mark.asyncio
async def test_expired_forgotten_and_purged_atoms_never_rebuild_graph(projection):
    await add(projection, {"status": "active", "atom_types": ["preference"], "key_facts": ["expired"]})
    projection.engine.atom_store.get_by_parent.return_value = [
        MemoryAtom(parent_memory_id=1, content="expired", atom_type=AtomType.PREFERENCE, expires_at=time.time()-1),
        MemoryAtom(parent_memory_id=1, content="forgotten", atom_type=AtomType.PREFERENCE, status=AtomStatus.FORGOTTEN, expires_at=time.time()+100)]
    assert (await projection.repair())["repaired"] == 1
    projection.engine.graph_memory_manager.index_memory.assert_not_awaited()
    projection.engine.graph_memory_manager.delete_memory.assert_awaited_once_with(1)
    # Already-purged atoms have only a historical document marker.
    await write(projection, "UPDATE documents SET text='another historical summary' WHERE id=1")
    projection.engine.atom_store.get_by_parent.return_value = []
    await projection.repair()
    projection.engine.graph_memory_manager.index_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_projection_contains_only_live_atom_content(projection):
    await add(projection, {"status": "active", "atom_types": ["preference"], "key_facts": ["expired", "live"]})
    live = MemoryAtom(parent_memory_id=1, content="live", atom_type=AtomType.PREFERENCE, expires_at=time.time()+100)
    projection.engine.atom_store.get_by_parent.return_value = [MemoryAtom(parent_memory_id=1, content="expired", atom_type=AtomType.PREFERENCE, expires_at=time.time()-1), live]
    await projection.repair()
    args = projection.engine.graph_memory_manager.index_memory.await_args.args
    assert args[1] == "live" and args[2]["key_facts"] == ["live"] and args[3] == [live]


@pytest.mark.asyncio
async def test_active_graph_rebuild_defers_entire_projection_and_keeps_intent(projection):
    await add(projection)
    projection.engine.graph_memory_manager._rebuild_active = True
    assert (await projection.repair())["repaired"] == 0
    assert await projection.pending()
    projection.engine.faiss_db.embedding_storage.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_delta_queue_does_not_count_as_durable_projection_completion(projection):
    await add(projection)
    async def queued(*args):
        projection.engine.graph_memory_manager._rebuild_active = True
    projection.engine.graph_memory_manager.index_memory.side_effect = queued
    assert (await projection.repair())["repaired"] == 0
    assert await projection.pending()


@pytest.mark.asyncio
async def test_cancelled_vector_insert_retains_intent_for_retry(projection):
    await add(projection)
    projection.engine.faiss_db.embedding_storage.insert.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await projection.repair()
    assert await projection.pending()
    projection.engine.faiss_db.embedding_storage.insert.side_effect = None
    assert (await projection.repair())["repaired"] == 1
    assert not await projection.pending()


@pytest.mark.asyncio
async def test_real_full_rebuild_entrypoint_excludes_projection_worker(projection):
    await add(projection)
    started, release = asyncio.Event(), asyncio.Event()
    validator = IndexValidator(projection.engine.db_path, projection.engine.faiss_db)
    async def rebuild(*args, **kwargs):
        started.set()
        await release.wait()
        return {"success": True}
    validator._rebuild_indexes_locked = rebuild
    full = asyncio.create_task(validator.rebuild_indexes(projection.engine, force_full_vector=True))
    await asyncio.wait_for(started.wait(), 10)
    replay = asyncio.create_task(projection.repair())
    await asyncio.sleep(.01)
    projection.engine.faiss_db.embedding_provider.get_embedding_with_retry.assert_not_awaited()
    release.set()
    await full
    assert (await replay)["repaired"] == 1


@pytest.mark.asyncio
async def test_projection_guard_is_reentrant_and_released_after_cancellation(projection):
    entered = asyncio.Event()
    async def owner():
        async with index_projection_guard(projection.engine), index_projection_guard(projection.engine):
            entered.set()
            await asyncio.Future()
    task = asyncio.create_task(owner())
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(1), index_projection_guard(projection.engine):
        assert projection.engine._index_projection_owner is asyncio.current_task()


@pytest.mark.asyncio
async def test_initializer_missing_primary_schema_fails_fast(tmp_path):
    engine = SimpleNamespace(db_path=str(tmp_path / "invalid.db"), config={})
    with pytest.raises(sqlite3.OperationalError, match="documents"):
        await ProjectionOutbox(engine).initialize()
