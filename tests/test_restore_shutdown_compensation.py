"""A cancelled restore leaves its original archive retryable after real shutdown."""

import asyncio
import json
import time
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.managers.memory_administration import (
    MemoryAdministration,
)
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore

from tests.test_memory_administration import add
from tests.test_memory_transfer_runtime_safety import _engine


@pytest_asyncio.fixture
async def restore_context(tmp_path):
    engine = await _engine(tmp_path / "memory.db")
    engine.atom_enabled = False
    config = ConfigManager({"user_profile": {"enabled": True}})
    service = MemoryAdministration(engine, config)
    engine.memory_administration = service
    await service.initialize()
    profile_store = ConversationStore(str(tmp_path / "conversation.db"))
    await profile_store.initialize()
    profiles = UserProfileManager(config, SimpleNamespace(store=profile_store))
    engine.user_profile_manager = profiles
    profiles.memory_engine = engine
    event = SimpleNamespace(
        unified_msg_origin="qq:GroupMessage:1",
        get_sender_id=lambda: "alice",
        get_sender_name=lambda: "Alice",
        get_platform_name=lambda: "qq",
    )
    memory_id = await add(service)
    await profiles.upsert_facts(
        event,
        [
            {
                "key": "coffee",
                "value": "喜欢咖啡",
                "category": "preference",
                "explicit": True,
                "confidence": 0.95,
            }
        ],
        source_memory_id=memory_id,
    )
    try:
        yield engine, service, profiles, profile_store, event, memory_id
    finally:
        await engine.close()
        await profile_store.close()


async def read_record(path, memory_id):
    async with aiosqlite.connect(path) as db:
        row = await (
            await db.execute("SELECT metadata FROM documents WHERE id=?", (memory_id,))
        ).fetchone()
        fts = await (
            await db.execute(
                "SELECT doc_id FROM livingmemory_memories_fts WHERE doc_id=?",
                (memory_id,),
            )
        ).fetchall()
    return json.loads(row[0]), fts


@pytest.mark.asyncio
@pytest.mark.parametrize("moment", [1793000000.1234567, 1793000000.125])
@pytest.mark.parametrize("had_restore_marker", [False, True])
async def test_engine_close_compensates_profile_wait_and_restart_can_restore(
    restore_context, monkeypatch, moment, had_restore_marker
):
    engine, service, profiles, profile_store, event, memory_id = restore_context
    clock = [1792999990.875]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    if had_restore_marker:
        assert await engine.archive_memories([memory_id]) == 1
        assert await engine.restore_memory(memory_id)
    deleted = await service.delete([memory_id], actor="webui:a")
    operation_id = deleted["operation_id"]
    before = await service.snapshot(memory_id)
    assert ("restored_at" in before["metadata"]) == had_restore_marker
    clock[0] = moment
    unsuppression_started = asyncio.Event()
    unsuppress = profiles.set_source_suppressed

    async def observed(memory_ids, suppressed):
        if not suppressed:
            await engine.update_access_time(memory_id)
            unsuppression_started.set()
        return await unsuppress(memory_ids, suppressed)

    monkeypatch.setattr(profiles, "set_source_suppressed", observed)
    await profile_store._write_lock.acquire()
    restoration, shutdown, fresh = None, None, None
    try:
        restoration = asyncio.create_task(
            service.restore(operation_id, actor="webui:a")
        )
        await asyncio.wait_for(unsuppression_started.wait(), 5)
        active = await service.snapshot(memory_id)
        assert active["metadata"]["status"] == "active"
        shutdown = asyncio.create_task(engine.close())
        await asyncio.wait_for(shutdown, 5)
        with pytest.raises(asyncio.CancelledError):
            await restoration
        profile_store._write_lock.release()
        compensated, fts = await read_record(engine.db_path, memory_id)
        assert compensated["status"] == "archived" and fts == []
        assert ("restored_at" in compensated) == had_restore_marker
        assert compensated.get("restored_at") == before["metadata"].get("restored_at")
        assert compensated["archived_at"] == before["metadata"]["archived_at"]
        assert compensated["access_count"] == active["metadata"]["access_count"]
        assert compensated["last_access_time"] == active["metadata"]["last_access_time"]
        assert (await service.store.items(operation_id))[0]["status"] == "restoring"
        monkeypatch.setattr(profiles, "set_source_suppressed", unsuppress)
        fresh = await _engine(engine.db_path)
        fresh.atom_enabled = False
        fresh.user_profile_manager = profiles
        profiles.memory_engine = fresh
        resumed = MemoryAdministration(fresh, service.config)
        fresh.memory_administration = resumed
        recovered = await resumed.restore(operation_id, actor="webui:a")
        assert recovered["ids"] == [memory_id] and recovered["conflicts"] == []
        assert await resumed.restore(operation_id, actor="webui:a") == recovered
        current, fts = await read_record(engine.db_path, memory_id)
        assert current["status"] == "active" and len(fts) == 1
        assert len(await profiles.get_profile(event)) == 1
    finally:
        if profile_store._write_lock.locked():
            profile_store._write_lock.release()
        for task in (restoration, shutdown):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (restoration, shutdown) if task is not None),
            return_exceptions=True,
        )
        if fresh is not None:
            await fresh.close()


