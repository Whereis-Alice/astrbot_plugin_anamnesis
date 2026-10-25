"""Runtime shutdown and background failures use real asyncio tasks."""

import asyncio
from unittest.mock import Mock

import pytest
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.event_handler import EventHandler
from astrbot_plugin_anamnesis.core.managers import memory_engine as engine_module
from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine


def _handler():
    return EventHandler(Mock(), ConfigManager(), Mock(), Mock(), Mock())


@pytest.mark.asyncio
async def test_shutdown_cancels_timed_out_storage_and_waits_for_cleanup():
    handler = _handler()
    handler._SHUTDOWN_TIMEOUT_SECONDS = 0.01
    started = asyncio.Event()
    cancelled = asyncio.Event()
    cleaned = asyncio.Event()

    async def store_forever():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            # A cancellation is not complete until storage has released resources.
            await asyncio.sleep(0)
            cleaned.set()

    task = asyncio.create_task(store_forever())
    handler._storage_tasks.add(task)
    handler._storage_sessions_inflight.add("session-a")
    await started.wait()
    try:
        await asyncio.wait_for(handler.shutdown(), timeout=1.0)

        assert cancelled.is_set()
        assert cleaned.is_set()
        assert task.cancelled()
        assert handler._shutting_down
        assert handler._memory_reflection._shutting_down
        assert not handler._storage_tasks
        assert not handler._storage_sessions_inflight
        # Repeated unload must be safe after resources have been released.
        await handler.shutdown()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_allows_inflight_storage_to_complete_without_cancellation():
    handler = _handler()
    handler._SHUTDOWN_TIMEOUT_SECONDS = 1.0
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def store_once():
        started.set()
        await release.wait()
        completed.set()
        return "stored"

    task = asyncio.create_task(store_once())
    handler._storage_tasks.add(task)
    await started.wait()
    shutdown = asyncio.create_task(handler.shutdown())
    try:
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        await asyncio.wait_for(shutdown, timeout=1.0)

        assert completed.is_set()
        assert task.result() == "stored"
        assert not task.cancelled()
        assert not handler._storage_tasks
    finally:
        release.set()
        await asyncio.gather(task, shutdown, return_exceptions=True)


@pytest.mark.asyncio
async def test_tracked_failure_is_logged_and_exception_retrieved(tmp_path, monkeypatch):
    engine = MemoryEngine(str(tmp_path / "runtime.db"), Mock())
    logger = Mock()
    monkeypatch.setattr(engine_module, "logger", logger)
    failure = RuntimeError("background write failed")
    original_callback = engine._on_background_task_done
    callback_finished = asyncio.Event()

    def observe_callback(task):
        original_callback(task)
        callback_finished.set()

    monkeypatch.setattr(engine, "_on_background_task_done", observe_callback)

    async def fail():
        raise failure

    task = engine._create_tracked_task(fail())
    exception_reader = Mock(wraps=task.exception)
    monkeypatch.setattr(task, "exception", exception_reader)
    # Wait for its callback, without awaiting/inspecting the failing task ourselves.
    await asyncio.wait_for(callback_finished.wait(), timeout=1.0)

    exception_reader.assert_called_once_with()
    assert task not in engine._pending_tasks
    logger.error.assert_called_once()
    args, kwargs = logger.error.call_args
    assert "后台任务失败" in args[0]
    assert args[1] is failure
    assert kwargs["exc_info"][1] is failure


@pytest.mark.asyncio
async def test_tracked_cancellation_is_removed_without_error_log(tmp_path, monkeypatch):
    engine = MemoryEngine(str(tmp_path / "runtime.db"), Mock())
    logger = Mock()
    monkeypatch.setattr(engine_module, "logger", logger)
    started = asyncio.Event()

    async def run():
        started.set()
        await asyncio.Event().wait()

    task = engine._create_tracked_task(run())
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert task.cancelled()
    assert task not in engine._pending_tasks
    logger.error.assert_not_called()
