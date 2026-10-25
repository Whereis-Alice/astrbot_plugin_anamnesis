"""Feature changes reject queued work without interrupting admitted commits."""

import asyncio
import json

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_mutation import memory_mutation_guard

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]
ACTIONS = ("delete", "restore", "purge", "preview", "execute", "rollback")


async def operation_call(service, action, memory_id, operation_id, token):
    if action == "delete":
        return await service.delete([memory_id], actor="webui:a")
    if action in ("restore", "purge"):
        return await service.restore(
            operation_id, actor="webui:a", purge=action == "purge"
        )
    if action == "preview":
        return await service.preview_correction(memory_id, "改为茶", actor="webui:a")
    if action == "execute":
        return await service.execute_correction(operation_id, token, actor="webui:a")
    return await service.rollback_correction(operation_id, actor="webui:a")


async def setup_operation(service, action):
    memory_id = await add(service)
    operation_id = token = None
    feature = "recovery" if action in ("delete", "restore", "purge") else "correction"
    if action in ("restore", "purge"):
        operation_id = (await service.delete([memory_id], actor="webui:a"))[
            "operation_id"
        ]
    elif action in ("execute", "rollback"):
        planned = await service.preview_correction(
            memory_id, "改为茶", actor="webui:a", new_content="喜欢茶"
        )
        operation_id = planned["operation_id"]
        token = (await service.confirm(operation_id, actor="webui:a"))[
            "confirmation_token"
        ]
        if action == "rollback":
            memory_id = (
                await service.execute_correction(operation_id, token, actor="webui:a")
            )["new_memory_id"]
    service.processor._call_llm_with_retry.reset_mock()
    return memory_id, operation_id, token, feature


def set_feature(service, feature, enabled):
    service.config._config.setdefault("memory_administration", {})[
        f"{feature}_enabled"
    ] = enabled


async def settle(tasks, release):
    release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("enabled", [False, True])
async def test_feature_is_rechecked_after_waiting_for_document_writer(
    administration, monkeypatch, action, enabled
):
    memory_id, operation_id, token, feature = await setup_operation(
        administration, action
    )
    before = await administration.snapshot(memory_id)
    saved_operations = await administration.store.list()
    checked, locked, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    require = administration.require

    def observed(feature_name):
        require(feature_name)
        if feature_name == feature:
            checked.set()

    monkeypatch.setattr(administration, "require", observed)

    async def writer():
        async with memory_mutation_guard(administration.engine):
            locked.set()
            await release.wait()

    writing = asyncio.create_task(writer())
    tasks = [writing]
    try:
        await asyncio.wait_for(locked.wait(), 5)
        pending = asyncio.create_task(
            operation_call(administration, action, memory_id, operation_id, token)
        )
        tasks.append(pending)
        await asyncio.wait_for(checked.wait(), 5)
        assert not pending.done()
        set_feature(administration, feature, enabled)
        release.set()
        if enabled:
            result = await asyncio.wait_for(pending, 5)
        else:
            with pytest.raises(ValueError, match="功能已关闭"):
                await asyncio.wait_for(pending, 5)
            assert await administration.snapshot(memory_id) == before
            assert await administration.store.list() == saved_operations
            administration.processor._call_llm_with_retry.assert_not_awaited()
            # Rejection did not consume a token or strand a recovery journal.
            set_feature(administration, feature, True)
            result = await operation_call(
                administration, action, memory_id, operation_id, token
            )
        assert result
        if action == "delete":
            assert result["deleted_ids"] == [memory_id]
        elif action in ("restore", "purge"):
            assert result["ids"] == [memory_id] and result["conflicts"] == []
        elif action == "preview":
            assert result["requires_webui_confirmation"]
        else:
            assert (await administration.store.get(operation_id))["status"] == (
                "completed" if action == "execute" else "rolled_back"
            )
    finally:
        await settle(tasks, release)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,phase", [("delete", "prepared"), ("execute", "prepared_commit")]
)
async def test_disabling_feature_after_commit_admission_still_settles_journal(
    administration, monkeypatch, action, phase
):
    memory_id, operation_id, token, feature = await setup_operation(
        administration, action
    )
    committed, release = asyncio.Event(), asyncio.Event()
    original = (
        administration.store.put_item
        if action == "delete"
        else administration.store.update
    )
    paused = False

    async def observe(*args, **kwargs):
        nonlocal paused
        result = await original(*args, **kwargs)
        if not paused and kwargs.get("status") == phase:
            paused = True
            committed.set()
            await release.wait()
        return result

    monkeypatch.setattr(
        administration.store, "put_item" if action == "delete" else "update", observe
    )
    pending = asyncio.create_task(
        operation_call(administration, action, memory_id, operation_id, token)
    )
    try:
        await asyncio.wait_for(committed.wait(), 5)
        set_feature(administration, feature, False)
        release.set()
        result = await asyncio.wait_for(pending, 5)
        saved_id = result["operation_id"] if action == "delete" else operation_id
        assert (await administration.store.get(saved_id))["status"] == "completed"
        set_feature(administration, feature, True)
        if action == "delete":
            assert (await administration.restore(saved_id, actor="webui:a"))["ids"] == [
                memory_id
            ]
        else:
            restored = await administration.rollback_correction(
                saved_id, actor="webui:a"
            )
            assert (await administration.snapshot(restored["memory_id"]))[
                "text"
            ] == "喜欢咖啡"
    finally:
        await settle([pending], release)