@pytest.mark.asyncio
async def test_cancelled_commit_await_is_reconciled_before_shutdown_finishes(
    restore_context, monkeypatch
):
    engine, service, profiles, _, _, memory_id = restore_context
    deleted = await service.delete([memory_id], actor="webui:a")
    operation_id = deleted["operation_id"]
    before = await service.snapshot(memory_id)
    committed, release = asyncio.Event(), asyncio.Event()
    commit = engine.db_connection.commit

    async def observed_commit():
        await commit()
        row = await (
            await engine.db_connection.execute(
                "SELECT json_extract(metadata,'$.status') FROM documents WHERE id=?",
                (memory_id,),
            )
        ).fetchone()
        if row[0] == "active" and not committed.is_set():
            committed.set()
            await release.wait()

    monkeypatch.setattr(engine.db_connection, "commit", observed_commit)
    restoration = asyncio.create_task(service.restore(operation_id, actor="webui:a"))
    shutdown, fresh = None, None
    try:
        await asyncio.wait_for(committed.wait(), 5)
        shutdown = asyncio.create_task(engine.close())
        await asyncio.wait_for(shutdown, 5)
        with pytest.raises(asyncio.CancelledError):
            await restoration
        compensated, fts = await read_record(engine.db_path, memory_id)
        assert compensated == before["metadata"] and fts == []
        fresh = await _engine(engine.db_path)
        fresh.atom_enabled = False
        fresh.user_profile_manager = profiles
        profiles.memory_engine = fresh
        resumed = MemoryAdministration(fresh, service.config)
        fresh.memory_administration = resumed
        result = await resumed.restore(operation_id, actor="webui:a")
        assert result["ids"] == [memory_id] and result["conflicts"] == []
        assert len((await read_record(engine.db_path, memory_id))[1]) == 1
    finally:
        release.set()
        for task in (restoration, shutdown):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (restoration, shutdown) if task is not None),
            return_exceptions=True,
        )
        if fresh is not None:
            await fresh.close()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_restore_compensation(
    restore_context, monkeypatch
):
    engine, service, _, _, _, memory_id = restore_context
    await service.delete([memory_id], actor="webui:a")
    before = await service.snapshot(memory_id)
    inserting, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    embedding_delete = engine.faiss_db.embedding_storage.delete
    embedding_insert = engine.faiss_db.embedding_storage.insert

    async def inserted_and_waiting(*args):
        await embedding_insert(*args)
        inserting.set()
        await asyncio.Future()

    async def paused_cleanup(*args):
        cleaning.set()
        await release.wait()
        return await embedding_delete(*args)

    monkeypatch.setattr(
        engine.faiss_db.embedding_storage, "insert", inserted_and_waiting
    )
    monkeypatch.setattr(engine.faiss_db.embedding_storage, "delete", paused_cleanup)
    restoration = asyncio.create_task(engine.restore_memory(memory_id))
    try:
        await asyncio.wait_for(inserting.wait(), 5)
        restoration.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        restoration.cancel()
        await asyncio.sleep(0)
        restoration.cancel()
        await asyncio.sleep(0)
        assert not restoration.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(restoration, 5)
        assert (await service.snapshot(memory_id)) == before
        assert (await read_record(engine.db_path, memory_id))[1] == []
        assert not engine._memory_mutation_lock.locked()
    finally:
        release.set()
        if not restoration.done():
            restoration.cancel()
        await asyncio.gather(restoration, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_restore_preserves_later_note_and_recovery_conflict(
    restore_context, monkeypatch
):
    engine, service, profiles, _, _, memory_id = restore_context
    deleted = await service.delete([memory_id], actor="webui:a")

    async def changed_note_then_cancelled(_memory_ids, _suppressed):
        assert await engine.update_memory(
            memory_id, {"metadata": {"user_note": "保留后来修改的备注"}}
        )
        raise asyncio.CancelledError

    monkeypatch.setattr(profiles, "set_source_suppressed", changed_note_then_cancelled)
    with pytest.raises(asyncio.CancelledError):
        await engine.restore_memory(memory_id)
    current, fts = await read_record(engine.db_path, memory_id)
    assert current["status"] == "archived" and fts == []
    assert current["user_note"] == "保留后来修改的备注"
    assert "restored_at" not in current
    recovered = await service.restore(deleted["operation_id"], actor="webui:a")
    assert recovered["ids"] == [] and recovered["conflicts"] == [memory_id]
    assert (await service.snapshot(memory_id))["metadata"]["user_note"] == current[
        "user_note"
    ]


@pytest.mark.asyncio
async def test_cancelled_restore_does_not_compensate_a_newer_active_source(
    restore_context, monkeypatch
):
    engine, service, profiles, _, _, memory_id = restore_context
    await service.delete([memory_id], actor="webui:a")
    later_restore_marker = time.time() + 10

    async def changed_source_then_cancelled(_memory_ids, _suppressed):
        assert await engine.update_memory(
            memory_id,
            {
                "metadata": {
                    "session_id": "qq:GroupMessage:2",
                    "persona_id": "another-persona",
                    "restored_at": later_restore_marker,
                }
            },
        )
        raise asyncio.CancelledError

    monkeypatch.setattr(
        profiles, "set_source_suppressed", changed_source_then_cancelled
    )
    with pytest.raises(asyncio.CancelledError):
        await engine.restore_memory(memory_id)
    current, fts = await read_record(engine.db_path, memory_id)
    assert current["status"] == "active" and len(fts) == 1
    assert current["session_id"] == "qq:GroupMessage:2"
    assert current["persona_id"] == "another-persona"
    assert current["restored_at"] == later_restore_marker
    assert engine.index_rebuild_required
