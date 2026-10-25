"""Normal API audit of published 3.5.7 follow-up operations."""

import asyncio
import json
from types import MethodType

import pytest

from tests.test_profile_migration_followup import current, fact, owner

pytest_plugins = ["tests.test_profile_migration_followup"]


async def prune_357(self, connection, scope, key):
    """Published a438c083 history cleanup, kept locally for upgrade regressions."""
    limit = self._ledger_limit("history_max_versions", 20, 100)
    async with connection.execute(
        "SELECT claim_id FROM user_profile_claims WHERE profile_scope=? AND profile_key=? "
        "AND state NOT IN ('current','pending') ORDER BY claim_id DESC LIMIT -1 OFFSET ?",
        (scope, key, limit),
    ) as cursor:
        expired = [row[0] for row in await cursor.fetchall()]
    async with connection.execute(
        "SELECT claim_id FROM user_profile_claims WHERE profile_scope=? AND profile_key=? "
        "AND state='pending' ORDER BY claim_id DESC LIMIT -1 OFFSET 3",
        (scope, key),
    ) as cursor:
        expired += [row[0] for row in await cursor.fetchall()]
    for claim_id in expired:
        await connection.execute(
            "DELETE FROM user_profile_evidence WHERE claim_id=?", (claim_id,)
        )
        await connection.execute(
            "DELETE FROM user_profile_claims WHERE claim_id=?", (claim_id,)
        )
    await connection.execute(
        "DELETE FROM user_profile_transitions WHERE profile_scope=? AND profile_key=? AND transition_id NOT IN "
        "(SELECT transition_id FROM user_profile_transitions WHERE profile_scope=? AND profile_key=? ORDER BY transition_id DESC LIMIT ?)",
        (scope, key, scope, key, limit * 3),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("published_cleanup", [False, True])
async def test_source_deleted_placeholder_survives_history_prune_and_scope_migration(
    manager, monkeypatch, published_cleanup
):
    manager.config_manager.values["user_profile.history_max_versions"] = 1
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=11)
    await manager.delete_by_source_memory(10)
    row = await current(manager)
    assert row["value"] == "[来源已删除]"
    await manager.review_for_web(
        row["profile_scope"],
        "drink",
        claim_id=row["pending"][0]["claim_id"],
        action="reject",
        expected_revision=row["revision"],
    )
    if published_cleanup:
        # Replay the released cleanup through its ordinary writer API. This
        # verifies upgrade recovery without fabricating an inconsistent row.
        monkeypatch.setattr(manager, "_prune_ledger", MethodType(prune_357, manager))
    await manager.upsert_facts(owner(), [fact("喜欢可可")], source_memory_id=12)
    if published_cleanup:
        monkeypatch.delattr(manager, "_prune_ledger")
    assert (await current(manager))["pending"][0]["can_accept"]
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    assert await manager.get_profile(owner()) == []
    row = await current(manager)
    assert row["pending"][0]["value"] == "喜欢可可"
    async with manager.store.connection.execute(
        "SELECT state FROM user_profile_claims WHERE claim_id=?",
        (row["current_claim_id"],),
    ) as cursor:
        assert (await cursor.fetchone())[0] == "withdrawn"
    accepted = await manager.review_for_web(
        row["profile_scope"],
        "drink",
        claim_id=row["pending"][0]["claim_id"],
        action="accept",
        expected_revision=row["revision"],
    )
    assert accepted["value"] == "喜欢可可"
    assert len(accepted["history"]) <= 1


@pytest.mark.asyncio
async def test_accept_migrated_manual_proposal_keeps_administrator_metadata(manager):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=12)
    shared = await current(manager)
    updated = await manager.update_for_web(
        shared["profile_scope"],
        "drink",
        {
            "category": "status",
            "display_label": "管理员确认的饮品",
            "confidence": 0.82,
        },
    )
    assert updated["category"] == "status"
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    pending = row["pending"][0]
    assert pending["authority"] == "manual"
    accepted = await manager.review_for_web(
        row["profile_scope"],
        "drink",
        claim_id=pending["claim_id"],
        action="accept",
        expected_revision=row["revision"],
    )
    assert {
        key: accepted[key] for key in ("category", "display_label", "confidence")
    } == {key: updated[key] for key in ("category", "display_label", "confidence")}


