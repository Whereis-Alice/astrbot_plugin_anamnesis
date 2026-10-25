"""Expired missing sources permit explicit admin cleanup without deleting edits."""

import asyncio
import time

import pytest

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]


async def expired_operation(service, monkeypatch, operation_id):
    operation = await service.store.get(operation_id)
    monkeypatch.setattr(time, "time", lambda: operation["expires_at"] + 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired,actor,purge,source", [
    (True, "webui:a", True, "missing"),
    (False, "webui:a", True, "missing"),
    (True, "tool:scope:user", True, "missing"),
    (True, "webui:a", True, "edited"),
    (True, "webui:a", True, "restored"),
    (True, "webui:a", False, "missing"),
    (True, "tool:scope:user", False, "missing"),
])
async def test_missing_snapshot_cleanup_requires_expired_explicit_admin_purge(
    administration, monkeypatch, expired, actor, purge, source
):
    memory_id = await add(administration)
    deleted = await administration.delete([memory_id], actor=actor)
    operation_id = deleted["operation_id"]
    if source == "missing":
        await administration.engine.batch_delete_memories([memory_id])
    elif source == "edited":
        assert await administration.engine.update_memory(memory_id, {"metadata": {"independent_edit": True}})
    else:
        assert await administration.engine.restore_memory(memory_id)
    before = await administration.snapshot(memory_id)
    if expired:
        await expired_operation(administration, monkeypatch, operation_id)
    if expired and not purge:
        with pytest.raises(ValueError, match="期限"):
            await administration.restore(operation_id, actor=actor)
        assert (await administration.store.items(operation_id))[0]["payload"]
    else:
        result = await administration.restore(operation_id, actor=actor, purge=purge)
        item = (await administration.store.items(operation_id))[0]
        if expired and actor.startswith("webui:") and source == "missing":
            assert result["ids"] == [memory_id] and result["conflicts"] == []
            assert item["status"] == "purged" and item["payload"] == {}
            assert await administration.remove_operation(operation_id, actor=actor) == {"removed": True}
            assert await administration.store.get(operation_id) is None
        else:
            assert result["conflicts"] == [memory_id]
            assert item["payload"] and item["status"] == "deleted"
    assert await administration.snapshot(memory_id) == before


@pytest.mark.asyncio
async def test_expired_multi_item_cleanup_preserves_changed_source_conflicts(administration, monkeypatch):
    missing = await add(administration)
    changed = await add(administration, "喜欢书")
    deleted = await administration.delete([missing, changed], actor="webui:a")
    operation_id = deleted["operation_id"]
    await administration.engine.batch_delete_memories([missing])
    assert await administration.engine.update_memory(changed, {"metadata": {"independent_edit": True}})
    before = await administration.snapshot(changed)
    await expired_operation(administration, monkeypatch, operation_id)
    result = await administration.restore(operation_id, actor="webui:a", purge=True)
    assert result["ids"] == [missing] and result["conflicts"] == [changed]
    items = {int(item["item_key"]): item for item in await administration.store.items(operation_id)}
    assert items[missing]["status"] == "purged" and items[missing]["payload"] == {}
    assert items[changed]["status"] == "deleted" and items[changed]["payload"]
    usage = await administration.store.storage_usage()
    again = await administration.restore(operation_id, actor="webui:a", purge=True)
    assert again["ids"] == [] and again["conflicts"] == [changed]
    assert (await administration.store.storage_usage())["used_bytes"] <= usage["used_bytes"]
    assert await administration.snapshot(changed) == before
    with pytest.raises(ValueError):
        await administration.remove_operation(operation_id, actor="webui:a")


@pytest.mark.asyncio
@pytest.mark.parametrize("error,committed", [
    (OSError, False), (OSError, True), (asyncio.CancelledError, True),
])
async def test_expired_missing_cleanup_can_retry_after_failure_or_cancellation(
    administration, monkeypatch, error, committed
):
    memory_id = await add(administration)
    deleted = await administration.delete([memory_id], actor="webui:a")
    operation_id = deleted["operation_id"]
    await administration.engine.batch_delete_memories([memory_id])
    await expired_operation(administration, monkeypatch, operation_id)
    put = administration.store.put_item

    async def interrupted_put(*args, **kwargs):
        if kwargs.get("status") == "purged":
            if committed:
                await put(*args, **kwargs)
            raise error("snapshot cleanup interrupted")
        return await put(*args, **kwargs)

    monkeypatch.setattr(administration.store, "put_item", interrupted_put)
    with pytest.raises(error):
        await administration.restore(operation_id, actor="webui:a", purge=True)
    item = (await administration.store.items(operation_id))[0]
    assert (item["payload"] == {}) == committed
    monkeypatch.setattr(administration.store, "put_item", put)
    result = await administration.restore(operation_id, actor="webui:a", purge=True)
    assert result["ids"] == ([] if committed else [memory_id]) and result["conflicts"] == []
    assert (await administration.store.get(operation_id))["status"] == "purged"
    assert await administration.snapshot(memory_id) is None
    await administration.remove_operation(operation_id, actor="webui:a")
    assert (await administration.store.storage_usage())["used_bytes"] == 0


@pytest.mark.asyncio
async def test_restore_rechecks_retention_after_waiting_for_mutation_guard(administration, monkeypatch):
    memory_id = await add(administration)
    deleted = await administration.delete([memory_id], actor="webui:a")
    operation_id = deleted["operation_id"]
    operation = await administration.store.get(operation_id)
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    owned = administration._owned
    checked = asyncio.Event()

    async def observed_owned(*args, **kwargs):
        result = await owned(*args, **kwargs)
        checked.set()
        return result

    monkeypatch.setattr(administration, "_owned", observed_owned)
    restoring = None
    try:
        async with administration.engine.memory_mutation_guard():
            restoring = asyncio.create_task(administration.restore(operation_id, actor="webui:a"))
            await asyncio.wait_for(checked.wait(), 3)
            clock[0] = operation["expires_at"] + 1
        with pytest.raises(ValueError, match="期限"):
            await asyncio.wait_for(restoring, 3)
        assert (await administration.snapshot(memory_id))["metadata"]["status"] == "archived"
        assert (await administration.store.items(operation_id))[0]["payload"]
    finally:
        if restoring is not None:
            restoring.cancel()
            await asyncio.gather(restoring, return_exceptions=True)
