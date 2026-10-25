"""Web source replacement must agree with profile details and directory counts."""

import time

import pytest
from astrbot_plugin_anamnesis.core.page_api_modules.memory_handler import MemoryHandler
from astrbot_plugin_anamnesis.core.page_api_modules.utils import PageApiUtils
from quart import Quart

from tests.test_profile_migration_followup import fact, owner
from tests.test_profile_observation_lifecycle import source

pytest_plugins = ["tests.test_profile_observation_lifecycle"]


async def web_edit(engine, memory_id, field="content"):
    app = Quart(__name__)
    handler = MemoryHandler(PageApiUtils())
    values = {
        "content": "source summary edited by administrator",
        "topics": ["updated topic"],
        "key_facts": ["updated fact"],
        "structured": {"content": "updated summary", "topics": ["updated topic"]},
    }
    async with app.test_request_context("/memories/update", method="POST", json={
        "memory_id": memory_id, "field": field, "value": values[field]
    }):
        result = await handler.update_memory(engine)
    assert result["status"] == "ok", result
    assert await engine.get_memory(memory_id) is None
    return result["data"]["new_memory_id"]


async def profile_row(profiles):
    return (await profiles.list_for_web())["items"][0]


async def add_observation(engine, profiles, event, *, key="drink", value="喜欢茶"):
    memory_id = await engine.add_memory(content=fact(value)["evidence"],
                                        session_id=event.unified_msg_origin, persona_id=None)
    assert await profiles.upsert_facts(event, [{**fact(value), "key": key}],
                                      source_memory_id=memory_id) == 1
    return memory_id


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_mode", ["user", "session"])
@pytest.mark.parametrize("field", ["content", "topics", "key_facts", "structured"])
async def test_directory_counts_match_details_after_real_source_replacement(system, scope_mode, field):
    engine, profiles = system
    profiles.config_manager.values["user_profile.scope_mode"] = scope_mode
    memory_id = await add_observation(engine, profiles, owner())
    before = await profile_row(profiles)
    new_id = await web_edit(engine, memory_id, field)
    detail = await profile_row(profiles)
    assert detail["suppressed"] == 1 and detail["live_evidence_count"] == 0
    assert detail["source_memory_id"] is None and detail["source_session_id"] is None
    assert detail["profile_scope"] == before["profile_scope"] and detail["identity"] == before["identity"]
    assert {item["source_memory_id"] for item in detail["evidence"]} == {memory_id}
    assert new_id not in {item["source_memory_id"] for item in detail["evidence"]}
    assert await profiles.get_profile(owner()) == []
    user = (await profiles.list_users_for_web())["users"][0]
    assert user["fact_count"] == 1 and user["active_fact_count"] == 0
    assert user["suppressed_fact_count"] == 1 and user["expired_fact_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining_suppressed", [False, True])
async def test_current_source_uses_only_remaining_live_evidence(system, remaining_suppressed):
    engine, profiles = system
    first = await add_observation(engine, profiles, owner())
    second = await add_observation(engine, profiles, owner("2"))
    before = await profile_row(profiles)
    if remaining_suppressed:
        await engine.archive_memories([first])
    await web_edit(engine, second)
    row = await profile_row(profiles)
    assert row["profile_scope"] == before["profile_scope"] and row["identity"] == before["identity"]
    assert row["context_session_id"] == owner("2").unified_msg_origin
    if remaining_suppressed:
        assert row["suppressed"] == 1 and row["source_memory_id"] is None
        assert row["source_session_id"] is None
    else:
        assert row["live_evidence_count"] == 1 and row["source_memory_id"] == first
        assert row["source_session_id"] == owner().unified_msg_origin and row["source_group_id"] == "1"
        assert {item["source_memory_id"] for item in row["evidence"] if item["active"]} == {first}
    user = (await profiles.list_users_for_web())["users"][0]
    assert user["active_fact_count"] == int(not remaining_suppressed)
    assert user["suppressed_fact_count"] == int(remaining_suppressed)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_mode", ["user", "session"])
async def test_independent_manual_fact_survives_source_replacement(system, scope_mode):
    engine, profiles = system
    profiles.config_manager.values["user_profile.scope_mode"] = scope_mode
    memory_id = await add_observation(engine, profiles, owner())
    row = await profile_row(profiles)
    await profiles.update_for_web(row["profile_scope"], "drink", {"value": "管理员确认的饮品偏好"})
    await web_edit(engine, memory_id)
    row = await profile_row(profiles)
    assert row["manual_override"] and row["live_evidence_count"] == 1 and row["suppressed"] == 0
    assert row["source_memory_id"] is None
    assert "管理员确认的饮品偏好" in await profiles.format_for_injection(owner())
    user = (await profiles.list_users_for_web())["users"][0]
    assert user["active_fact_count"] == 1 and user["suppressed_fact_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_mode", ["user", "session"])
async def test_archive_restore_keeps_directory_and_detail_state_consistent(system, scope_mode):
    engine, profiles = system
    profiles.config_manager.values["user_profile.scope_mode"] = scope_mode
    memory_id = await source(engine)
    await profiles.upsert_facts(owner(), [fact()], source_memory_id=memory_id)
    for archived in (True, False):
        if archived:
            assert await engine.archive_memories([memory_id]) == 1
        else:
            assert await engine.restore_memory(memory_id)
        row = await profile_row(profiles)
        user = (await profiles.list_users_for_web())["users"][0]
        assert row["suppressed"] == int(archived)
        assert user["active_fact_count"] == int(not archived)
        assert user["suppressed_fact_count"] == int(archived)
        assert row["source_memory_id"] == (None if archived else memory_id)


@pytest.mark.asyncio
async def test_directory_counts_live_manual_expired_and_unsupported_facts(system):
    engine, profiles = system
    await add_observation(engine, profiles, owner(), key="live")
    deleted = await add_observation(engine, profiles, owner(), key="unsupported")
    await web_edit(engine, deleted)
    for key in ("manual", "expired"):
        await add_observation(engine, profiles, owner(), key=key)
        row = next(item for item in (await profiles.list_for_web())["items"] if item["profile_key"] == key)
        await profiles.update_for_web(row["profile_scope"], key, {
            "value": "管理员独立确认", "expires_at": time.time() - 60 if key == "expired" else None
        })
    rows = (await profiles.list_for_web())["items"]
    assert len(rows) == 4
    user = (await profiles.list_users_for_web())["users"][0]
    assert user["fact_count"] == 4 and user["active_fact_count"] == 2
    assert user["expired_fact_count"] == 1 and user["suppressed_fact_count"] == 1


@pytest.mark.asyncio
async def test_replacing_one_account_source_does_not_change_another_accounts_counts(system):
    engine, profiles = system
    alice_id = await add_observation(engine, profiles, owner())
    bob = owner()
    bob.get_sender_id = lambda: "bob"
    bob.get_sender_name = lambda: "Bob"
    bob_id = await add_observation(engine, profiles, bob)
    await web_edit(engine, alice_id)
    users = {user["sender_id"]: user for user in (await profiles.list_users_for_web())["users"]}
    assert users["alice"]["active_fact_count"] == 0 and users["alice"]["suppressed_fact_count"] == 1
    assert users["bob"]["active_fact_count"] == 1 and users["bob"]["suppressed_fact_count"] == 0
    assert (await profiles.get_profile(bob))[0]["source_memory_id"] == bob_id
