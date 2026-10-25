"""Real SQLite episode provenance and legacy publication compatibility."""

import asyncio
import json

import pytest
from astrbot_plugin_anamnesis.core.managers.scope_migrator import ScopeMigrator

from tests.test_memory_administration import administration as administration


SCOPE = "livingmemory:user:qq:alice"
SOURCE = "qq:GroupMessage:1"
OTHER_SOURCE = "qq:GroupMessage:2"


async def add_evidence(service, index, metadata, *, source=SOURCE):
    text = f"coffee event evidence {index}"
    return await service.engine.add_memory(
        content=text,
        session_id=SCOPE,
        persona_id="p",
        metadata={"key_facts": [text], **metadata},
        source_messages=[
            {"role": "user", "sender_id": "alice", "content": text, "session_id": source}
        ],
    )


async def run_episode(service, ids, **payload):
    job = await service.create_job(
        "episode", {"memory_ids": ids, "use_llm": False, **payload}
    )
    await service._jobs[job["operation_id"]]
    return await service.store.get(job["operation_id"])


async def stored_episode(service, episode_id):
    async with service.store.connection() as db:
        row = await (
            await db.execute("SELECT * FROM memory_episodes WHERE id=?", (episode_id,))
        ).fetchone()
    return service.store.decode(row)


async def publish_legacy_episode(service, monkeypatch, ids):
    """Exercise the old public job path, which omitted window-only sources."""
    member = service._episode_member

    async def legacy_member(memory_id):
        current = await member(memory_id)
        return {**current, "source_session_id": None}

    with monkeypatch.context() as patch:
        patch.setattr(service, "_episode_member", legacy_member)
        operation = await run_episode(service, ids)
    assert operation["status"] == "completed"
    original = await stored_episode(service, operation["result"]["episode_id"])
    assert original["source_session_id"] is None
    return original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"source_session_id": SOURCE},
        {"source_window": {"session_id": SOURCE}},
        {"source_session_id": SOURCE, "source_window": {"session_id": SOURCE}},
        {"source_session_id": "1", "source_window": {"session_id": SOURCE}},
    ],
    ids=["explicit", "legacy-window", "consistent-hints", "bare-id-with-window"],
)
async def test_same_chat_evidence_keeps_explicit_source(administration, metadata):
    ids = [await add_evidence(administration, index, metadata) for index in range(2)]
    operation = await run_episode(administration, ids)
    assert operation["status"] == "completed"
    episode = (await administration.list_episodes())[0]
    assert not episode["stale"] and episode["source_session_id"] == SOURCE
    assert episode["memory_ids"] == ids
    assert [item["id"] for item in await administration.episode_candidates(
        "coffee", [], session_id=SCOPE, persona_id="p"
    )] == [episode["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("window_only", [False, True])
async def test_shared_user_scope_cannot_merge_different_chats(administration, window_only):
    ids = []
    for index, source in enumerate((SOURCE, OTHER_SOURCE)):
        metadata = {"source_window": {"session_id": source}}
        if not window_only:
            metadata["source_session_id"] = source
        ids.append(await add_evidence(administration, index, metadata, source=source))
    operation = await run_episode(administration, ids)
    assert operation["status"] == "failed"
    assert not await administration.list_episodes()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"source_session_id": SOURCE, "source_window": {"session_id": OTHER_SOURCE}},
        {},
        {"source_session_id": "1"},
        {"source_window": {"session_id": "1"}},
        {"source_session_id": "qq:GroupMessage: 1"},
    ],
    ids=["conflicting-hints", "no-hints", "bare-id", "bare-window", "malformed-hint"],
)
async def test_unknown_or_conflicting_origin_is_not_inferred_from_scope_or_raw_source(
    administration, metadata
):
    memory_id = await add_evidence(administration, 0, metadata)
    operation = await run_episode(administration, [memory_id])
    assert operation["status"] == "failed"
    assert not await administration.list_episodes()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["explicit-source", "window-source", "scope-migration"])
