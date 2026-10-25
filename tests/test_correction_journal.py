"""Recovery across interrupted confirmation and journal write failures."""

import asyncio

import pytest

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]


async def plan(service):
    memory_id = await add(service)
    preview = await service.preview_correction(memory_id, "改为茶", actor="webui:a")
    token = await service.confirm(preview["operation_id"], actor="webui:a")
    return memory_id, preview["operation_id"], token["confirmation_token"]


@pytest.mark.asyncio
async def test_journal_failure_keeps_original_and_compensates_new_copy(administration, monkeypatch):
    old_id, operation_id, token = await plan(administration)
    update = administration.store.update

    async def fail_prepared(*args, **kwargs):
        if kwargs.get("status") == "prepared_commit":
            raise OSError("journal unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(administration.store, "update", fail_prepared)
    with pytest.raises(OSError):
        await administration.execute_correction(operation_id, token, actor="webui:a")
    assert (await administration.snapshot(old_id))["text"] == "喜欢咖啡"
    rows = await (await administration.engine.db_connection.execute("SELECT id FROM documents")).fetchall()
    assert [row[0] for row in rows] == [old_id]
    assert (await administration.store.get(operation_id))["status"] == "failed"


@pytest.mark.asyncio
async def test_post_commit_log_failure_preserves_new_id_and_restart_can_rollback(administration, monkeypatch):
    from astrbot_plugin_anamnesis.core.managers.memory_administration import (
        MemoryAdministration,
    )

    old_id, operation_id, token = await plan(administration)
    update = administration.store.update

    async def fail_completed(*args, **kwargs):
        if kwargs.get("status") == "completed":
            raise OSError("completion unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(administration.store, "update", fail_completed)
    with pytest.raises(OSError):
        await administration.execute_correction(operation_id, token, actor="webui:a")
    stored = await administration.store.get(operation_id)
    assert stored["status"] == "prepared_commit"
    assert stored["result"]["new_memory_id"] > 0
    assert await administration.snapshot(old_id) is None
    fresh = MemoryAdministration(administration.engine, administration.config, administration.processor)
    await fresh.initialize()
    assert (await fresh.store.get(operation_id))["status"] == "completed"
    result = await fresh.rollback_correction(operation_id, actor="webui:a")
    assert (await fresh.snapshot(result["memory_id"]))["text"] == "喜欢咖啡"


@pytest.mark.asyncio
async def test_interrupted_rollback_completion_is_idempotent(administration, monkeypatch):
    old_id, operation_id, token = await plan(administration)
    await administration.execute_correction(operation_id, token, actor="webui:a")
    update = administration.store.update

    async def fail_rolled_back(*args, **kwargs):
        if kwargs.get("status") == "rolled_back":
            raise OSError("rollback completion unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(administration.store, "update", fail_rolled_back)
    with pytest.raises(OSError):
        await administration.rollback_correction(operation_id, actor="webui:a")
    stored = await administration.store.get(operation_id)
    assert stored["status"] == "rollback_prepared"
    rollback_id = stored["result"]["rollback_memory_id"]
    monkeypatch.setattr(administration.store, "update", update)
    assert (await administration.rollback_correction(operation_id, actor="webui:a"))["memory_id"] == rollback_id
    assert (await administration.snapshot(rollback_id))["text"] == "喜欢咖啡"
    assert await administration.snapshot(old_id) is None


@pytest.mark.asyncio
async def test_cancellation_before_token_claim_settles_authorized_execution(administration, monkeypatch):
    old_id, operation_id, token = await plan(administration)
    reached, release = asyncio.Event(), asyncio.Event()
    claim = administration._claim_and_execute

    async def paused(*args):
        reached.set()
        await release.wait()
        return await claim(*args)

    monkeypatch.setattr(administration, "_claim_and_execute", paused)
    caller = asyncio.create_task(administration.execute_correction(operation_id, token, actor="webui:a"))
    await asyncio.wait_for(reached.wait(), 2)
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert (await administration.store.get(operation_id))["status"] == "completed"
    assert await administration.snapshot(old_id) is None
