"""Preparation/close races and fixed-query retry provenance."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.test_memory_administration import (
    administration as _administration_fixture,
)

administration = _administration_fixture


@pytest.mark.asyncio
async def test_close_cancels_import_preparation_before_it_creates_a_job(
    administration, monkeypatch
):
    entered, settled = asyncio.Event(), asyncio.Event()

    async def prepare(payload):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    monkeypatch.setattr(administration, "_parse_import", prepare)
    launch = Mock()
    monkeypatch.setattr(administration, "_launch_job", launch)
    creation = asyncio.create_task(
        administration.create_job("import", {"content": "text"})
    )
    await asyncio.wait_for(entered.wait(), 10)
    await administration.close_job_tasks()
    with pytest.raises(asyncio.CancelledError):
        await creation
    assert settled.is_set()
    assert not administration._jobs and not administration._job_preparations
    assert await administration.store.list() == []
    launch.assert_not_called()
    with pytest.raises(ValueError, match="关闭"):
        await administration.create_job("projection", {})


@pytest.mark.asyncio
async def test_close_after_queue_commit_keeps_interrupted_job_without_launch(
    administration, monkeypatch
):
    entered = asyncio.Event()
    update = administration.store.update

    async def pause_after_queue(operation_id, **kwargs):
        await update(operation_id, **kwargs)
        if kwargs.get("status") == "queued":
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(administration.store, "update", pause_after_queue)
    launch = Mock()
    monkeypatch.setattr(administration, "_launch_job", launch)
    creation = asyncio.create_task(administration.create_job("projection", {}))
    await asyncio.wait_for(entered.wait(), 10)
    await administration.close_job_tasks()
    with pytest.raises(asyncio.CancelledError):
        await creation
    records = await administration.store.list()
    assert len(records) == 1 and records[0]["status"] == "interrupted"
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_close_during_partial_import_preparation_removes_incomplete_manifest(
    administration, monkeypatch
):
    entered = asyncio.Event()
    put = administration.store.put_item

    async def first_chunk(operation_id, key, payload, **kwargs):
        await put(operation_id, key, payload, **kwargs)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(administration.store, "put_item", first_chunk)
    creation = asyncio.create_task(
        administration.create_job(
            "import", {"content": "a" * 3000, "format": "txt", "session_id": "scope"}
        )
    )
    await asyncio.wait_for(entered.wait(), 10)
    await administration.close_job_tasks()
    with pytest.raises(asyncio.CancelledError):
        await creation
    assert await administration.store.list() == []
    administration.processor._call_llm_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_preparation_is_cancelled_and_settled_before_close(
    administration, monkeypatch
):
    operation_id = await administration._create("projection", "webui", {}, "failed")
    entered = asyncio.Event()
    update = administration.store.update

    async def pause_after_queue(operation_id, **kwargs):
        await update(operation_id, **kwargs)
        if kwargs.get("status") == "queued":
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(administration.store, "update", pause_after_queue)
    launch = Mock()
    monkeypatch.setattr(administration, "_launch_job", launch)
    retry = asyncio.create_task(administration.job_action(operation_id, "retry"))
    await asyncio.wait_for(entered.wait(), 10)
    await administration.close_job_tasks()
    with pytest.raises(asyncio.CancelledError):
        await retry
    assert (await administration.store.get(operation_id))["status"] == "interrupted"
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_evaluation_retry_keeps_original_time_timezone_and_rejects_changed_config(
    administration, monkeypatch
):
    from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
    from astrbot_plugin_anamnesis.core.retrieval import retrieval_evaluation

    origins = []
    administration.processor.context = SimpleNamespace(
        get_config=lambda origin: (
            origins.append(origin) or {"timezone": "America/New_York"}
        )
    )
    evaluate = AsyncMock(side_effect=RuntimeError("first attempt failed"))
    monkeypatch.setattr(retrieval_evaluation, "evaluate_queries", evaluate)
    job = await administration.create_job(
        "evaluation",
        {
            "cases": [{"query": "上周咖啡", "expected_ids": [1]}],
            "session_id": "scope",
            "evaluation_reference": {"reference_time": "forged", "timezone": "UTC"},
        },
    )
    operation_id = job["operation_id"]
    await administration._jobs[operation_id]
    stored = await administration.store.get(operation_id)
    reference = stored["payload"]["evaluation_reference"]
    assert stored["status"] == "failed" and reference["timezone"] == "America/New_York"
    assert datetime.fromisoformat(reference["reference_time"]).tzinfo is not None
    first_now = evaluate.await_args.kwargs["now"]
    evaluate.side_effect = None
    evaluate.return_value = {"summary": {}, "cases": []}
    await administration.job_action(operation_id, "retry")
    await administration._jobs[operation_id]
    assert evaluate.await_args.kwargs["now"] == first_now
    assert evaluate.await_args.kwargs["timezone_name"] == "America/New_York"
    assert all(origin == "scope" for origin in origins)
    await administration.store.update(operation_id, status="failed")
    administration.config = ConfigManager({"recall_engine": {"top_k": 7}})
    with pytest.raises(ValueError, match="配置"):
        await administration.job_action(operation_id, "retry")
    assert evaluate.await_count == 2


@pytest.mark.asyncio
async def test_evaluation_rejects_missing_labels_before_creating_job(administration):
    with pytest.raises(ValueError, match="expected_ids"):
        await administration.create_job("evaluation", {"cases": [{"query": "coffee"}]})
    assert await administration.store.list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["import", "projection"])
async def test_close_waits_for_manifest_commit_then_cleans_up(
    administration, monkeypatch, kind
):
    entered, release = asyncio.Event(), asyncio.Event()
    create = administration._create

    async def pause_after_create(*args, **kwargs):
        operation_id = await create(*args, **kwargs)
        entered.set()
        await release.wait()
        return operation_id

    monkeypatch.setattr(administration, "_create", pause_after_create)
    launch = Mock()
    monkeypatch.setattr(administration, "_launch_job", launch)
    payload = (
        {"content": "a", "format": "txt", "session_id": "scope"}
        if kind == "import"
        else {}
    )
    creation = asyncio.create_task(administration.create_job(kind, payload))
    await asyncio.wait_for(entered.wait(), 10)
    closing = asyncio.create_task(administration.close_job_tasks())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not closing.done()
    # Cancel a second time while the independent commit task is settling.
    creation.cancel()
    release.set()
    await closing
    with pytest.raises(asyncio.CancelledError):
        await creation
    records = await administration.store.list()
    if kind == "import":
        assert records == []
    else:
        assert len(records) == 1 and records[0]["status"] == "interrupted"
    launch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", [False, True])
async def test_repeated_worker_cancel_finishes_status_commit(
    administration, monkeypatch, closing
):
    running, committing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    update = administration.store.update

    async def repair(**kwargs):
        running.set()
        await asyncio.Event().wait()

    async def pause_status(operation_id, **kwargs):
        if kwargs.get("status") in ("cancelled", "interrupted"):
            committing.set()
            await release.wait()
        await update(operation_id, **kwargs)

    monkeypatch.setattr(administration.projection, "repair", repair)
    monkeypatch.setattr(administration.store, "update", pause_status)
    result = await administration.create_job("projection", {})
    operation_id = result["operation_id"]
    worker = administration._jobs[operation_id]
    await asyncio.wait_for(running.wait(), 10)
    administration._closing = closing
    worker.cancel()
    await asyncio.wait_for(committing.wait(), 10)
    worker.cancel()
    await asyncio.sleep(0)
    worker.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await worker
    expected = "interrupted" if closing else "cancelled"
    assert (await administration.store.get(operation_id))["status"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["remove", "retry", "cancel"])
async def test_preparing_job_actions_keep_unfinished_chunks(administration, action):
    operation_id = await administration._create(
        "import", "webui", {"chunk_count": 2}, "preparing"
    )
    await administration.store.put_item(
        operation_id, "000000", {"content": "first chunk"}
    )
    with pytest.raises(ValueError, match="准备"):
        await administration.job_action(operation_id, action)
    assert (await administration.store.get(operation_id))["status"] == "preparing"
    assert len(await administration.store.items(operation_id)) == 1


@pytest.mark.asyncio
async def test_concurrent_retries_launch_a_job_once(administration, monkeypatch):
    operation_id = await administration._create("projection", "webui", {}, "failed")
    entered, release = asyncio.Event(), asyncio.Event()
    update = administration.store.update

    async def pause_after_queue(operation_id, **kwargs):
        await update(operation_id, **kwargs)
        if kwargs.get("status") == "queued":
            entered.set()
            await release.wait()

    monkeypatch.setattr(administration.store, "update", pause_after_queue)
    launch = Mock()
    monkeypatch.setattr(administration, "_launch_job", launch)
    first = asyncio.create_task(administration.job_action(operation_id, "retry"))
    await asyncio.wait_for(entered.wait(), 10)
    second = asyncio.create_task(administration.job_action(operation_id, "retry"))
    await asyncio.sleep(0)
    release.set()
    await first
    with pytest.raises(ValueError, match="不能重试"):
        await second
    launch.assert_called_once_with(operation_id)


@pytest.mark.asyncio
async def test_restart_rejects_partial_import_manifest(administration):
    from astrbot_plugin_anamnesis.core.managers.memory_administration import (
        MemoryAdministration,
    )

    operation_id = await administration._create(
        "import", "webui", {"chunk_count": 2}, "preparing"
    )
    await administration.store.put_item(
        operation_id, "000000", {"content": "first chunk"}
    )
    fresh = MemoryAdministration(
        administration.engine, administration.config, administration.processor
    )
    await fresh.initialize()
    assert (await fresh.store.get(operation_id))["status"] == "interrupted"
    with pytest.raises(ValueError, match="准备不完整"):
        await fresh.job_action(operation_id, "retry")
    # The worker independently validates persisted state too.
    await fresh.store.update(operation_id, status="queued")
    await fresh._run_job(operation_id)
    record = await fresh.store.get(operation_id)
    assert record["status"] == "failed" and "准备不完整" in record["result"]["error"]
    assert len(await fresh.store.items(operation_id)) == 1
    administration.processor._call_llm_with_retry.assert_not_awaited()
