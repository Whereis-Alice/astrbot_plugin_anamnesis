"""Web edits append history against the latest serialized memory snapshot."""

import asyncio

import pytest
from astrbot_plugin_anamnesis.core.page_api_modules.memory_handler import MemoryHandler
from astrbot_plugin_anamnesis.core.page_api_modules.utils import PageApiUtils
from quart import Quart

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]


async def edit(app, handler, engine, memory_id, field, value):
    async with app.test_request_context(
        "/memories/update",
        method="POST",
        json={
            "memory_id": memory_id,
            "field": field,
            "value": value,
            "reason": f"{field} change",
        },
    ):
        return await handler.update_memory(engine)


def pause_first_read(handler, monkeypatch):
    read, release = asyncio.Event(), asyncio.Event()
    original = handler._get_memory_record
    calls = []

    async def paused(*args):
        record = await original(*args)
        calls.append(record)
        if len(calls) == 1:
            read.set()
            await release.wait()
        return record

    monkeypatch.setattr(handler, "_get_memory_record", paused)
    return read, release, calls


async def finish_tasks(tasks, release):
    release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_field", ["importance", "type"])
async def test_disjoint_edits_keep_both_values_and_audit_entries(
    administration,
    monkeypatch,
    first_field,
):
    memory_id = await add(administration)
    handler, app = MemoryHandler(PageApiUtils()), Quart(__name__)
    read, release, calls = pause_first_read(handler, monkeypatch)
    values = {"importance": 0.8, "type": "PREFERENCE"}
    second_field = "type" if first_field == "importance" else "importance"
    queued = asyncio.Event()

    async def second_edit():
        queued.set()
        return await edit(
            app,
            handler,
            administration.engine,
            memory_id,
            second_field,
            values[second_field],
        )

    first = asyncio.create_task(
        edit(
            app,
            handler,
            administration.engine,
            memory_id,
            first_field,
            values[first_field],
        )
    )
    tasks = [first]
    try:
        await asyncio.wait_for(read.wait(), 5)
        second = asyncio.create_task(second_edit())
        tasks.append(second)
        await asyncio.wait_for(queued.wait(), 5)
        assert len(calls) == 1 and not second.done()
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert all(result["status"] == "ok" for result in results)
        current = await administration.snapshot(memory_id)
        assert current["metadata"]["importance"] == 0.8
        assert current["metadata"]["memory_type"] == "PREFERENCE"
        history = current["metadata"]["update_history"]
        assert [item["field"] for item in history] == [first_field, second_field]
        assert [item["reason"] for item in history] == [
            f"{first_field} change",
            f"{second_field} change",
        ]
    finally:
        await finish_tasks(tasks, release)


@pytest.mark.asyncio
async def test_same_field_edit_records_the_latest_old_value(
    administration, monkeypatch
):
    memory_id = await add(administration)
    handler, app = MemoryHandler(PageApiUtils()), Quart(__name__)
    read, release, calls = pause_first_read(handler, monkeypatch)
    queued = asyncio.Event()

    async def second_edit():
        queued.set()
        return await edit(
            app, handler, administration.engine, memory_id, "importance", 0.9
        )

    first = asyncio.create_task(
        edit(app, handler, administration.engine, memory_id, "importance", 0.8)
    )
    tasks = [first]
    try:
        await asyncio.wait_for(read.wait(), 5)
        second = asyncio.create_task(second_edit())
        tasks.append(second)
        await asyncio.wait_for(queued.wait(), 5)
        assert len(calls) == 1 and not second.done()
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert all(result["status"] == "ok" for result in results)
        current = await administration.snapshot(memory_id)
        assert current["metadata"]["importance"] == 0.9
        history = current["metadata"]["update_history"]
        assert [(item["old_value"], item["new_value"]) for item in history] == [
            (5.0, 8.0),
            (8.0, 9.0),
        ]
    finally:
        await finish_tasks(tasks, release)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("importance", 0.8), ("content", "喜欢书")])
async def test_edit_wins_over_a_queued_stale_correction(
    administration,
    monkeypatch,
    field,
    value,
):
    memory_id = await add(administration)
    preview = await administration.preview_correction(
        memory_id, "改为茶", actor="webui:a", new_content="喜欢茶"
    )
    token = await administration.confirm(preview["operation_id"], actor="webui:a")
    handler, app = MemoryHandler(PageApiUtils()), Quart(__name__)
    read, release, _ = pause_first_read(handler, monkeypatch)
    checked = asyncio.Event()
    owned = administration._owned

    async def observe_owned(*args, **kwargs):
        result = await owned(*args, **kwargs)
        if asyncio.current_task().get_name() == "queued-correction":
            checked.set()
        return result

    monkeypatch.setattr(administration, "_owned", observe_owned)
    updating = asyncio.create_task(
        edit(app, handler, administration.engine, memory_id, field, value)
    )
    tasks = [updating]
    try:
        await asyncio.wait_for(read.wait(), 5)
        correcting = asyncio.create_task(
            administration.execute_correction(
                preview["operation_id"],
                token["confirmation_token"],
                actor="webui:a",
            ),
            name="queued-correction",
        )
        tasks.append(correcting)
        await asyncio.wait_for(checked.wait(), 5)
        assert not correcting.done()
        release.set()
        updated = await asyncio.wait_for(updating, 5)
        assert updated["status"] == "ok"
        with pytest.raises(ValueError, match="变化"):
            await asyncio.wait_for(correcting, 5)
        current_id = updated["data"].get("new_memory_id", memory_id)
        current = await administration.snapshot(current_id)
        if field == "content":
            assert (
                current["text"] == value
                and await administration.snapshot(memory_id) is None
            )
        else:
            assert current["metadata"]["importance"] == value
        assert current["metadata"]["update_history"][-1]["field"] == field
        assert (await administration.store.get(preview["operation_id"]))[
            "status"
        ] == "stale"
    finally:
        await finish_tasks(tasks, release)


@pytest.mark.asyncio
async def test_cancelled_edit_releases_writer_without_a_phantom_history_entry(
    administration,
    monkeypatch,
):
    memory_id = await add(administration)
    handler, app = MemoryHandler(PageApiUtils()), Quart(__name__)
    read, release, _ = pause_first_read(handler, monkeypatch)
    updating = asyncio.create_task(
        edit(app, handler, administration.engine, memory_id, "importance", 0.8)
    )
    try:
        await asyncio.wait_for(read.wait(), 5)
        updating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(updating, 5)
        assert (await administration.snapshot(memory_id))["metadata"].get(
            "update_history", []
        ) == []
        result = await asyncio.wait_for(
            edit(app, handler, administration.engine, memory_id, "type", "PREFERENCE"),
            5,
        )
        assert result["status"] == "ok"
        history = (await administration.snapshot(memory_id))["metadata"][
            "update_history"
        ]
        assert [item["field"] for item in history] == ["type"]
    finally:
        await finish_tasks([updating], release)
