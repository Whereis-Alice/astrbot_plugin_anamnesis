"""Profile authority, evidence and review boundaries use real SQLite."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore


class Config:
    def __init__(self, **values):
        self.values = {"user_profile.enabled": True, **values}

    def get(self, key, default=None):
        return self.values.get(key, default)


def event(sender="alice", session="qq:GroupMessage:42", name="相同昵称"):
    return SimpleNamespace(
        unified_msg_origin=session, get_sender_id=lambda: sender,
        get_sender_name=lambda: name, get_platform_name=lambda: "qq",
        get_group_id=lambda: "42",
        message_obj=SimpleNamespace(group=SimpleNamespace(group_id="42", group_name="读书群")))


def fact(value="喜欢茶", key="drink", **extra):
    return {"key": key, "value": value, "category": "preference", "explicit": True,
            "confidence": .95, "evidence": "我喜欢喝茶", **extra}


@pytest_asyncio.fixture
async def manager(tmp_path):
    store = ConversationStore(str(tmp_path / "profiles.db"))
    await store.initialize()
    result = UserProfileManager(Config(), SimpleNamespace(store=store))
    try:
        yield result
    finally:
        await store.close()


async def current(manager, owner=None):
    return (await manager.list_for_web(profile_scope=manager.scope_for_event(owner or event())))["items"][0]


@pytest.mark.asyncio
async def test_default_changes_are_proposals_and_never_injected(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    await manager.upsert_facts(event(), [fact("不再喝茶", evidence="我现在不再喝茶")], source_memory_id=11)
    row = await current(manager)
    assert row["value"] == "喜欢茶"
    assert [item["value"] for item in row["pending"]] == ["不再喝茶"]
    assert "不再喝茶" not in await manager.format_for_injection(event())
    assert row["authority"] == "direct_user" and not row["manual_override"]


@pytest.mark.asyncio
async def test_manual_priority_survives_disable_and_future_extraction(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    await manager.update_for_web(manager.scope_for_event(event()), "drink", {"value": "喜欢咖啡"})
    manager.config_manager.values.update({"user_profile.review_changes": False, "user_profile.manual_override_enabled": False})
    await manager.upsert_facts(event(), [fact("喜欢可乐")], source_memory_id=11)
    row = await current(manager)
    assert row["value"] == "喜欢咖啡" and row["manual_override"] == 1
    assert row["authority"] == "manual" and row["pending"][0]["value"] == "喜欢可乐"
    assert row["history"][0]["value"] == "喜欢茶"
    assert row["history"][0]["evidence"][0]["quote"] == "我喜欢喝茶"


@pytest.mark.asyncio
async def test_same_value_keeps_multiple_support_and_only_last_delete_retracts(manager):
    for source in (10, 11):
        await manager.upsert_facts(event(), [fact()], source_memory_id=source)
    assert len((await current(manager))["evidence"]) == 2
    assert await manager.delete_by_source_memory(11) == 0
    assert (await manager.get_profile(event()))[0]["value"] == "喜欢茶"
    assert await manager.delete_by_source_memory(10) == 1
    assert await manager.get_profile(event()) == []
    async with manager.store.connection.execute("SELECT value,snapshot FROM user_profile_claims") as cursor:
        assert all(row[0] == "[来源已删除]" and row[1] == "{}" for row in await cursor.fetchall())


@pytest.mark.asyncio
async def test_archive_tombstones_only_last_support_and_restore_does_not_revive_history(manager):
    for source in (10, 11):
        await manager.upsert_facts(event(), [fact()], source_memory_id=source)
    await manager.set_source_suppressed([10], True)
    assert len(await manager.get_profile(event())) == 1
    await manager.set_source_suppressed([11], True)
    assert await manager.get_profile(event()) == []
    await manager.set_source_suppressed([10], False)
    assert len(await manager.get_profile(event())) == 1
    await manager.update_for_web(manager.scope_for_event(event()), "drink", {"value": "人工纠正"})
    await manager.set_source_suppressed([11], False)
    assert (await manager.get_profile(event()))[0]["value"] == "人工纠正"


@pytest.mark.asyncio
async def test_live_source_check_checks_all_evidence_before_capacity(manager):
    for source in (10, 11):
        await manager.upsert_facts(event(), [fact()], source_memory_id=source)
    connection = manager.store.connection
    await connection.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY,metadata TEXT)")
    await connection.execute("INSERT INTO documents VALUES(10,'{\"status\":\"active\"}'),(11,'{\"status\":\"archived\"}')")
    await connection.commit()
    manager.memory_engine = SimpleNamespace(db_connection=connection)
    row = (await manager.get_profile(event()))[0]
    assert row["live_evidence_count"] == 1 and row["value"] == "喜欢茶"


@pytest.mark.asyncio
async def test_manual_confirmation_is_independent_of_removed_historical_source(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    await manager.update_for_web(manager.scope_for_event(event()), "drink", {"value": "管理员确认"})
    assert await manager.delete_by_source_memory(10) == 0
    row = (await manager.get_profile(event()))[0]
    assert row["value"] == "管理员确认" and row["source_memory_id"] is None
    assert row["evidence"][0]["source_memory_id"] is None


@pytest.mark.asyncio
async def test_review_is_revision_bound_and_account_bound(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    await manager.upsert_facts(event(), [fact("咖啡")], source_memory_id=11)
    row = await current(manager)
    claim = row["pending"][0]["claim_id"]
    assert await manager.review_for_web(row["profile_scope"], "drink", claim_id=claim, action="accept", expected_revision=row["revision"] - 1) is None
    await manager.upsert_facts(event("bob"), [fact("其他人的资料")], source_memory_id=12)
    bob = await current(manager, event("bob"))
    assert await manager.review_for_web(bob["profile_scope"], "drink", claim_id=claim, action="accept", expected_revision=bob["revision"]) is None
    result = await manager.review_for_web(row["profile_scope"], "drink", claim_id=claim, action="accept", expected_revision=row["revision"])
    assert result["value"] == "咖啡" and result["manual_override"] == 1
    assert not result["pending"]
    assert {item["state"] for item in result["history"]} == {"accepted", "superseded"}


@pytest.mark.asyncio
async def test_accept_rejects_expired_or_deleted_source_proposal(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    await manager.upsert_facts(event(), [fact("暂时变化", category="status")], source_memory_id=11)
    row = await current(manager)
    claim = row["pending"][0]["claim_id"]
    await manager.set_source_suppressed([11], True)
    row = await current(manager)
    assert row["pending"][0]["can_accept"] is False
    assert await manager.review_for_web(row["profile_scope"], "drink", claim_id=claim, action="accept", expected_revision=row["revision"]) is None
    await manager.set_source_suppressed([11], False)
    await manager.store.connection.execute("UPDATE user_profile_claims SET snapshot=json_set(snapshot,'$.expires_at',?) WHERE claim_id=?", (time.time()-1, claim))
    await manager.store.connection.commit()
    row = await current(manager)
    assert await manager.review_for_web(row["profile_scope"], "drink", claim_id=claim, action="accept", expected_revision=row["revision"]) is None


@pytest.mark.asyncio
async def test_review_rejection_and_unlock_do_not_change_current_value(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    await manager.update_for_web(manager.scope_for_event(event()), "drink", {"value": "人工值"})
    await manager.upsert_facts(event(), [fact("候选值")], source_memory_id=11)
    row = await current(manager)
    result = await manager.review_for_web(row["profile_scope"], "drink", claim_id=row["pending"][0]["claim_id"], action="reject", expected_revision=row["revision"])
    assert result["value"] == "人工值" and not result["pending"]
    result = await manager.review_for_web(row["profile_scope"], "drink", action="unlock", expected_revision=result["revision"])
    assert result["value"] == "人工值" and result["manual_override"] == 0


@pytest.mark.asyncio
async def test_set_preferences_coexist_while_same_value_deduplicates(manager):
    for value in ("喜欢茶", "喜欢咖啡", "喜欢茶"):
        await manager.upsert_facts(event(), [fact(value, cardinality="set")], source_memory_id=10)
    rows = await manager.get_profile(event())
    assert {row["value"] for row in rows} == {"喜欢茶", "喜欢咖啡"}
    assert len({row["profile_key"] for row in rows}) == 2


@pytest.mark.asyncio
async def test_history_and_proposals_are_bounded(manager):
    manager.config_manager.values.update({"user_profile.history_max_versions": 2, "user_profile.review_changes": False})
    for number in range(8):
        await manager.upsert_facts(event(), [fact(str(number))], source_memory_id=number+1)
    assert len((await current(manager))["history"]) == 2
    manager.config_manager.values["user_profile.review_changes"] = True
    for number in range(8, 15):
        await manager.upsert_facts(event(), [fact(str(number))], source_memory_id=number+1)
    assert len((await current(manager))["pending"]) == 3


@pytest.mark.asyncio
async def test_alias_never_merges_same_nickname_accounts_and_is_searchable(manager):
    for owner in (event(), event("bob")):
        await manager.upsert_facts(owner, [fact()], source_memory_id=10)
    users = (await manager.list_users_for_web())["users"]
    alice = next(user for user in users if user["sender_id"] == "alice")
    assert await manager.set_alias_for_web("unknown", "私有备注") is None
    assert await manager.set_alias_for_web(alice["identity"], "我的队友")
    result = await manager.list_users_for_web(query="我的队友")
    assert result["total"] == 1 and result["users"][0]["sender_id"] == "alice"
    assert (await manager.list_users_for_web())["total"] == 2
    assert await manager.set_alias_for_web(alice["identity"], "")


@pytest.mark.asyncio
async def test_ledger_write_failure_rolls_back_projection_evidence_and_history(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    manager._transition = AsyncMock(side_effect=RuntimeError("interrupted write"))
    with pytest.raises(RuntimeError):
        await manager.update_for_web(manager.scope_for_event(event()), "drink", {"value": "不得提交"})
    row = await current(manager)
    assert row["value"] == "喜欢茶" and not row["history"] and not row["manual_override"]
    async with manager.store.connection.execute("SELECT COUNT(*) FROM user_profile_claims") as cursor:
        assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_exact_profile_delete_removes_pending_evidence_and_history(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    await manager.upsert_facts(event(), [fact("候选")], source_memory_id=11)
    await manager.delete_for_web(manager.scope_for_event(event()), "drink")
    for table in ("user_profile_claims", "user_profile_evidence", "user_profile_transitions"):
        async with manager.store.connection.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
            assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_source_deletion_keeps_supported_proposal_reviewable_without_old_content(manager):
    await manager.upsert_facts(event(), [fact("旧资料")], source_memory_id=10)
    await manager.upsert_facts(event(), [fact("新说法")], source_memory_id=11)
    assert await manager.delete_by_source_memory(10) == 1
    assert await manager.get_profile(event()) == []
    row = await current(manager)
    assert row["value"] == "[来源已删除]" and row["pending"][0]["value"] == "新说法"
    result = await manager.review_for_web(row["profile_scope"], "drink", claim_id=row["pending"][0]["claim_id"], action="accept", expected_revision=row["revision"])
    assert result["value"] == "新说法" and result["manual_override"] == 1


@pytest.mark.asyncio
async def test_replaying_evidence_does_not_renew_ttl_or_reopen_rejected_proposal(manager):
    await manager.upsert_facts(event(), [fact(category="status")], source_memory_id=10)
    row = await current(manager)
    await manager.upsert_facts(event(), [fact(category="status")], source_memory_id=10)
    replay = await current(manager)
    assert replay["revision"] == row["revision"] and replay["expires_at"] == row["expires_at"]
    await manager.upsert_facts(event(), [fact("不再喝茶")], source_memory_id=11)
    row = await current(manager)
    result = await manager.review_for_web(row["profile_scope"], "drink", claim_id=row["pending"][0]["claim_id"], action="reject", expected_revision=row["revision"])
    await manager.upsert_facts(event(), [fact("不再喝茶")], source_memory_id=11)
    row = await current(manager)
    assert not row["pending"] and row["revision"] == result["revision"]


@pytest.mark.asyncio
async def test_fresh_evidence_renews_an_expired_pending_status_without_replay(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    await manager.upsert_facts(event(), [fact("最近失眠", category="status")], source_memory_id=11)
    row = await current(manager)
    claim = row["pending"][0]["claim_id"]
    expired = int(time.time()) - 100
    await manager.store.connection.execute(
        "UPDATE user_profile_claims SET snapshot=json_set(snapshot,'$.expires_at',?) WHERE claim_id=?", (expired, claim))
    await manager.store.connection.commit()
    await manager.upsert_facts(event(), [fact("最近失眠", category="status")], source_memory_id=11)
    assert (await current(manager))["pending"][0]["expires_at"] == expired

    await manager.upsert_facts(event(), [fact("最近失眠", category="status")], source_memory_id=12)
    renewed = await current(manager)
    pending = renewed["pending"][0]
    assert pending["claim_id"] == claim
    assert pending["expires_at"] > time.time() and pending["can_accept"]
    assert len(pending["evidence"]) == 2
    result = await manager.review_for_web(renewed["profile_scope"], "drink", claim_id=claim,
                                          action="accept", expected_revision=renewed["revision"])
    assert result["value"] == "最近失眠"


@pytest.mark.asyncio
async def test_unprotected_acceptance_uses_the_candidates_source_not_the_old_fact(manager):
    manager.config_manager.values["user_profile.manual_override_enabled"] = False
    await manager.upsert_facts(event(), [fact("旧资料")], source_memory_id=10)
    await manager.upsert_facts(event(), [fact("新说法")], source_memory_id=11)
    row = await current(manager)
    result = await manager.review_for_web(row["profile_scope"], "drink", claim_id=row["pending"][0]["claim_id"],
                                          action="accept", expected_revision=row["revision"])
    assert result["value"] == "新说法" and not result["manual_override"]
    assert result["source_memory_id"] == 11
    assert {item["source_memory_id"] for item in result["evidence"]} == {11}
    assert await manager.delete_by_source_memory(10) == 0
    assert (await manager.get_profile(event()))[0]["value"] == "新说法"
    assert await manager.delete_by_source_memory(11) == 1
    assert await manager.get_profile(event()) == []
    async with manager.store.connection.execute("SELECT value,snapshot FROM user_profile_claims") as cursor:
        assert all(item[0] == "[来源已删除]" and json.loads(item[1]) == {} for item in await cursor.fetchall())
