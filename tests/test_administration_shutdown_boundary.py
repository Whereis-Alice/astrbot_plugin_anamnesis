"""Administration preparation cannot outlive service shutdown."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]


@pytest.mark.asyncio
async def test_shutdown_cancels_and_settles_provider_preview_without_holding_mutation_lock(
    administration,
):
    memory_id = await add(administration)
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def provider(*args):
        entered.set()
        try:
            await release.wait()
            return '{"content":"喜欢茶","key_facts":["喜欢茶"]}'
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    administration.processor._call_llm_with_retry = AsyncMock(side_effect=provider)
    pending = asyncio.create_task(
        administration.preview_correction(memory_id, "改为茶", actor="webui:a")
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(
            administration.engine.update_memory(memory_id, {"importance": 0.6}), 2
        )
        await asyncio.wait_for(administration.engine.quiesce_background_writes(), 2)
        assert cleaned.is_set() and pending.done()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await administration.store.list(kind="correction") == []
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_settles_confirmation_preparation(administration, monkeypatch):
    memory_id = await add(administration)
    preview = await administration.preview_correction(
        memory_id, "改为茶", new_content="喜欢茶", actor="webui:a"
    )
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    get = administration.store.get

    async def slow_get(operation_id, **kwargs):
        entered.set()
        try:
            await release.wait()
            return await get(operation_id, **kwargs)
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    monkeypatch.setattr(administration.store, "get", slow_get)
    pending = asyncio.create_task(
        administration.confirm(preview["operation_id"], actor="webui:a")
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(administration.close(), 2)
        assert cleaned.is_set() and pending.done()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (await get(preview["operation_id"]))["token_hash"] is None
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["close", "caller"])
async def test_committed_but_undelivered_preview_is_removed_before_stop_finishes(
    administration, monkeypatch, stop
):
    memory_id = await add(administration)
    create = administration.store.create
    committed, release = asyncio.Event(), asyncio.Event()

    async def commit_before_return(*args, **kwargs):
        await create(*args, **kwargs)
        committed.set()
        await release.wait()

    monkeypatch.setattr(administration.store, "create", commit_before_return)
    pending = asyncio.create_task(
        administration.preview_correction(
            memory_id, "改为茶", new_content="喜欢茶", actor="webui:a"
        )
    )
    closing = None
    try:
        await asyncio.wait_for(committed.wait(), 2)
        if stop == "close":
            closing = asyncio.create_task(administration.close())
        else:
            pending.cancel()
        await asyncio.sleep(0)
        release.set()
        if closing is not None:
            await asyncio.wait_for(closing, 2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert await administration.store.list(kind="correction") == []
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(
            *(task for task in (pending, closing) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", ["confirmed", "executing"])
async def test_cancelled_preview_creator_keeps_another_requests_claimed_log(
    administration, monkeypatch, claimed
):
    memory_id = await add(administration)
    create = administration.store.create
    replace = administration.engine.replace_memory
    committed, release, executing, finish_execution = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def commit_before_return(*args, **kwargs):
        await create(*args, **kwargs)
        committed.set()
        await release.wait()

    async def slow_replace(*args, **kwargs):
        executing.set()
        await finish_execution.wait()
        return await replace(*args, **kwargs)

    monkeypatch.setattr(administration.store, "create", commit_before_return)
    monkeypatch.setattr(administration.engine, "replace_memory", slow_replace)
    creator = asyncio.create_task(
        administration.preview_correction(
            memory_id, "改为茶", new_content="喜欢茶", actor="webui:a"
        )
    )
    executor = None
    try:
        await asyncio.wait_for(committed.wait(), 2)
        operation_id = (await administration.store.list(kind="correction"))[0]["id"]
        confirmation = await administration.confirm(operation_id, actor="webui:a")
        if claimed == "executing":
            executor = asyncio.create_task(
                administration.execute_correction(
                    operation_id, confirmation["confirmation_token"], actor="webui:a"
                )
            )
            await asyncio.wait_for(executing.wait(), 2)
        creator.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(creator, 2)
        operation = await administration.store.get(operation_id)
        assert operation is not None
        if claimed == "confirmed":
            assert operation["token_hash"] is not None
        if executor is not None:
            assert operation["status"] == "executing"
            finish_execution.set()
            await asyncio.wait_for(executor, 2)
            assert (await administration.store.get(operation_id))[
                "status"
            ] == "completed"
    finally:
        release.set()
        finish_execution.set()
        await asyncio.gather(
            *(task for task in (creator, executor) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("preview_correction", (1, "更正"), {"new_content": "更正正文"}),
        ("confirm", ("missing",), {}),
        ("delete", ([1],), {}),
        ("restore", ("missing",), {}),
        ("execute_correction", ("missing", "token"), {}),
        ("rollback_correction", ("missing",), {}),
        ("remove_operation", ("missing",), {}),
        ("_create", ("correction", "webui", {}), {}),
    ],
)
async def test_closed_administration_rejects_new_mutation_preparations(
    administration, method, args, kwargs
):
    await administration.close()
    with pytest.raises(ValueError, match="关闭"):
        await getattr(administration, method)(*args, **kwargs)
