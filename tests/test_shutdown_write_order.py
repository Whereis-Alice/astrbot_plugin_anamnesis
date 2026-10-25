"""Stop plugin-owned writes before closing their index persisters."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_anamnesis.core.managers.memory_mutation import memory_mutation_guard
from astrbot_plugin_anamnesis.core.plugin_initializer_finalize import (
    InitializerFinalizeMixin,
)


@pytest.mark.asyncio
async def test_index_persister_close_waits_for_admin_lifecycle_and_mutation_cleanup(
    tmp_path,
):
    engine = MemoryEngine(str(tmp_path / "memory.db"), SimpleNamespace())
    admin_stopped, lifecycle_stopped, mutation_stopped = [], [], []
    engine.memory_administration = SimpleNamespace(
        close=AsyncMock(side_effect=lambda: admin_stopped.append(True))
    )
    engine.atom_lifecycle_manager = SimpleNamespace(
        stop=AsyncMock(side_effect=lambda: lifecycle_stopped.append(True))
    )
    started = asyncio.Event()

    async def mutation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            mutation_stopped.append(True)

    worker = engine._create_tracked_task(mutation())
    await asyncio.wait_for(started.wait(), 2)

    async def persist_close():
        assert admin_stopped and lifecycle_stopped and mutation_stopped
        assert worker.done() and not engine._pending_tasks

    initializer = InitializerFinalizeMixin()
    initializer.memory_engine = engine
    initializer._index_persisters = [
        SimpleNamespace(aclose=AsyncMock(side_effect=persist_close))
    ]
    try:
        await initializer.shutdown_index_persisters()
        assert admin_stopped and lifecycle_stopped and mutation_stopped
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_quiesce_blocks_new_writes_but_settles_authorized_compensation(tmp_path):
    engine = MemoryEngine(str(tmp_path / "memory.db"), SimpleNamespace())
    started, compensated = asyncio.Event(), asyncio.Event()

    async def compensation():
        await asyncio.sleep(0)
        compensated.set()

    async def mutation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await engine._create_tracked_task(compensation())

    worker = engine._create_tracked_task(mutation())
    await asyncio.wait_for(started.wait(), 2)
    try:
        await engine.quiesce_background_writes()
        assert compensated.is_set() and not engine._pending_tasks
        with pytest.raises(RuntimeError, match="关闭"):
            engine._create_tracked_task(asyncio.sleep(0))
        with pytest.raises(RuntimeError, match="关闭"):
            await engine.add_memory("late memory")
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_guard_waiter_rechecks_shutdown_after_acquiring_lock(tmp_path):
    engine = MemoryEngine(str(tmp_path / "memory.db"), SimpleNamespace())
    engine._memory_mutation_lock = asyncio.Lock()
    engine._memory_mutation_owner = None
    engine._memory_mutation_children = set()
    started = asyncio.Event()
    entered = []

    async def waiting_write():
        started.set()
        async with memory_mutation_guard(engine):
            entered.append(True)

    await engine._memory_mutation_lock.acquire()
    pending = asyncio.create_task(waiting_write())
    try:
        await asyncio.wait_for(started.wait(), 2)
        engine._writes_closing = True
    finally:
        engine._memory_mutation_lock.release()
    with pytest.raises(RuntimeError, match="关闭"):
        await asyncio.wait_for(pending, 2)
    assert not entered


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["add", "guard"])
async def test_shutdown_waits_for_foreground_write_and_its_children(tmp_path, kind):
    engine = MemoryEngine(str(tmp_path / "memory.db"), SimpleNamespace())
    started, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    caller_release = asyncio.Event()
    finished, child_finished, persisted = [], [], []

    async def child():
        await asyncio.sleep(0)
        child_finished.append(True)

    async def write_body(*args):
        started.set()
        await release.wait()
        await engine._create_tracked_task(child())
        finished.append(True)
        return 11

    async def guarded_write():
        async with memory_mutation_guard(engine):
            await write_body()

    async def foreground_caller():
        if kind == "add":
            await engine.add_memory("foreground fact")
        else:
            await guarded_write()
        # AstrBot may continue processing this event after our write finishes.
        await caller_release.wait()

    async def begin_shutdown():
        closing.set()

    async def persist_close():
        assert finished and child_finished
        persisted.append(True)

    engine.memory_administration = SimpleNamespace(close=begin_shutdown)
    engine._start_write_op = AsyncMock(return_value=None)
    engine._advance_write_op = AsyncMock()
    engine.hybrid_retriever = SimpleNamespace(add_memory=write_body)
    initializer = InitializerFinalizeMixin()
    initializer.memory_engine = engine
    initializer._index_persisters = [SimpleNamespace(aclose=persist_close)]
    writer = asyncio.create_task(foreground_caller())
    shutdown = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        shutdown = asyncio.create_task(initializer.shutdown_index_persisters())
        await asyncio.wait_for(closing.wait(), 2)
        await asyncio.sleep(0)
        assert not shutdown.done() and not persisted
        release.set()
        await asyncio.wait_for(shutdown, 2)
        assert persisted and not engine._pending_tasks
        assert not writer.done()
    finally:
        release.set()
        caller_release.set()
        await asyncio.gather(
            *(task for task in (writer, shutdown) if task is not None),
            return_exceptions=True,
        )
