"""Independent red tests for profile candidate acceptance and session migration."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore


class Config:
    def __init__(self):
        self.values = {"user_profile.enabled": True, "user_profile.scope_mode": "user"}

    def get(self, key, default=None):
        return self.values.get(key, default)


def owner(group):
    return SimpleNamespace(
        unified_msg_origin=f"qq:GroupMessage:{group}",
        get_sender_id=lambda: "alice",
        get_sender_name=lambda: "Alice",
        get_platform_name=lambda: "qq",
        get_group_id=lambda: group,
        message_obj=SimpleNamespace(
            group=SimpleNamespace(group_id=group, group_name=group)
        ),
    )


def fact(value="喜欢茶", quote="我喜欢喝茶"):
    return {
        "key": "drink",
        "value": value,
        "category": "preference",
        "confidence": 0.95,
        "explicit": True,
        "evidence": quote,
    }


@pytest_asyncio.fixture
async def manager(tmp_path):
    store = ConversationStore(str(tmp_path / "profiles.db"))
    await store.initialize()
    manager = UserProfileManager(Config(), SimpleNamespace(store=store))
    try:
        yield manager
    finally:
        await store.close()


async def current(manager, group="2"):
    return (
        await manager.list_for_web(profile_scope=manager.scope_for_event(owner(group)))
    )["items"][0]


@pytest.mark.asyncio
async def test_session_migration_does_not_keep_foreign_current_evidence(manager):
    await manager.upsert_facts(owner("1"), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    assert len(await manager.get_profile(owner("2"))) == 1
    await manager.delete_by_source_memory(11)
    assert await manager.get_profile(owner("2")) == []


@pytest.mark.asyncio
async def test_session_migration_does_not_make_foreign_pending_claim_acceptable(
    manager,
):
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=10)
    await manager.upsert_facts(
        owner("1"),
        [fact("只在群1说过的偏好", "我只在群1说过这个偏好")],
        source_memory_id=11,
    )
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner("2"))
    row = await current(manager)
    foreign = next(
        (claim for claim in row["pending"] if claim["value"] == "只在群1说过的偏好"),
        None,
    )
    if foreign is None:
        return
    accepted = await manager.review_for_web(
        row["profile_scope"],
        "drink",
        claim_id=foreign["claim_id"],
        action="accept",
        expected_revision=row["revision"],
    )
    assert accepted is None


@pytest.mark.asyncio
async def test_acceptance_keeps_existing_support_when_evidence_limit_is_lowered(
    manager,
):
    manager.config_manager.values["user_profile.manual_override_enabled"] = False
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=10)
    for source in (11, 12):
        await manager.upsert_facts(
            owner("2"), [fact("喜欢咖啡", "我现在喜欢喝咖啡")], source_memory_id=source
        )
    row = await current(manager)
    manager.config_manager.values["user_profile.evidence_max_sources"] = 1
    accepted = await manager.review_for_web(
        row["profile_scope"],
        "drink",
        claim_id=row["pending"][0]["claim_id"],
        action="accept",
        expected_revision=row["revision"],
    )
    assert accepted["value"] == "喜欢咖啡"
    await manager.delete_by_source_memory(12)
    assert [row["value"] for row in await manager.get_profile(owner("2"))] == [
        "喜欢咖啡"
    ]


@pytest.mark.asyncio
async def test_accepting_live_proposal_reactivates_directory_projection(manager):
    manager.config_manager.values["user_profile.manual_override_enabled"] = False
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=10)
    await manager.upsert_facts(
        owner("2"), [fact("喜欢咖啡", "我现在喜欢喝咖啡")], source_memory_id=11
    )
    await manager.set_source_suppressed([10], True)
    row = await current(manager)
    accepted = await manager.review_for_web(
        row["profile_scope"],
        "drink",
        claim_id=row["pending"][0]["claim_id"],
        action="accept",
        expected_revision=row["revision"],
    )
    assert accepted["suppressed"] == 0
    assert (await manager.get_profile(owner("2")))[0]["value"] == "喜欢咖啡"
    user = (await manager.list_users_for_web())["users"][0]
    assert user["active_fact_count"] == 1 and user["suppressed_fact_count"] == 0


@pytest.mark.asyncio
async def test_session_filter_keeps_same_session_live_proposal_after_current_source_is_archived(
    manager,
):
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=10)
    await manager.upsert_facts(
        owner("2"), [fact("喜欢咖啡", "我现在喜欢喝咖啡")], source_memory_id=11
    )
    await manager.set_source_suppressed([10], True)
    row = await current(manager)
    assert row["pending"][0]["can_accept"]
    session = owner("2").unified_msg_origin
    assert (await manager.list_for_web(source_session_id=session))["total"] == 1
    assert (await manager.list_users_for_web(source_session_id=session))["total"] == 1


@pytest.mark.asyncio
async def test_split_current_support_preserves_other_session_and_evidence_timeline(
    manager,
):
    await manager.upsert_facts(owner("1"), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=11)
    before = {
        item["source_memory_id"]: (
            item["evidence_id"],
            item["created_at"],
            item["quote"],
        )
        for item in (await current(manager))["evidence"]
    }
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    first, second = await asyncio.gather(
        manager.get_profile(owner("1")), manager.get_profile(owner("2"))
    )
    assert [item["source_memory_id"] for item in first[0]["evidence"]] == [10]
    assert [item["source_memory_id"] for item in second[0]["evidence"]] == [11]
    for item in (*first[0]["evidence"], *second[0]["evidence"]):
        assert (item["evidence_id"], item["created_at"], item["quote"]) == before[
            item["source_memory_id"]
        ]
    assert first[0]["context_session_id"] == owner("1").unified_msg_origin
    assert second[0]["context_session_id"] == owner("2").unified_msg_origin


@pytest.mark.asyncio
async def test_foreign_pending_stays_reviewable_in_its_own_session_after_split(manager):
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=10)
    await manager.upsert_facts(
        owner("1"), [fact("喜欢咖啡", "我现在喜欢喝咖啡")], source_memory_id=11
    )
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner("2"))
    assert (await current(manager))["pending"] == []
    assert await manager.get_profile(owner("1")) == []
    row = await current(manager, "1")
    claim = row["pending"][0]
    assert claim["value"] == "喜欢咖啡" and claim["can_accept"]
    assert [item["source_memory_id"] for item in claim["evidence"]] == [11]
    accepted = await manager.review_for_web(
        row["profile_scope"],
        "drink",
        claim_id=claim["claim_id"],
        action="accept",
        expected_revision=row["revision"],
    )
    assert accepted["value"] == "喜欢咖啡"
    assert (await manager.get_profile(owner("2")))[0]["value"] == "喜欢茶"


@pytest.mark.asyncio
async def test_split_shared_pending_keeps_only_its_own_sources_and_independent_review(
    manager,
):
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=10)
    for group, source in (("1", 11), ("2", 12)):
        await manager.upsert_facts(
            owner(group),
            [fact("喜欢咖啡", "我现在喜欢喝咖啡")],
            source_memory_id=source,
        )
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner("2"))
    second = await current(manager)
    assert [item["source_memory_id"] for item in second["pending"][0]["evidence"]] == [
        12
    ]
    await manager.get_profile(owner("1"))
    first = await current(manager, "1")
    assert [item["source_memory_id"] for item in first["pending"][0]["evidence"]] == [
        11
    ]
    await manager.delete_by_source_memory(12)
    assert (await current(manager, "1"))["pending"][0]["can_accept"]
    assert (await current(manager))["pending"] == []


@pytest.mark.asyncio
async def test_historical_only_chat_remains_visible_without_reviving_history(manager):
    manager.config_manager.values["user_profile.review_changes"] = False
    await manager.upsert_facts(owner("1"), [fact()], source_memory_id=10)
    await manager.upsert_facts(
        owner("2"), [fact("喜欢咖啡", "我现在喜欢喝咖啡")], source_memory_id=11
    )
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    assert await manager.get_profile(owner("1")) == []
    first = await current(manager, "1")
    assert first["history"][0]["value"] == "喜欢茶"
    assert first["history"][0]["evidence"][0]["source_memory_id"] == 10
    assert (await manager.get_profile(owner("2")))[0]["value"] == "喜欢咖啡"
    assert (
        await manager.list_for_web(source_session_id=owner("1").unified_msg_origin)
    )["total"] == 1


@pytest.mark.asyncio
async def test_protected_independent_manual_support_migrates_only_with_its_actual_chat(
    manager,
):
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=10)
    row = await current(manager)
    await manager.update_for_web(
        row["profile_scope"], "drink", {"value": "管理员确认的偏好"}
    )
    await manager.upsert_facts(
        owner("1"), [fact("喜欢咖啡", "我现在喜欢喝咖啡")], source_memory_id=11
    )
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    second = (await manager.get_profile(owner("2")))[0]
    assert (
        second["manual_override"] and second["evidence"][0]["source_memory_id"] is None
    )
    await manager.delete_by_source_memory(10)
    assert (await manager.get_profile(owner("2")))[0]["value"] == "管理员确认的偏好"
    assert await manager.get_profile(owner("1")) == []
    assert (await current(manager, "1"))["pending"][0]["value"] == "喜欢咖啡"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_group", ["1", "2"])
async def test_shared_manual_current_does_not_grant_protection_to_foreign_automatic_support(
    manager, first_group
):
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=10)
    row = await current(manager)
    await manager.update_for_web(row["profile_scope"], "drink", {"value": "喜欢咖啡"})
    await manager.upsert_facts(
        owner("1"), [fact("喜欢咖啡", "我现在喜欢喝咖啡")], source_memory_id=11
    )
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner(first_group))
    first = (await manager.get_profile(owner("1")))[0]
    assert not first["manual_override"] and first["authority"] == "direct_user"
    assert [item["source_memory_id"] for item in first["evidence"]] == [11]
    second = (await manager.get_profile(owner("2")))[0]
    assert second["manual_override"] and second["authority"] == "manual"
    assert [item["source_memory_id"] for item in second["evidence"]] == [None]
    await manager.delete_by_source_memory(11)
    assert await manager.get_profile(owner("1")) == []
    assert (await manager.get_profile(owner("2")))[0]["value"] == "喜欢咖啡"


@pytest.mark.asyncio
async def test_existing_target_current_precedes_migrated_value_without_losing_other_chat(
    manager,
):
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner("1"), [fact()], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.upsert_facts(
        owner("2"), [fact("喜欢咖啡", "我现在喜欢喝咖啡")], source_memory_id=12
    )
    assert (await manager.get_profile(owner("2")))[0]["value"] == "喜欢咖啡"
    second = await current(manager)
    assert any(
        item["value"] == "喜欢茶" and item["evidence"][0]["source_memory_id"] == 10
        for item in second["history"]
    )
    assert (await manager.get_profile(owner("1")))[0]["value"] == "喜欢茶"


@pytest.mark.asyncio
async def test_partial_evidence_move_failure_rolls_back_all_scopes_and_sources(
    manager, monkeypatch
):
    await manager.upsert_facts(owner("1"), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=11)
    row = await current(manager)
    execute_many = manager.store.connection.executemany

    async def interrupted(sql, parameters):
        result = await execute_many(sql, parameters)
        if sql.startswith("UPDATE user_profile_evidence SET claim_id"):
            raise RuntimeError("evidence migration interrupted")
        return result

    monkeypatch.setattr(manager.store.connection, "executemany", interrupted)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    with pytest.raises(RuntimeError, match="interrupted"):
        await manager.get_profile(owner("2"))
    legacy = (await manager.list_for_web(profile_scope=row["profile_scope"]))["items"][
        0
    ]
    assert [item["source_memory_id"] for item in legacy["evidence"]] == [11, 10]
    assert (
        await manager.list_for_web(profile_scope=manager.scope_for_event(owner("2")))
    )["total"] == 0
    assert legacy["revision"] == row["revision"]


@pytest.mark.asyncio
async def test_user_filter_follows_current_and_pending_support_not_recent_context_or_history(
    manager,
):
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=10)
    await manager.touch_source_context(owner("1"))
    session = owner("1").unified_msg_origin
    assert (await manager.list_for_web(source_session_id=session))["total"] == 0
    assert (await manager.list_users_for_web(source_session_id=session))["total"] == 0
    await manager.upsert_facts(
        owner("1"), [fact("喜欢咖啡", "我现在喜欢喝咖啡")], source_memory_id=11
    )
    assert (await manager.list_for_web(source_session_id=session))["total"] == 1
    assert (await manager.list_users_for_web(source_session_id=session))["total"] == 1
    await manager.set_source_suppressed([11], True)
    assert (await manager.list_for_web(source_session_id=session))["total"] == 0