def job_payload(kind, memory_id):
    if kind == "import":
        return {"format": "json", "content": json.dumps([{"content": "imported fact"}])}
    if kind == "episode":
        return {"memory_ids": [memory_id], "use_llm": False}
    if kind == "evaluation":
        return {"cases": [{"query": "咖啡", "expected_ids": [memory_id]}]}
    return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["import", "episode", "evaluation", "projection"])
async def test_disabled_queued_job_does_not_start_and_can_retry_after_reenable(
    administration, kind
):
    memory_id = await add(administration)
    gate = asyncio.Semaphore(0)
    administration._job_semaphore = gate
    job = await administration.create_job(
        kind, job_payload(kind, memory_id), actor="webui:a"
    )
    operation_id = job["operation_id"]
    task = administration._jobs[operation_id]
    try:
        set_feature(administration, kind, False)
        gate.release()
        await asyncio.wait_for(task, 5)
        failed = await administration.store.get(operation_id)
        assert (
            failed["status"] == "failed" and "功能已关闭" in failed["result"]["error"]
        )
        administration.processor._call_llm_with_retry.assert_not_awaited()
        assert (await administration.snapshot(memory_id))["text"] == "喜欢咖啡"
        if kind == "import":
            assert len(await administration.engine.get_memory_transfer_records()) == 1
        elif kind == "episode":
            assert await administration.list_episodes() == []
        set_feature(administration, kind, True)
        await administration.job_action(operation_id, "retry", actor="webui:a")
        retrying = administration._jobs[operation_id]
        await asyncio.wait_for(retrying, 5)
        assert (await administration.store.get(operation_id))["status"] == "completed"
    finally:
        if not task.done():
            gate.release()
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_import_rechecks_feature_after_waiting_for_import_writer(
    administration, monkeypatch, enabled
):
    engine = administration.engine
    engine._memory_import_lock = asyncio.Lock()
    await engine._memory_import_lock.acquire()
    started = asyncio.Event()
    update = administration.store.update

    async def observed_update(*args, **kwargs):
        result = await update(*args, **kwargs)
        if kwargs.get("status") == "running":
            started.set()
        return result

    monkeypatch.setattr(administration.store, "update", observed_update)
    job = await administration.create_job(
        "import", job_payload("import", 0), actor="webui:a"
    )
    operation_id = job["operation_id"]
    task = administration._jobs[operation_id]
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert not task.done()
        set_feature(administration, "import", enabled)
        engine._memory_import_lock.release()
        await asyncio.wait_for(task, 5)
        operation = await administration.store.get(operation_id)
        if enabled:
            assert operation["status"] == "completed"
        else:
            assert (
                operation["status"] == "failed"
                and "功能已关闭" in operation["result"]["error"]
            )
            assert await engine.get_memory_transfer_records() == []
            administration.processor._call_llm_with_retry.assert_not_awaited()
            set_feature(administration, "import", True)
            await administration.job_action(operation_id, "retry", actor="webui:a")
            await asyncio.wait_for(administration._jobs[operation_id], 5)
            assert (await administration.store.get(operation_id))[
                "status"
            ] == "completed"
        assert len(await engine.get_memory_transfer_records()) == 1
    finally:
        if engine._memory_import_lock.locked():
            engine._memory_import_lock.release()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