@pytest.mark.asyncio
async def test_unprotected_accept_keeps_migrated_support_after_evidence_budget_lowered(
    manager,
):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    for source in (11, 12):
        await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=source)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    manager.config_manager.values.update(
        {
            "user_profile.manual_override_enabled": False,
            "user_profile.evidence_max_sources": 1,
        }
    )
    row = await current(manager)
    accepted = await manager.review_for_web(
        row["profile_scope"],
        "drink",
        claim_id=row["pending"][0]["claim_id"],
        action="accept",
        expected_revision=row["revision"],
    )
    assert {item["source_memory_id"] for item in accepted["evidence"]} == {11, 12}
    await manager.delete_by_source_memory(11)
    assert (await manager.get_profile(owner()))[0]["source_memory_id"] == 12


@pytest.mark.asyncio
async def test_manual_migration_delete_then_unlock_has_no_grant_revival(manager):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact()], source_memory_id=11)
    shared = await current(manager)
    await manager.update_for_web(shared["profile_scope"], "drink", {"value": "喜欢茶"})
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    await manager.delete_by_source_memory(10)
    await manager.delete_by_source_memory(11)
    row = await current(manager)
    assert row["manual_override"] and {
        item["source_memory_id"] for item in row["evidence"]
    } == {None}
    unlocked = await manager.review_for_web(
        row["profile_scope"],
        "drink",
        action="unlock",
        expected_revision=row["revision"],
    )
    assert not unlocked["manual_override"]
    manager.config_manager.values["user_profile.review_changes"] = False
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=12)
    assert (await current(manager))["value"] == "喜欢咖啡"


@pytest.mark.asyncio
async def test_same_value_manual_grant_to_automatic_current_keeps_selected_fact_fields(
    manager,
):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact()], source_memory_id=11)
    shared = await current(manager)
    selected = await manager.update_for_web(
        shared["profile_scope"],
        "drink",
        {
            "category": "status",
            "display_label": "管理员选择",
            "confidence": 0.82,
            "expires_at": 9999999999.0,
        },
    )
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    assert row["authority"] == "manual" and row["manual_override"]
    assert {
        key: row[key]
        for key in ("value", "category", "display_label", "confidence", "expires_at")
    } == {
        key: selected[key]
        for key in ("value", "category", "display_label", "confidence", "expires_at")
    }
    async with manager.store.connection.execute(
        "SELECT authority,snapshot FROM user_profile_claims WHERE claim_id=?",
        (row["current_claim_id"],),
    ) as cursor:
        claim = await cursor.fetchone()
    snapshot = json.loads(claim["snapshot"])
    assert claim["authority"] == snapshot["authority"] == "manual"
    assert {
        key: snapshot[key]
        for key in ("value", "category", "display_label", "confidence", "expires_at")
    } == {
        key: row[key]
        for key in ("value", "category", "display_label", "confidence", "expires_at")
    }
    await manager.delete_by_source_memory(10)
    await manager.delete_by_source_memory(11)
    row = (await manager.get_profile(owner()))[0]
    assert (
        row["display_label"] == selected["display_label"]
        and row["category"] == selected["category"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", [False, True])
async def test_manual_merge_keeps_existing_target_fact_selection(manager, protected):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    manager.config_manager.values["user_profile.manual_override_enabled"] = protected
    selected = await manager.update_for_web(
        row["profile_scope"],
        "drink",
        {"display_label": "本会话选择", "confidence": 0.86},
    )
    manager.config_manager.values["user_profile.manual_override_enabled"] = True
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact()], source_memory_id=11)
    shared = await current(manager)
    await manager.update_for_web(
        shared["profile_scope"],
        "drink",
        {
            "category": "status",
            "display_label": "较新的共享选择",
            "confidence": 0.82,
        },
    )
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    assert bool(row["manual_override"]) is protected
    assert {key: row[key] for key in ("category", "display_label", "confidence")} == {
        key: selected[key] for key in ("category", "display_label", "confidence")
    }


