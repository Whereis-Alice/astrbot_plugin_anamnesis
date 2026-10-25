"""Committed chat migrations cannot let old extractions recreate deleted profiles."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.test_profile_migration_followup import fact, owner
from tests.test_profile_observation_lifecycle import extracting, source
from tests.test_profile_observation_lifecycle import system as system


def start_extraction(profiles, event, memory_id):
    entered, release = asyncio.Event(), asyncio.Event()

    async def llm(*args, **kwargs):
        entered.set()
        await release.wait()
        return json.dumps({"facts": [fact()]}, ensure_ascii=False)

    task = asyncio.create_task(
        profiles.update_from_messages(
            event,
            [{"role": "user", "sender_id": event.get_sender_id(), "content": fact()["evidence"]}],
            SimpleNamespace(_call_llm_with_retry=llm),
            source_memory_id=memory_id,
        )
    )
    return task, entered, release


@pytest.mark.asyncio
@pytest.mark.parametrize("deletion", ["web", "chat_key", "chat_all"])
@pytest.mark.parametrize("boundary", ["mode", "isolated"])
async def test_deleted_migrated_profile_is_not_resurrected_after_scope_roundtrip(
    system, deletion, boundary
):
    engine, profiles = system
    memory_id = await source(engine)
    await profiles.upsert_facts(owner(), [fact()], source_memory_id=memory_id)
    task, entered, release = extracting(profiles, memory_id)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        original_scope = profiles.scope_for_event(owner())
        if boundary == "mode":
            profiles.config_manager.values["user_profile.scope_mode"] = "session"
        else:
            profiles.config_manager.values["filtering_settings.isolated_sessions"] = owner().unified_msg_origin
        moved = await profiles.get_profile(owner())
        assert len(moved) == 1 and moved[0]["profile_scope"] != original_scope
        if deletion == "web":
            assert await profiles.delete_for_web(moved[0]["profile_scope"], "drink") == 1
        else:
            assert await profiles.delete_profile(owner(), "drink" if deletion == "chat_key" else None) == 1
        assert await profiles.get_profile(owner()) == []
        profiles.config_manager.values["user_profile.scope_mode"] = "user"
        profiles.config_manager.values["filtering_settings.isolated_sessions"] = ""
        release.set()
        assert await asyncio.wait_for(task, 5) == 0
        assert await profiles.get_profile(owner()) == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("other", ["chat", "account"])
async def test_migration_and_deletion_in_other_scope_keeps_valid_extraction(system, other):
    engine, profiles = system
    migrating = owner()
    if other == "account":
        migrating.get_sender_id = lambda: "bob"
    first_id = await source(engine)
    await profiles.upsert_facts(migrating, [fact()], source_memory_id=first_id)
    unaffected = owner("2") if other == "chat" else owner()
    second_id = await engine.add_memory(
        content=fact()["evidence"], session_id=unaffected.unified_msg_origin, persona_id=None,
    )
    task, entered, release = start_extraction(profiles, unaffected, second_id)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        profiles.config_manager.values["filtering_settings.isolated_sessions"] = migrating.unified_msg_origin
        assert len(await profiles.get_profile(migrating)) == 1
        assert await profiles.delete_profile(migrating) == 1
        profiles.config_manager.values["filtering_settings.isolated_sessions"] = ""
        release.set()
        assert await asyncio.wait_for(task, 5) == 1
        assert (await profiles.get_profile(unaffected))[0]["source_memory_id"] == second_id
        if other == "account":
            assert await profiles.get_profile(migrating) == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["split", "commit"])
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_failed_or_cancelled_migration_rolls_back_without_fencing_extraction(
    system, monkeypatch, stage, error_type
):
    engine, profiles = system
    original_id, fresh_id = await source(engine), await source(engine)
    await profiles.upsert_facts(owner(), [fact()], source_memory_id=original_id)
    task, entered, release = extracting(profiles, fresh_id)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        profiles.config_manager.values["user_profile.scope_mode"] = "session"
        with monkeypatch.context() as patch:
            if stage == "split":
                split = profiles._finish_profile_split

                async def failed_split(*args):
                    await split(*args)
                    raise error_type("migration interrupted")

                patch.setattr(profiles, "_finish_profile_split", failed_split)
            else:
                async def failed_commit():
                    raise error_type("migration interrupted")

                patch.setattr(profiles.store.connection, "commit", failed_commit)
            with pytest.raises(error_type):
                await profiles.get_profile(owner())
        profiles.config_manager.values["user_profile.scope_mode"] = "user"
        before = await profiles.get_profile(owner())
        assert len(before) == 1 and before[0]["source_memory_id"] == original_id
        assert not profiles.store.connection.in_transaction
        release.set()
        assert await asyncio.wait_for(task, 5) == 1
        row = (await profiles.get_profile(owner()))[0]
        assert {item["source_memory_id"] for item in row["evidence"]} == {original_id, fresh_id}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("committed_before_cancel", [False, True])
async def test_cancellation_during_successful_commit_still_fences_old_chat_extraction(
    system, monkeypatch, committed_before_cancel
):
    engine, profiles = system
    memory_id = await source(engine)
    await profiles.upsert_facts(owner(), [fact()], source_memory_id=memory_id)
    task, entered, release = extracting(profiles, memory_id)
    committing, finish_commit = asyncio.Event(), asyncio.Event()
    commit = profiles.store.connection.commit
    migrating = None

    async def delayed_commit():
        if committed_before_cancel:
            await commit()
        committing.set()
        await finish_commit.wait()
        if not committed_before_cancel:
            await commit()

    try:
        await asyncio.wait_for(entered.wait(), 5)
        profiles.config_manager.values["user_profile.scope_mode"] = "session"
        with monkeypatch.context() as patch:
            patch.setattr(profiles.store.connection, "commit", delayed_commit)
            migrating = asyncio.create_task(profiles.get_profile(owner()))
            await asyncio.wait_for(committing.wait(), 5)
            migrating.cancel()
            await asyncio.sleep(0)
            migrating.cancel()
            finish_commit.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(migrating, 5)
        assert len(await profiles.get_profile(owner())) == 1
        assert await profiles.delete_profile(owner()) == 1
        profiles.config_manager.values["user_profile.scope_mode"] = "user"
        release.set()
        assert await asyncio.wait_for(task, 5) == 0
        assert await profiles.get_profile(owner()) == []
        assert not profiles._profile_extractions()
    finally:
        finish_commit.set()
        release.set()
        await asyncio.gather(*(item for item in (task, migrating) if item is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_fresh_extraction_in_migrated_chat_still_records_new_self_report(system):
    engine, profiles = system
    memory_id = await source(engine)
    await profiles.upsert_facts(owner(), [fact()], source_memory_id=memory_id)
    old, entered, release = extracting(profiles, memory_id)
    fresh = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        profiles.config_manager.values["user_profile.scope_mode"] = "session"
        assert len(await profiles.get_profile(owner())) == 1
        assert await profiles.delete_profile(owner()) == 1
        release.set()
        assert await asyncio.wait_for(old, 5) == 0
        fresh_id = await source(engine)
        fresh, fresh_entered, fresh_release = extracting(profiles, fresh_id)
        await asyncio.wait_for(fresh_entered.wait(), 5)
        fresh_release.set()
        assert await asyncio.wait_for(fresh, 5) == 1
        assert (await profiles.get_profile(owner()))[0]["source_memory_id"] == fresh_id
    finally:
        release.set()
        if fresh is not None:
            fresh_release.set()
        await asyncio.gather(*(item for item in (old, fresh) if item is not None), return_exceptions=True)
