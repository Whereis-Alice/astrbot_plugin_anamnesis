"""Queued rollback must authorize the current retained correction journal."""

import asyncio
import time

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_mutation import memory_mutation_guard

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]


async def completed_correction(service, *, actor="webui:a"):
    original_id = await add(service)
    preview = await service.preview_correction(
        original_id, "改为茶", actor=actor, new_content="喜欢茶"
    )
    operation_id = preview["operation_id"]
    token = await service.confirm(operation_id, actor=actor)
    corrected = await service.execute_correction(
        operation_id, token["confirmation_token"], actor=actor
    )
    assert await service.snapshot(original_id) is None
    return operation_id, corrected["new_memory_id"]


async def document_ids(service):
    async with service.engine.db_connection.execute(
        "SELECT id FROM documents ORDER BY id"
    ) as cursor:
        return [row[0] for row in await cursor.fetchall()]


async def finish_tasks(tasks, release):
    release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_rollback_already_expired_refuses_without_writing(
    administration, monkeypatch
):
    operation_id, corrected_id = await completed_correction(administration)
    operation = await administration.store.get(operation_id)
    snapshot = await administration.snapshot(corrected_id)
    monkeypatch.setattr(time, "time", lambda: operation["expires_at"] + 1)
    with pytest.raises(ValueError, match="期限"):
        await administration.rollback_correction(operation_id, actor="webui:a")
    assert await document_ids(administration) == [corrected_id]
    assert await administration.snapshot(corrected_id) == snapshot
    assert (await administration.store.get(operation_id))["status"] == "completed"


@pytest.mark.asyncio
async def test_rollback_waiting_for_writer_rechecks_retention(
    administration, monkeypatch
):
    operation_id, corrected_id = await completed_correction(administration)
    operation = await administration.store.get(operation_id)
    snapshot = await administration.snapshot(corrected_id)
    clock = [operation["expires_at"] - 1]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    locked, release, checked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    owned = administration._owned

    async def observed(*args, **kwargs):
        result = await owned(*args, **kwargs)
        if asyncio.current_task().get_name() == "retention-rollback":
            checked.set()
        return result

    monkeypatch.setattr(administration, "_owned", observed)

    async def holder():
        async with memory_mutation_guard(administration.engine):
            locked.set()
            await release.wait()

    holding = asyncio.create_task(holder())
    tasks = [holding]
    try:
        await asyncio.wait_for(locked.wait(), 2)
        rollback = asyncio.create_task(
            administration.rollback_correction(operation_id, actor="webui:a"),
            name="retention-rollback",
        )
        tasks.append(rollback)
        await asyncio.wait_for(checked.wait(), 2)
        assert not rollback.done()
        clock[0] = operation["expires_at"] + 1
        release.set()
        with pytest.raises(ValueError, match="期限"):
            await asyncio.wait_for(rollback, 5)
        assert await document_ids(administration) == [corrected_id]
        assert await administration.snapshot(corrected_id) == snapshot
        assert (await administration.store.get(operation_id))["status"] == "completed"
    finally:
        await finish_tasks(tasks, release)


@pytest.mark.asyncio
async def test_expired_journal_cleanup_wins_over_queued_rollback(
    administration, monkeypatch
):
    operation_id, corrected_id = await completed_correction(administration)
    operation = await administration.store.get(operation_id)
    snapshot = await administration.snapshot(corrected_id)
    clock = [operation["expires_at"] - 1]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    removal_read, release, rollback_read = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    owned = administration._owned

    async def observed(*args, **kwargs):
        result = await owned(*args, **kwargs)
        task_name = asyncio.current_task().get_name()
        if task_name == "retention-remove":
            removal_read.set()
            await release.wait()
        elif task_name == "retention-rollback":
            rollback_read.set()
        return result

    monkeypatch.setattr(administration, "_owned", observed)
    removing = asyncio.create_task(
        administration.remove_operation(operation_id, actor="webui:a"),
        name="retention-remove",
    )
    tasks = [removing]
    try:
        await asyncio.wait_for(removal_read.wait(), 2)
        rollback = asyncio.create_task(
            administration.rollback_correction(operation_id, actor="webui:a"),
            name="retention-rollback",
        )
        tasks.append(rollback)
        await asyncio.wait_for(rollback_read.wait(), 2)
        assert not rollback.done()
        clock[0] = operation["expires_at"] + 1
        release.set()
        assert (await asyncio.wait_for(removing, 5))["removed"]
        with pytest.raises(ValueError, match="不存在"):
            await asyncio.wait_for(rollback, 5)
        assert await administration.store.get(operation_id) is None
        assert await document_ids(administration) == [corrected_id]
        assert await administration.snapshot(corrected_id) == snapshot
    finally:
        await finish_tasks(tasks, release)


@pytest.mark.asyncio
async def test_two_queued_rollbacks_use_latest_completed_state(
    administration, monkeypatch
):
    operation_id, corrected_id = await completed_correction(administration)
    locked, release, first_read, second_read = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    owned = administration._owned

    async def observed(*args, **kwargs):
        result = await owned(*args, **kwargs)
        task_name = asyncio.current_task().get_name()
        if task_name == "rollback-first":
            first_read.set()
        elif task_name == "rollback-second":
            second_read.set()
        return result

    monkeypatch.setattr(administration, "_owned", observed)

    async def holder():
        async with memory_mutation_guard(administration.engine):
            locked.set()
            await release.wait()

    holding = asyncio.create_task(holder())
    tasks = [holding]
    try:
        await asyncio.wait_for(locked.wait(), 2)
        first = asyncio.create_task(
            administration.rollback_correction(operation_id, actor="webui:a"),
            name="rollback-first",
        )
        tasks.append(first)
        await asyncio.wait_for(first_read.wait(), 2)
        second = asyncio.create_task(
            administration.rollback_correction(operation_id, actor="webui:a"),
            name="rollback-second",
        )
        tasks.append(second)
        await asyncio.wait_for(second_read.wait(), 2)
        assert not first.done() and not second.done()
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert results[0] == results[1]
        restored_id = results[0]["memory_id"]
        assert await document_ids(administration) == [restored_id]
        assert (await administration.snapshot(restored_id))["text"] == "喜欢咖啡"
        assert await administration.snapshot(corrected_id) is None
        assert (await administration.store.get(operation_id))["status"] == "rolled_back"
    finally:
        await finish_tasks(tasks, release)


@pytest.mark.asyncio
async def test_valid_owner_rollback_is_idempotent_and_foreign_owner_is_refused(
    administration,
):
    owner = "tool:qq:GroupMessage:1:alice"
    operation_id, corrected_id = await completed_correction(administration, actor=owner)
    snapshot = await administration.snapshot(corrected_id)
    with pytest.raises(ValueError, match="不属于"):
        await administration.rollback_correction(
            operation_id, actor="tool:qq:GroupMessage:1:bob"
        )
    assert await document_ids(administration) == [corrected_id]
    assert await administration.snapshot(corrected_id) == snapshot
    restored = await administration.rollback_correction(operation_id, actor=owner)
    assert (
        await administration.rollback_correction(operation_id, actor=owner) == restored
    )
    assert await document_ids(administration) == [restored["memory_id"]]
    assert (await administration.snapshot(restored["memory_id"]))["text"] == "喜欢咖啡"
