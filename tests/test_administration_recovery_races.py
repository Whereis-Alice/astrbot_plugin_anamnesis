"""Management interruption and concurrent action regression tests."""

import asyncio

import pytest

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]


@pytest.mark.asyncio
async def test_interrupted_purge_can_finish_after_document_delete(administration, monkeypatch):
    from astrbot_plugin_anamnesis.core.managers.memory_administration import (
        MemoryAdministration,
    )

    memory_id = await add(administration)
    deleted = await administration.delete([memory_id], actor="webui:a")
    operation_id = deleted["operation_id"]
    put_item = administration.store.put_item

    async def fail_completion(*args, **kwargs):
        if kwargs.get("status") == "purged":
            raise OSError("purge completion unavailable")
        return await put_item(*args, **kwargs)

    monkeypatch.setattr(administration.store, "put_item", fail_completion)
    with pytest.raises(OSError):
        await administration.restore(operation_id, actor="webui:a", purge=True)
    assert await administration.snapshot(memory_id) is None
    monkeypatch.setattr(administration.store, "put_item", put_item)

    fresh = MemoryAdministration(administration.engine, administration.config, administration.processor)
    result = await fresh.restore(operation_id, actor="webui:a", purge=True)
    assert result["ids"] == [memory_id]
    assert result["conflicts"] == []
    item = (await administration.store.items(operation_id))[0]
    assert item["status"] == "purged" and item["payload"] == {}


@pytest.mark.asyncio
async def test_missing_document_without_purge_intent_is_a_conflict(administration):
    memory_id = await add(administration)
    deleted = await administration.delete([memory_id], actor="webui:a")
    await administration.engine.delete_memory(memory_id)
    result = await administration.restore(deleted["operation_id"], actor="webui:a", purge=True)
    assert result["conflicts"] == [memory_id]
    assert (await administration.store.items(deleted["operation_id"]))[0]["payload"]


@pytest.mark.asyncio
async def test_purge_journal_failure_keeps_archived_source(administration, monkeypatch):
    memory_id = await add(administration)
    deleted = await administration.delete([memory_id], actor="webui:a")
    put_item = administration.store.put_item

    async def fail_intent(*args, **kwargs):
        if kwargs.get("status") == "purging":
            raise OSError("purge intent unavailable")
        return await put_item(*args, **kwargs)

    monkeypatch.setattr(administration.store, "put_item", fail_intent)
    with pytest.raises(OSError):
        await administration.restore(deleted["operation_id"], actor="webui:a", purge=True)
    assert (await administration.snapshot(memory_id))["metadata"]["status"] == "archived"


@pytest.mark.asyncio
@pytest.mark.parametrize("rebuild", [False, True])
async def test_episode_result_failure_does_not_publish_a_partial_event(administration, monkeypatch, rebuild):
    memory_id = await add(administration)
    payload = {"memory_ids": [memory_id], "use_llm": False, "title": "new event"}
    original = None
    if rebuild:
        first = await administration.create_job("episode", payload)
        await administration._jobs[first["operation_id"]]
        original = (await administration.list_episodes())[0]
        payload = {**payload, "episode_id": original["id"], "expected_revision": original["revision"], "title": "rebuilt event"}
    encode = administration.store._encode

    def fail_result(value):
        if isinstance(value, dict) and "episode_id" in value and "title" in value and "memory_ids" in value and "use_llm" not in value:
            raise ValueError("result storage unavailable")
        return encode(value)

    monkeypatch.setattr(administration.store, "_encode", fail_result)
    job = await administration.create_job("episode", payload)
    await administration._jobs[job["operation_id"]]
    assert (await administration.store.get(job["operation_id"]))["status"] == "failed"
    episodes = await administration.list_episodes()
    assert episodes == ([original] if rebuild else [])


@pytest.mark.asyncio
async def test_cancellation_after_episode_commit_keeps_completed_publication(administration, monkeypatch):
    memory_id = await add(administration)
    generate = administration._run_episode_job

    async def cancel_after_commit(job):
        await generate(job)
        raise asyncio.CancelledError

    monkeypatch.setattr(administration, "_run_episode_job", cancel_after_commit)
    job = await administration.create_job("episode", {"memory_ids": [memory_id], "use_llm": False})
    await asyncio.gather(administration._jobs[job["operation_id"]], return_exceptions=True)
    operation = await administration.store.get(job["operation_id"])
    assert operation["status"] == "completed"
    episodes = await administration.list_episodes()
    assert len(episodes) == 1 and operation["result"]["episode_id"] == episodes[0]["id"]


@pytest.mark.asyncio
async def test_cancel_completed_job_cannot_make_it_retryable(administration):
    memory_id = await add(administration)
    job = await administration.create_job("episode", {"memory_ids": [memory_id], "use_llm": False})
    await administration._jobs[job["operation_id"]]
    with pytest.raises(ValueError, match="取消"):
        await administration.job_action(job["operation_id"], "cancel")
    assert (await administration.store.get(job["operation_id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_remove_preview_cannot_erase_an_executing_correction(administration, monkeypatch):
    from astrbot_plugin_anamnesis.core.page_api import PluginPageApi
    from quart import Quart, g

    from tests.test_page_api import FakePlugin

    memory_id = await add(administration)
    preview = await administration.preview_correction(memory_id, "改为茶", actor="webui:a")
    operation_id = preview["operation_id"]
    token = await administration.confirm(operation_id, actor="webui:a")
    plugin = FakePlugin(memory_engine=administration.engine)
    plugin.config_manager = administration.config
    api = PluginPageApi(plugin)
    app = Quart(__name__)
    read, release = asyncio.Event(), asyncio.Event()
    owned = administration._owned

    async def pause_remove(*args, **kwargs):
        operation = await owned(*args, **kwargs)
        if asyncio.current_task().get_name() == "remove-preview":
            read.set()
            await release.wait()
        return operation

    monkeypatch.setattr(administration, "_owned", pause_remove)

    async def remove():
        async with app.test_request_context("/operations/remove", method="POST", json={"operation_id": operation_id}):
            g.username = "a"
            return await api.administration_request("remove")

    removing = asyncio.create_task(remove(), name="remove-preview")
    executing = None
    try:
        await asyncio.wait_for(read.wait(), 2)
        executing = asyncio.create_task(administration.execute_correction(operation_id, token["confirmation_token"], actor="webui:a"))
        # With the shared mutation guard this waits for removal; without it the
        # execution completes while removal still holds a stale preview row.
        try:
            await asyncio.wait_for(asyncio.shield(executing), 1)
        except TimeoutError:
            pass
        release.set()
        await asyncio.wait_for(asyncio.gather(removing, executing, return_exceptions=True), 5)
    finally:
        release.set()
        tasks = [task for task in (removing, executing) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    source = await administration.snapshot(memory_id)
    operation = await administration.store.get(operation_id)
    # Either removing the untouched preview wins, or execution wins and its
    # durable rollback journal survives. Losing both is never acceptable.
    assert source is not None or (operation is not None and operation["status"] == "completed")
