"""Malformed explicit cleanup keys cannot widen deletion or extraction fencing."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore

from tests.test_profile_migration_followup import fact, owner

pytest_plugins = ["tests.test_profile_observation_lifecycle"]


async def add_fact(engine, profiles, event, key, value="喜欢茶"):
    observation = {**fact(value), "key": key}
    memory_id = await engine.add_memory(
        content=observation["evidence"],
        session_id=event.unified_msg_origin,
        persona_id=None,
    )
    assert (
        await profiles.upsert_facts(event, [observation], source_memory_id=memory_id)
        == 1
    )
    return memory_id


async def populate(engine, profiles, scope_mode):
    profiles.config_manager.values["user_profile.scope_mode"] = scope_mode
    alice, another_chat, bob = owner(), owner("2"), owner()
    bob.get_sender_id = lambda: "bob"
    bob.get_sender_name = lambda: "Bob"
    await add_fact(engine, profiles, alice, "drink")
    await add_fact(engine, profiles, alice, "books", "喜欢读书")
    await add_fact(engine, profiles, another_chat, "other_chat", "喜欢游戏")
    await add_fact(engine, profiles, bob, "drink", "喜欢咖啡")
    return alice, another_chat, bob


async def ledger_snapshot(profiles):
    result = {}
    for table in (
        "user_profiles",
        "user_profile_claims",
        "user_profile_evidence",
        "user_profile_transitions",
    ):
        async with profiles.store.connection.execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        ) as cursor:
            result[table] = [dict(row) for row in await cursor.fetchall()]
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_mode", ["user", "session"])
@pytest.mark.parametrize("key", [" ", "\t", " \n ", "invalid key", "x" * 65])
async def test_invalid_explicit_key_preserves_all_profiles_and_ledger(
    system, scope_mode, key
):
    engine, profiles = system
    alice, _, _ = await populate(engine, profiles, scope_mode)
    before = await ledger_snapshot(profiles)
    assert await profiles.delete_profile(alice, key) == 0
    assert await ledger_snapshot(profiles) == before
    assert len((await profiles.list_for_web())["items"]) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_mode", ["user", "session"])
async def test_trimmed_valid_key_deletes_exact_key_and_preserves_other_scopes(
    system, scope_mode
):
    engine, profiles = system
    alice, another_chat, bob = await populate(engine, profiles, scope_mode)
    assert await profiles.delete_profile(alice, " DRINK ") == 1
    assert {row["profile_key"] for row in await profiles.get_profile(alice)} == (
        {"books", "other_chat"} if scope_mode == "user" else {"books"}
    )
    assert "other_chat" in {
        row["profile_key"] for row in await profiles.get_profile(another_chat)
    }
    assert [row["value"] for row in await profiles.get_profile(bob)] == ["喜欢咖啡"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_mode", ["user", "session"])
@pytest.mark.parametrize("key", [None, ""])
async def test_omitted_or_empty_key_keeps_whole_calling_scope_contract(
    system, scope_mode, key
):
    engine, profiles = system
    alice, another_chat, bob = await populate(engine, profiles, scope_mode)
    assert await profiles.delete_profile(alice, key) == (
        3 if scope_mode == "user" else 2
    )
    assert await profiles.get_profile(alice) == []
    assert bool(await profiles.get_profile(another_chat)) == (scope_mode == "session")
    assert [row["value"] for row in await profiles.get_profile(bob)] == ["喜欢咖啡"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [" ", "\t"])
async def test_invalid_key_preserves_facts_after_reopening_sqlite(system, key):
    engine, profiles = system
    alice, _, _ = await populate(engine, profiles, "session")
    assert await profiles.delete_profile(alice, key) == 0
    db_path = profiles.store.db_path
    await profiles.store.close()
    reopened = ConversationStore(db_path)
    await reopened.initialize()
    fresh = UserProfileManager(profiles.config_manager, SimpleNamespace(store=reopened))
    fresh.memory_engine = engine
    try:
        assert {row["profile_key"] for row in await fresh.get_profile(alice)} == {
            "drink",
            "books",
        }
        assert len((await fresh.list_for_web())["items"]) == 4
    finally:
        await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_mode", ["user", "session"])
@pytest.mark.parametrize("key", [" \t", " DRINK "])
async def test_same_validated_key_controls_deletion_and_inflight_fact_fencing(
    system, scope_mode, key
):
    engine, profiles = system
    alice, _, bob = await populate(engine, profiles, scope_mode)
    observations = [fact(), {**fact("喜欢读书"), "key": "books"}]
    text = "。".join(item["evidence"] for item in observations)
    memory_id = await engine.add_memory(
        content=text, session_id=alice.unified_msg_origin, persona_id=None
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def llm(*args, **kwargs):
        entered.set()
        await release.wait()
        return json.dumps({"facts": observations}, ensure_ascii=False)

    extracting = asyncio.create_task(
        profiles.update_from_messages(
            alice,
            [{"role": "user", "sender_id": "alice", "content": text}],
            SimpleNamespace(_call_llm_with_retry=llm),
            source_memory_id=memory_id,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        valid = bool(key.strip())
        assert await profiles.delete_profile(alice, key) == int(valid)
        release.set()
        assert await asyncio.wait_for(extracting, 5) == (1 if valid else 2)
        current = {row["profile_key"] for row in await profiles.get_profile(alice)}
        assert ("drink" in current) == (not valid)
        assert "books" in current
        assert [row["value"] for row in await profiles.get_profile(bob)] == ["喜欢咖啡"]
    finally:
        release.set()
        if not extracting.done():
            extracting.cancel()
        await asyncio.gather(extracting, return_exceptions=True)
