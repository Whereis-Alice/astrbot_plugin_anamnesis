"""Skip imports coordinate with plain adds without serializing ordinary writers."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.managers import memory_mutation
from astrbot_plugin_anamnesis.core.managers.memory_mutation import (
    memory_mutation_guard,
    memory_mutation_task,
)
from astrbot_plugin_anamnesis.core.memory_transfer import ImportEntry
from astrbot_plugin_anamnesis.core.page_api_modules.memory_handler import MemoryHandler
from astrbot_plugin_anamnesis.core.page_api_modules.utils import PageApiUtils

from tests.test_memory_transfer_runtime_safety import _engine

SCOPE = "qq:GroupMessage:1"


@pytest_asyncio.fixture
async def engine(tmp_path):
    instance = await _engine(tmp_path / "admission.db")
    instance.atom_enabled = False
    try:
        yield instance
    finally:
        await instance.close()


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), 1)


def import_write(engine, content="shared fact"):
    entry = ImportEntry(0, content, session_id=SCOPE, persona_id="p")
    return MemoryHandler(PageApiUtils())._write_import_entry(
        engine,
        entry,
        content,
        0.5,
        {"session_id": SCOPE, "persona_id": "p", "status": "active"},
        None,
        skip_duplicates=True,
    )


def assert_admission_released(engine):
    assert not engine._memory_add_readers
    assert engine._memory_import_owner is None
    assert engine._memory_import_waiters == 0
    assert not engine._active_write_tasks


@pytest.mark.asyncio
async def test_skip_import_waits_for_inflight_plain_add_before_checking(
    engine, monkeypatch
):
    entered, release, checked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    add = engine.hybrid_retriever.add_memory
    check = engine.has_memory_import_key

    async def paused_add(*args, **kwargs):
        entered.set()
        await release.wait()
        return await add(*args, **kwargs)

    async def checked_key(key):
        checked.set()
        return await check(key)

    monkeypatch.setattr(engine.hybrid_retriever, "add_memory", paused_add)
    monkeypatch.setattr(engine, "has_memory_import_key", checked_key)
    ordinary = asyncio.create_task(
        engine.add_memory("shared fact", session_id=SCOPE, persona_id="p")
    )
    imported = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        imported = asyncio.create_task(import_write(engine))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(checked.wait(), 0.1)
        release.set()
        assert await asyncio.wait_for(ordinary, 1) > 0
        assert await asyncio.wait_for(imported, 1) is None
        rows = await (
            await engine.db_connection.execute("SELECT id FROM documents")
        ).fetchall()
        assert len(rows) == 1
        assert_admission_released(engine)
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (ordinary, imported) if task), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_later_plain_add_cannot_enter_after_skip_import_lookup(
    engine, monkeypatch
):
    checked, release, added = asyncio.Event(), asyncio.Event(), asyncio.Event()
    check = engine.has_memory_import_key
    add = engine.hybrid_retriever.add_memory

    async def paused_check(key):
        result = await check(key)
        checked.set()
        await release.wait()
        return result

    async def observed_add(content, metadata):
        if content == "later ordinary fact":
            added.set()
        return await add(content, metadata)

    monkeypatch.setattr(engine, "has_memory_import_key", paused_check)
    monkeypatch.setattr(engine.hybrid_retriever, "add_memory", observed_add)
    imported = asyncio.create_task(import_write(engine))
    ordinary = None
    try:
        await asyncio.wait_for(checked.wait(), 1)
        ordinary = asyncio.create_task(engine.add_memory("later ordinary fact"))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(added.wait(), 0.1)
        release.set()
        assert await asyncio.wait_for(imported, 1) > 0
        assert await asyncio.wait_for(ordinary, 1) > 0
        assert_admission_released(engine)
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (ordinary, imported) if task), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_multiple_plain_adds_reach_storage_concurrently(engine, monkeypatch):
    release, both_entered = asyncio.Event(), asyncio.Event()
    entered = set()
    add = engine.hybrid_retriever.add_memory

    async def barrier_add(content, metadata):
        entered.add(content)
        if len(entered) == 2:
            both_entered.set()
        await release.wait()
        return await add(content, metadata)

    monkeypatch.setattr(engine.hybrid_retriever, "add_memory", barrier_add)
    tasks = [
        asyncio.create_task(engine.add_memory(content))
        for content in ("first", "second")
    ]
    try:
        await asyncio.wait_for(both_entered.wait(), 1)
        release.set()
        assert all(
            memory_id > 0
            for memory_id in await asyncio.wait_for(asyncio.gather(*tasks), 1)
        )
        assert_admission_released(engine)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_inflight_add_can_finish_source_save_before_import_owns_mutation(
    engine, monkeypatch
):
    source_entered, release_source = asyncio.Event(), asyncio.Event()
    save = engine.save_memory_source
    source = [{"role": "user", "content": "shared fact", "sender_id": "u"}]

    async def paused_save(*args):
        source_entered.set()
        await release_source.wait()
        return await save(*args)

    monkeypatch.setattr(engine, "save_memory_source", paused_save)
    ordinary = asyncio.create_task(
        engine.add_memory(
            "shared fact", session_id=SCOPE, persona_id="p", source_messages=source
        )
    )
    imported = None
    try:
        await asyncio.wait_for(source_entered.wait(), 1)
        imported = asyncio.create_task(import_write(engine))
        await until(lambda: getattr(engine, "_memory_import_waiters", 0) == 1)
        release_source.set()
        memory_id = await asyncio.wait_for(ordinary, 1)
        assert await asyncio.wait_for(imported, 1) is None
        assert (await engine.get_memory_source(memory_id))[0][
            "content"
        ] == "shared fact"
        assert_admission_released(engine)
    finally:
        release_source.set()
        await asyncio.gather(
            *(task for task in (ordinary, imported) if task), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True])
async def test_mutation_owner_and_explicit_child_can_add_while_import_waits_for_mutation(
    engine, child
):
    imported = None
    try:
        async with memory_mutation_guard(engine):
            imported = asyncio.create_task(import_write(engine, "imported fact"))
            await until(
                lambda: getattr(engine, "_memory_import_owner", None) is not None
            )
            if child:
                added = memory_mutation_task(
                    engine, engine.add_memory("replacement fact"), track=False
                )
                assert await asyncio.wait_for(added, 1) > 0
            else:
                assert await engine.add_memory("replacement fact") > 0
        assert await asyncio.wait_for(imported, 1) > 0
        assert_admission_released(engine)
    finally:
        if imported:
            imported.cancel()
            await asyncio.gather(imported, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeatedly_cancelled_exclusive_waiter_releases_admission(
    engine, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    add = engine.hybrid_retriever.add_memory

    async def paused_add(content, metadata):
        if content == "first":
            entered.set()
            await release.wait()
        return await add(content, metadata)

    monkeypatch.setattr(engine.hybrid_retriever, "add_memory", paused_add)
    ordinary = asyncio.create_task(engine.add_memory("first"))
    imported = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        imported = asyncio.create_task(import_write(engine))
        await until(lambda: getattr(engine, "_memory_import_waiters", 0) == 1)
        async with engine._memory_add_condition:
            imported.cancel()
            await asyncio.sleep(0)
            imported.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await imported
        assert engine._memory_import_waiters == 0
        assert engine._memory_import_owner is None
        assert await asyncio.wait_for(engine.add_memory("after cancellation"), 1) > 0
        release.set()
        await asyncio.wait_for(ordinary, 1)
        assert_admission_released(engine)
    finally:
        release.set()
        if imported:
            imported.cancel()
        await asyncio.gather(
            *(task for task in (ordinary, imported) if task), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_target", ["reader", "owner"])
async def test_repeated_cancellation_of_reader_or_exclusive_owner_does_not_leak(
    engine, monkeypatch, cancel_target
):
    checked, release = asyncio.Event(), asyncio.Event()

    async def paused_check(key):
        checked.set()
        await release.wait()
        return False

    monkeypatch.setattr(engine, "has_memory_import_key", paused_check)
    imported = asyncio.create_task(import_write(engine))
    ordinary = None
    try:
        await asyncio.wait_for(checked.wait(), 1)
        ordinary = asyncio.create_task(engine.add_memory("waiting reader"))
        await until(lambda: ordinary in engine._active_write_tasks)
        target = ordinary if cancel_target == "reader" else imported
        async with engine._memory_add_condition:
            target.cancel()
            await asyncio.sleep(0)
            target.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await target
        release.set()
        survivor = imported if target is ordinary else ordinary
        await asyncio.wait_for(survivor, 1)
        assert_admission_released(engine)
        assert await asyncio.wait_for(engine.add_memory("next writer"), 1) > 0
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (ordinary, imported) if task), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_shutdown_settles_inflight_add_and_cancels_import_admission(
    engine, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    add = engine.hybrid_retriever.add_memory

    async def paused_add(*args):
        entered.set()
        await release.wait()
        return await add(*args)

    monkeypatch.setattr(engine.hybrid_retriever, "add_memory", paused_add)
    ordinary = asyncio.create_task(engine.add_memory("inflight fact"))
    imported = shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        entry = ImportEntry(0, "imported fact", session_id=SCOPE, persona_id="p")
        imported = asyncio.create_task(
            MemoryHandler(PageApiUtils())._import_entries(
                engine, None, [entry], [], "skip"
            )
        )
        await until(lambda: getattr(engine, "_memory_import_waiters", 0) == 1)
        shutdown = asyncio.create_task(engine.quiesce_background_writes())
        await until(lambda: engine._writes_closing)
        release.set()
        await asyncio.wait_for(shutdown, 1)
        assert await asyncio.wait_for(ordinary, 1) > 0
        with pytest.raises(asyncio.CancelledError):
            await imported
        assert_admission_released(engine)
        assert not engine._pending_tasks
        with pytest.raises(RuntimeError, match="关闭"):
            await engine.add_memory("new writer")
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (ordinary, imported, shutdown) if task),
            return_exceptions=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_stage", ["administration", "lifecycle"])
async def test_shutdown_rejects_queued_import_before_background_services_stop(
    engine, monkeypatch, slow_stage
):
    entered, release = asyncio.Event(), asyncio.Event()
    stopping, resume_stop = asyncio.Event(), asyncio.Event()
    add = engine.hybrid_retriever.add_memory
    stop_lifecycle = engine.atom_lifecycle_manager.stop

    async def paused_add(*args):
        entered.set()
        await release.wait()
        return await add(*args)

    async def paused_stop():
        stopping.set()
        await resume_stop.wait()
        if slow_stage == "lifecycle":
            await stop_lifecycle()

    if slow_stage == "administration":
        engine.memory_administration = SimpleNamespace(close=paused_stop)
    else:
        monkeypatch.setattr(engine.atom_lifecycle_manager, "stop", paused_stop)
    monkeypatch.setattr(engine.hybrid_retriever, "add_memory", paused_add)
    ordinary = asyncio.create_task(engine.add_memory("inflight fact"))
    imported = shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        entry = ImportEntry(0, "imported fact", session_id=SCOPE, persona_id="p")
        imported = asyncio.create_task(
            MemoryHandler(PageApiUtils())._import_entries(
                engine, None, [entry], [], "skip"
            )
        )
        await until(lambda: engine._memory_import_waiters == 1)
        shutdown = asyncio.create_task(engine.quiesce_background_writes())
        await asyncio.wait_for(stopping.wait(), 1)
        assert engine._writes_closing and not shutdown.done()
        release.set()
        assert await asyncio.wait_for(ordinary, 1) > 0
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(imported, 1)
        async with engine.db_connection.execute("SELECT text FROM documents") as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ["inflight fact"]
        assert_admission_released(engine)
        resume_stop.set()
        await asyncio.wait_for(shutdown, 1)
        assert not engine._pending_tasks
    finally:
        release.set()
        resume_stop.set()
        await asyncio.gather(
            *(task for task in (ordinary, imported, shutdown) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["ordinary", "import"])
async def test_cancellation_during_admission_cleanup_is_preserved_and_settled(
    engine, monkeypatch, writer
):
    cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event()
    cleanup = memory_mutation._release_memory_add_admission

    async def paused_cleanup(*args, **kwargs):
        cleanup_entered.set()
        await release_cleanup.wait()
        await cleanup(*args, **kwargs)

    monkeypatch.setattr(
        memory_mutation, "_release_memory_add_admission", paused_cleanup
    )
    operation = (
        engine.add_memory("ordinary fact")
        if writer == "ordinary"
        else import_write(engine)
    )
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(cleanup_entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert_admission_released(engine)
        assert await engine.add_memory("next ordinary fact") > 0
    finally:
        release_cleanup.set()
        await asyncio.gather(task, return_exceptions=True)
