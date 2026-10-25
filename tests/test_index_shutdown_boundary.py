"""Maintenance writes must finish before index persisters are closed."""

import asyncio
from types import SimpleNamespace

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_anamnesis.core.managers.memory_mutation import (
    index_projection_guard,
)
from astrbot_plugin_anamnesis.core.plugin_initializer_finalize import (
    InitializerFinalizeMixin,
)
from astrbot_plugin_anamnesis.core.validators.index_validator import IndexValidator


def maintenance_engine(tmp_path, operation):
    engine = MemoryEngine(str(tmp_path / "memory.db"), SimpleNamespace())
    engine.db_connection = object()
    engine.graph_memory_manager = SimpleNamespace(rebuild_memory_batches=operation)
    validator = IndexValidator(engine.db_path, engine.faiss_db)
    validator._rebuild_indexes_locked = operation
    return engine, validator


async def rebuild(kind, engine, validator):
    if kind == "document":
        return await validator.rebuild_indexes(engine)
    return await engine.rebuild_graph_index()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["document", "graph"])
async def test_shutdown_waits_for_manual_index_rebuild(tmp_path, kind):
    started, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finished, persisted = [], []

    async def operation(*args, **kwargs):
        started.set()
        await release.wait()
        finished.append(True)
        return {"success": True}

    async def begin_shutdown():
        closing.set()

    async def persist_close():
        assert finished
        persisted.append(True)

    engine, validator = maintenance_engine(tmp_path, operation)
    engine.memory_administration = SimpleNamespace(close=begin_shutdown)
    initializer = InitializerFinalizeMixin()
    initializer.memory_engine = engine
    initializer._index_persisters = [SimpleNamespace(aclose=persist_close)]
    writer = asyncio.create_task(rebuild(kind, engine, validator))
    shutdown = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        shutdown = asyncio.create_task(initializer.shutdown_index_persisters())
        await asyncio.wait_for(closing.wait(), 2)
        await asyncio.sleep(0)
        assert not shutdown.done() and not persisted
        release.set()
        await asyncio.wait_for(asyncio.gather(writer, shutdown), 2)
        assert persisted and not engine._active_write_tasks
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (writer, shutdown) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["document", "graph"])
async def test_rebuild_cannot_start_after_shutdown(tmp_path, kind):
    called = []

    async def operation(*args, **kwargs):
        called.append(True)
        return {"success": True}

    engine, validator = maintenance_engine(tmp_path, operation)
    await engine.quiesce_background_writes()
    with pytest.raises(RuntimeError, match="关闭"):
        await rebuild(kind, engine, validator)
    assert not called


@pytest.mark.asyncio
async def test_projection_waiter_rechecks_shutdown_after_lock(tmp_path):
    engine = MemoryEngine(str(tmp_path / "memory.db"), SimpleNamespace())
    engine._index_projection_lock = asyncio.Lock()
    engine._index_projection_owner = None
    started = asyncio.Event()
    entered = []

    async def waiting_rebuild():
        started.set()
        async with index_projection_guard(engine):
            entered.append(True)

    await engine._index_projection_lock.acquire()
    pending = asyncio.create_task(waiting_rebuild())
    try:
        await asyncio.wait_for(started.wait(), 2)
        engine._writes_closing = True
    finally:
        engine._index_projection_lock.release()
    with pytest.raises(RuntimeError, match="关闭"):
        await asyncio.wait_for(pending, 2)
    assert not entered
