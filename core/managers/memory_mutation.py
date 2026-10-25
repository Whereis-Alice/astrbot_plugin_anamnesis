"""Coordinate changes to existing documents without serializing ordinary adds."""

import asyncio
from contextlib import asynccontextmanager
from functools import wraps


@asynccontextmanager
async def memory_mutation_guard(engine):
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
    async with engine._memory_mutation_lock:
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
