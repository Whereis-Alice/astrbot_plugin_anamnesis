"""Episode reads cannot combine source versions that were never valid together."""

import asyncio

import pytest

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]


async def make_episode(service, monkeypatch, legacy=False):
    ids = [await add(service), await add(service, "喜欢咖啡和书")]
    member = service._episode_member

    async def old_member(memory_id):
        return {**await member(memory_id), "source_session_id": None}

    with monkeypatch.context() as patch:
        if legacy:
            patch.setattr(service, "_episode_member", old_member)
        job = await service.create_job("episode", {"memory_ids": ids, "use_llm": False})
        await service._jobs[job["operation_id"]]
    assert (await service.store.get(job["operation_id"]))["status"] == "completed"
    return ids


def pause_member(service, monkeypatch, memory_id):
    paused, release = asyncio.Event(), asyncio.Event()
    member = service._episode_member
    has_paused = False

    async def pause_once(current_id):
        nonlocal has_paused
        if current_id == memory_id and not has_paused:
            has_paused = True
            paused.set()
            await release.wait()
        return await member(current_id)

    monkeypatch.setattr(service, "_episode_member", pause_once)
    return paused, release


async def read_episode(service, method):
    if method == "list":
        return await service.list_episodes()
    return await service.episode_candidates(
        "咖啡", [], session_id="qq:GroupMessage:1", persona_id="p"
    )


def assert_stale_or_empty(result, method):
    if method == "list":
        assert len(result) == 1 and result[0]["stale"] and result[0]["summary"] == ""
    else:
        assert result == []


async def settle(tasks, release):
    release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["list", "candidates"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_sources_that_never_match_together_cannot_validate_an_event(
    administration, monkeypatch, method, legacy
):
    first, second = await make_episode(administration, monkeypatch, legacy)
    second_source = await administration.engine.get_memory_source(second)
    await administration.engine.save_memory_source(
        second, [{"content": "second evidence changed"}]
    )
    assert_stale_or_empty(await read_episode(administration, method), method)
    paused, release = pause_member(administration, monkeypatch, second)
    reading = asyncio.create_task(read_episode(administration, method))
    tasks = [reading]

    async def change_sources():
        # First invalidate the other member, then restore this member. The
        # original event never has two matching sources during this read.
        await administration.engine.save_memory_source(
            first, [{"content": "first evidence changed"}]
        )
        await administration.engine.save_memory_source(second, second_source)

    try:
        await asyncio.wait_for(paused.wait(), 5)
        changing = asyncio.create_task(change_sources())
        tasks.append(changing)
        try:
            await asyncio.wait_for(asyncio.shield(changing), 0.2)
        except TimeoutError:
            pass
        release.set()
        result = await asyncio.wait_for(reading, 5)
        await asyncio.wait_for(changing, 5)
        assert_stale_or_empty(result, method)
        assert_stale_or_empty(await read_episode(administration, method), method)
    finally:
        await settle(tasks, release)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["list", "candidates"])
async def test_cancelling_member_validation_releases_queued_source_deletion(
    administration, monkeypatch, method
):
    first, second = await make_episode(administration, monkeypatch)
    paused, release = pause_member(administration, monkeypatch, second)
    reading = asyncio.create_task(read_episode(administration, method))
    tasks = [reading]
    try:
        await asyncio.wait_for(paused.wait(), 5)
        deleting = asyncio.create_task(administration.engine.delete_memory(first))
        tasks.append(deleting)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reading, 5)
        assert await asyncio.wait_for(deleting, 5)
        assert_stale_or_empty(await read_episode(administration, method), method)
    finally:
        await settle(tasks, release)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["list", "candidates"])
async def test_shutdown_waits_for_started_episode_source_validation(
    administration, monkeypatch, method
):
    _, second = await make_episode(administration, monkeypatch)
    paused, release = pause_member(administration, monkeypatch, second)
    reading = asyncio.create_task(read_episode(administration, method))
    tasks = [reading]
    try:
        await asyncio.wait_for(paused.wait(), 5)
        closing = asyncio.create_task(administration.engine.close())
        tasks.append(closing)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), 0.05)
        release.set()
        result = await asyncio.wait_for(reading, 5)
        assert result and (method == "candidates" or not result[0]["stale"])
        await asyncio.wait_for(closing, 5)
        with pytest.raises(RuntimeError):
            await read_episode(administration, method)
    finally:
        await settle(tasks, release)
