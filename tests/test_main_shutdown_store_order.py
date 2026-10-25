"""Profile compensation still needs the shared conversation store on unload."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_anamnesis.core.managers.memory_mutation import memory_mutation_guard
from astrbot_plugin_anamnesis.core.plugin_initializer_finalize import (
    InitializerFinalizeMixin,
)
from astrbot_plugin_anamnesis.main import AnamnesisPlugin


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["background", "foreground"])
async def test_terminate_keeps_profile_store_open_until_writes_settle(tmp_path, kind):
    engine = MemoryEngine(str(tmp_path / "memory.db"), SimpleNamespace())
    started, release, shutdown_started = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    closed, cleaned = [], []

    async def stop_scheduler():
        shutdown_started.set()

    async def close_store():
        closed.append(True)

    async def profile_compensation():
        assert not closed
        cleaned.append(True)

    async def background_writer():
        started.set()
        try:
            await asyncio.Future()
        finally:
            await profile_compensation()

    async def foreground_writer():
        async with memory_mutation_guard(engine):
            started.set()
            await release.wait()
            await profile_compensation()

    initializer = InitializerFinalizeMixin()
    initializer.stop_background_tasks = AsyncMock()
    initializer.stop_scheduler = stop_scheduler
    initializer.conversation_manager = SimpleNamespace(
        store=SimpleNamespace(close=close_store)
    )
    initializer.memory_engine = engine
    initializer._index_persisters = []
    initializer.db = None
    plugin = SimpleNamespace(
        _background_tasks=set(), initializer=initializer, event_handler=None
    )
    writer = (
        engine._create_tracked_task(background_writer())
        if kind == "background"
        else asyncio.create_task(foreground_writer())
    )
    shutdown = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        shutdown = asyncio.create_task(AnamnesisPlugin.terminate(plugin))
        await asyncio.wait_for(shutdown_started.wait(), 2)
        if kind == "foreground":
            await asyncio.sleep(0)
            assert not closed
            release.set()
        await asyncio.wait_for(shutdown, 2)
        assert cleaned and closed
        if kind == "foreground":
            await writer
    finally:
        release.set()
        writer.cancel()
        await asyncio.gather(
            *(task for task in (writer, shutdown) if task is not None),
            return_exceptions=True,
        )