@pytest.mark.asyncio
async def test_foreign_automatic_support_does_not_inherit_manual_fact_fields_or_grant(
    manager,
):
    original = {**fact(), "label": "本会话自动偏好"}
    await manager.upsert_facts(owner("1"), [original], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner("1"))
    selected = await current(manager, "1")
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=11)
    shared = await current(manager, "2")
    await manager.update_for_web(
        shared["profile_scope"],
        "drink",
        {
            "category": "status",
            "display_label": "其他群人工选择",
            "confidence": 0.82,
        },
    )
    await manager.upsert_facts(owner("1"), [original], source_memory_id=12)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner("1"))
    row = await current(manager, "1")
    assert not row["manual_override"] and row["authority"] == "direct_user"
    assert {key: row[key] for key in ("category", "display_label", "confidence")} == {
        key: selected[key] for key in ("category", "display_label", "confidence")
    }


@pytest.mark.asyncio
async def test_existing_manual_pending_selection_precedes_later_shared_manual_fields(
    manager,
):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    selections = (
        {
            "category": "status",
            "display_label": "已选事实",
            "confidence": 0.82,
            "expires_at": 9999999999.0,
        },
        {
            "category": "preference",
            "display_label": "较新的共享字段",
            "confidence": 0.86,
            "expires_at": None,
        },
    )
    for index, fields in enumerate(selections):
        manager.config_manager.values["user_profile.scope_mode"] = "user"
        await manager.upsert_facts(
            owner(), [fact("喜欢咖啡")], source_memory_id=12 + index
        )
        shared = await current(manager)
        await manager.update_for_web(shared["profile_scope"], "drink", fields)
        manager.config_manager.values["user_profile.scope_mode"] = "session"
        await manager.get_profile(owner())
    row = await current(manager)
    proposal = row["pending"][0]
    assert proposal["authority"] == "manual"
    accepted = await manager.review_for_web(
        row["profile_scope"],
        "drink",
        claim_id=proposal["claim_id"],
        action="accept",
        expected_revision=row["revision"],
    )
    assert {field: accepted[field] for field in selections[0]} == selections[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accept", "reject"])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_review_final_cleanup_failure_or_cancellation_rolls_back_and_retries(
    manager, monkeypatch, action, error
):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    before = await current(manager)
    proposal = before["pending"][0]
    prune = manager._prune_ledger

    async def interrupted(connection, scope, key):
        await prune(connection, scope, key)
        async with connection.execute(
            "SELECT state FROM user_profile_claims WHERE claim_id=?",
            (proposal["claim_id"],),
        ) as cursor:
            claim = await cursor.fetchone()
        if claim["state"] == ("accepted" if action == "accept" else "rejected"):
            raise error("review final cleanup interrupted")

    monkeypatch.setattr(manager, "_prune_ledger", interrupted)
    with pytest.raises(error):
        await manager.review_for_web(
            before["profile_scope"],
            "drink",
            claim_id=proposal["claim_id"],
            action=action,
            expected_revision=before["revision"],
        )
    after = await current(manager)
    assert {
        field: after[field]
        for field in ("value", "current_claim_id", "revision", "manual_override")
    } == {
        field: before[field]
        for field in ("value", "current_claim_id", "revision", "manual_override")
    }
    assert after["pending"] == before["pending"]
    assert after["history"] == before["history"]
    assert after["evidence"] == before["evidence"]
    monkeypatch.delattr(manager, "_prune_ledger")
    retried = await manager.review_for_web(
        before["profile_scope"],
        "drink",
        claim_id=proposal["claim_id"],
        action=action,
        expected_revision=before["revision"],
    )
    assert retried["pending"] == []
    assert retried["value"] == ("喜欢咖啡" if action == "accept" else "喜欢茶")
