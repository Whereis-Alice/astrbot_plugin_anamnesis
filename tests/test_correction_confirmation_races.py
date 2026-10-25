"""Confirmation cannot return a token for a preview that another action consumed."""

import asyncio

import pytest

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["remove", "execute"])
async def test_confirmation_rejects_preview_removed_or_executed_while_reading(
    administration, monkeypatch, winner
):
    memory_id = await add(administration)
    preview = await administration.preview_correction(
        memory_id, "改为茶", actor="webui:a", new_content="喜欢茶"
    )
    operation_id = preview["operation_id"]
    prior = await administration.confirm(operation_id, actor="webui:a")
    snapshot = administration.snapshot
    read, release = asyncio.Event(), asyncio.Event()
    pause = [True]

    async def observed(*args, **kwargs):
        current = await snapshot(*args, **kwargs)
        if pause[0]:
            pause[0] = False
            read.set()
            await release.wait()
        return current

    monkeypatch.setattr(administration, "snapshot", observed)
    confirmation = asyncio.create_task(
        administration.confirm(operation_id, actor="webui:a")
    )
    try:
        await asyncio.wait_for(read.wait(), 2)
        if winner == "remove":
            assert (
                await administration.remove_operation(operation_id, actor="webui:a")
            )["removed"]
        else:
            completed = await administration.execute_correction(
                operation_id, prior["confirmation_token"], actor="webui:a"
            )
        release.set()
        with pytest.raises(ValueError, match="已执行或已作废"):
            await asyncio.wait_for(confirmation, 5)
        current = await administration.store.get(operation_id)
        if winner == "remove":
            assert current is None
            assert (await administration.snapshot(memory_id))["text"] == "喜欢咖啡"
        else:
            assert current["status"] == "completed"
            assert await administration.snapshot(memory_id) is None
            assert (await administration.snapshot(completed["new_memory_id"]))[
                "text"
            ] == "喜欢茶"
    finally:
        release.set()
        if not confirmation.done():
            confirmation.cancel()
        await asyncio.gather(confirmation, return_exceptions=True)


@pytest.mark.asyncio
async def test_sequential_confirmation_rotation_and_single_use_still_work(
    administration,
):
    memory_id = await add(administration)
    preview = await administration.preview_correction(
        memory_id, "改为茶", actor="webui:a", new_content="喜欢茶"
    )
    operation_id = preview["operation_id"]
    first = await administration.confirm(operation_id, actor="webui:a")
    second = await administration.confirm(operation_id, actor="webui:a")
    assert first["confirmation_token"] != second["confirmation_token"]
    with pytest.raises(ValueError, match="确认凭据"):
        await administration.execute_correction(
            operation_id, first["confirmation_token"], actor="webui:a"
        )
    assert (await administration.snapshot(memory_id))["text"] == "喜欢咖啡"
    completed = await administration.execute_correction(
        operation_id, second["confirmation_token"], actor="webui:a"
    )
    with pytest.raises(ValueError, match="确认凭据"):
        await administration.execute_correction(
            operation_id, second["confirmation_token"], actor="webui:a"
        )
    assert (await administration.snapshot(completed["new_memory_id"]))[
        "text"
    ] == "喜欢茶"