async def test_generation_rechecks_origin_and_scope_before_publication(
    administration, monkeypatch, tmp_path, change
):
    memory_id = await add_evidence(administration, 0, {
        "source_session_id": SOURCE, "source_window": {"session_id": SOURCE}
    })
    captured, release = asyncio.Event(), asyncio.Event()

    async def complete_generation(*args, **kwargs):
        captured.set()
        await release.wait()
        return json.dumps({
            "title": "coffee event", "summary": "coffee evidence", "memory_ids": [memory_id]
        })

    monkeypatch.setattr(administration.processor, "_call_llm_with_retry", complete_generation)
    job = await administration.create_job("episode", {"memory_ids": [memory_id]})
    task = administration._jobs[job["operation_id"]]
    try:
        await asyncio.wait_for(captured.wait(), 5)
        if change == "scope-migration":
            migrator = ScopeMigrator(administration.engine, tmp_path / "migration-backups")
            preview = await migrator.preview(SCOPE, "livingmemory:user:qq:alice-new")
            await migrator.apply(SCOPE, "livingmemory:user:qq:alice-new", preview["fingerprint"])
        else:
            patch = ({"source_session_id": OTHER_SOURCE} if change == "explicit-source"
                     else {"source_window": {"session_id": OTHER_SOURCE}})
            assert await administration.engine.update_memory(memory_id, {"metadata": patch})
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 5)
    assert (await administration.store.get(job["operation_id"]))["status"] == "failed"
    assert not await administration.list_episodes()


@pytest.mark.asyncio
async def test_legacy_same_chat_event_can_be_read_recalled_and_rebuilt(administration, monkeypatch):
    ids = [await add_evidence(administration, index, {"source_window": {"session_id": SOURCE}})
           for index in range(2)]
    original = await publish_legacy_episode(administration, monkeypatch, ids)
    episode = (await administration.list_episodes())[0]
    assert not episode["stale"] and episode["source_session_id"] == SOURCE
    candidates = await administration.episode_candidates("coffee", [], session_id=SCOPE, persona_id="p")
    assert [candidate["id"] for candidate in candidates] == [original["id"]]
    operation = await run_episode(administration, ids, episode_id=original["id"],
                                  expected_revision=original["revision"], title="rebuilt coffee event")
    assert operation["status"] == "completed"
    rebuilt = await stored_episode(administration, original["id"])
    assert rebuilt["source_session_id"] == SOURCE
    assert rebuilt["revision"] == original["revision"] + 1
    assert rebuilt["title"] == "rebuilt coffee event"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["importance", "persona_id", "session_id"])
async def test_legacy_source_upgrade_does_not_hide_other_member_changes(
    administration, monkeypatch, change
):
    memory_id = await add_evidence(administration, 0, {"source_window": {"session_id": SOURCE}})
    await publish_legacy_episode(administration, monkeypatch, [memory_id])
    value = 0.7 if change == "importance" else "changed-value"
    assert await administration.engine.update_memory(memory_id, {"metadata": {change: value}})
    episode = (await administration.list_episodes())[0]
    assert episode["stale"] and episode["summary"] == ""
    assert not await administration.episode_candidates("coffee", [], session_id=SCOPE, persona_id="p")


@pytest.mark.asyncio
@pytest.mark.parametrize("rebuild", ["same-members", "subset", "replacement"])
async def test_legacy_mixed_chat_event_is_stale_and_cannot_upgrade_other_members(
    administration, monkeypatch, rebuild
):
    ids = [await add_evidence(administration, index, {"source_window": {"session_id": source}},
                              source=source)
           for index, source in enumerate((SOURCE, OTHER_SOURCE))]
    original = await publish_legacy_episode(administration, monkeypatch, ids)
    episode = (await administration.list_episodes())[0]
    assert episode["stale"] and episode["summary"] == ""
    assert not await administration.list_episodes(include_stale=False)
    assert not await administration.episode_candidates("coffee", [], session_id=SCOPE, persona_id="p")
    if rebuild == "subset":
        ids = ids[:1]
    elif rebuild == "replacement":
        ids = [ids[0], await add_evidence(administration, 2, {"source_window": {"session_id": SOURCE}})]
    operation = await run_episode(administration, ids, episode_id=original["id"],
                                  expected_revision=original["revision"])
    assert operation["status"] == "failed"
    assert await stored_episode(administration, original["id"]) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("same_member", [False, True])
async def test_explicit_event_cannot_be_rebuilt_into_a_different_origin(administration, same_member):
    memory_id = await add_evidence(administration, 0, {"source_session_id": SOURCE})
    original_job = await run_episode(administration, [memory_id])
    original = await stored_episode(administration, original_job["result"]["episode_id"])
    if same_member:
        assert await administration.engine.update_memory(memory_id, {
            "metadata": {"source_session_id": OTHER_SOURCE}
        })
    else:
        memory_id = await add_evidence(administration, 1, {"source_session_id": OTHER_SOURCE},
                                       source=OTHER_SOURCE)
    operation = await run_episode(administration, [memory_id], episode_id=original["id"],
                                  expected_revision=original["revision"])
    assert operation["status"] == "failed"
    assert await stored_episode(administration, original["id"]) == original
