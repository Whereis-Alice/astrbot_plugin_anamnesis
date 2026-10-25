"""Coordinate changes to existing documents without serializing ordinary adds."""

import asyncio
from contextlib import asynccontextmanager
from functools import wraps


@asynccontextmanager
async def memory_write_guard(engine):
    """Track the write's lifetime without owning its caller's entire task."""
    assert_open = getattr(engine, "_assert_writes_open", None)
    if callable(assert_open):
        assert_open()
    if not isinstance(getattr(engine, "_active_write_tasks", None), dict):
        engine._active_write_tasks = {}
    current = asyncio.current_task()
    owned = current not in engine._active_write_tasks
    if owned:
        finished = asyncio.get_running_loop().create_future()
        engine._active_write_tasks[current] = finished
    try:
        yield
    finally:
        if owned:
            engine._active_write_tasks.pop(current, None)
            if not finished.done():
                finished.set_result(None)


def track_memory_write(function):
    @wraps(function)
    async def tracked(engine, *args, **kwargs):
        async with memory_write_guard(engine):
            return await function(engine, *args, **kwargs)

    return tracked


@asynccontextmanager
async def memory_mutation_guard(engine):
    assert_open = getattr(engine, "_assert_writes_open", None)
    if callable(assert_open):
        assert_open()
    if not hasattr(engine, "_memory_mutation_lock"):
        engine._memory_mutation_lock = asyncio.Lock()
        engine._memory_mutation_owner = None
        engine._memory_mutation_children = set()
    current = asyncio.current_task()
    if (
        engine._memory_mutation_owner is current
        or current in engine._memory_mutation_children
    ):
        yield
        return
    # Recheck after acquiring the lock: admission may predate shutdown.
    async with engine._memory_mutation_lock, memory_write_guard(engine):
        engine._memory_mutation_owner = current
        try:
            yield
        finally:
            engine._memory_mutation_owner = None
            engine._memory_mutation_children.clear()


def memory_mutation_task(engine, coroutine, *, track=True):
    """Grant only an explicitly awaited child access to its parent's guard.

    Untracked children must be shielded and settled by their tracked parent so
    shutdown cannot cancel both sides of a staged mutation independently.
    """
    creator = (
        getattr(engine, "_create_tracked_task", asyncio.create_task)
        if track
        else asyncio.create_task
    )
    task = creator(coroutine)
    current = asyncio.current_task()
    children = getattr(engine, "_memory_mutation_children", set())
    if (
        getattr(engine, "_memory_mutation_owner", None) is current
        or current in children
    ):
        children.add(task)
        task.add_done_callback(children.discard)
    return task


async def settle_memory_mutation_task(task):
    """Finish authorized compensation even when its caller is cancelled again."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
    return task.result()


def serialize_memory_mutation(function):
    @wraps(function)
    async def guarded(engine, *args, **kwargs):
        async with memory_mutation_guard(engine):
            return await function(engine, *args, **kwargs)

    return guarded


@asynccontextmanager
async def index_projection_guard(engine):
    """Coordinate bounded projection replay with full index rebuilds."""
    if not hasattr(engine, "_index_projection_lock"):
        engine._index_projection_lock = asyncio.Lock()
        engine._index_projection_owner = None
    task = asyncio.current_task()
    if engine._index_projection_owner is task:
        yield
        return
    async with engine._index_projection_lock:
        engine._index_projection_owner = task
        try:
            yield
        finally:
            engine._index_projection_owner = None


def serialize_index_projection(function):
    @wraps(function)
    async def guarded(host, memory_engine, *args, **kwargs):
        async with index_projection_guard(memory_engine):
            return await function(host, memory_engine, *args, **kwargs)
    return guarded
