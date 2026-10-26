"""Manual adoption holds preserve real SQLite claims, evidence and scope fences."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.core.page_api import PluginPageApi
from quart import Quart, g

from tests.test_profile_migration_followup import owner
from tests.test_profile_observation_lifecycle import system as _system
from tests.test_user_profile_ledger import current, event, fact
from tests.test_user_profile_ledger import manager as _manager

manager = _manager
system = _system


async def hold(profiles, row, action="pause", **kwargs):
    return await profiles.set_quarantine_for_web(
        row["profile_scope"],
        row["profile_key"],
        current_claim_id=row["current_claim_id"],
        expected_revision=row["revision"],
        action=action,
        actor="webui:Alice",
        reason="来源不足，等待核实",
        **kwargs,
    )


async def ledger_state(profiles):
    result = {}
    for table in ("user_profile_claims", "user_profile_evidence"):
        async with profiles.store.connection.execute(
            f"SELECT * FROM {table}"
        ) as cursor:
            result[table] = [dict(row) for row in await cursor.fetchall()]
    return result


@pytest.mark.asyncio
async def test_pause_keeps_original_assertion_and_support_but_excludes_injection_and_preview(
    manager,
):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    before = await current(manager)
    ledger = await ledger_state(manager)
    paused = await hold(manager, before)
    assert paused["quarantined"] == 1 and not paused["adopted"]
    assert paused["quarantine_reason"] == "来源不足，等待核实"
    assert paused["quarantined_at"] > 0 and paused["revision"] == before["revision"] + 1
    for field in (
        "value",
        "authority",
        "current_claim_id",
        "confidence",
        "updated_at",
        "evidence",
        "history",
        "manual_override",
    ):
        assert paused[field] == before[field]
    assert await ledger_state(manager) == ledger
    assert await manager.get_profile(event()) == []
    assert await manager.format_for_injection(event()) == ""
    directory = await manager.list_users_for_web()
    assert directory["total"] == 1
    assert directory["users"][0]["active_fact_count"] == 0
    assert directory["users"][0]["quarantined_fact_count"] == 1
    assert directory["users"][0]["fact_preview"] == []
    resumed = await hold(manager, paused, "resume")
    assert not resumed["quarantined"] and resumed["adopted"]
    assert resumed["quarantine_reason"] == "" and resumed["quarantined_at"] is None
    assert await ledger_state(manager) == ledger
    assert (await manager.get_profile(event()))[0]["value"] == before["value"]
    assert [row["action"] for row in resumed["adoption_history"]] == [
        "resumed",
        "quarantined",
    ]
    assert all(row["actor"] == "webui:Alice" for row in resumed["adoption_history"])
    assert resumed["adoption_history"][1]["reason"] == "来源不足，等待核实"


@pytest.mark.asyncio
async def test_hold_replay_is_idempotent_only_with_current_claim_and_revision(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    before = await current(manager)
    paused = await hold(manager, before)
    assert await hold(manager, before) is None
    replay = await hold(manager, paused)
    assert replay["revision"] == paused["revision"]
    assert replay["adoption_history"] == paused["adoption_history"]
    resumed = await hold(manager, replay, "resume")
    repeated = await hold(manager, resumed, "resume")
    assert repeated["revision"] == resumed["revision"]
    assert repeated["adoption_history"] == resumed["adoption_history"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["expired", "suppressed"])
async def test_resume_does_not_clear_source_suppression_or_override_expiry(
    manager, lifecycle
):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    paused = await hold(manager, await current(manager))
    if lifecycle == "expired":
        await manager.update_for_web(
            paused["profile_scope"], paused["profile_key"], {"expires_at": 100.0}
        )
    else:
        await manager.set_source_suppressed([10], True)
    row = await current(manager)
    resumed = await hold(manager, row, "resume")
    assert not resumed["quarantined"] and not resumed["adopted"]
    assert resumed["expires_at"] == row["expires_at"]
    assert resumed["suppressed"] == row["suppressed"]
    assert resumed["authority"] == row["authority"]
    assert await manager.get_profile(event()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "same_source",
        "new_source",
        "sources",
        "label",
        "value",
        "accept",
        "reject",
        "unlock",
        "automatic_replace",
    ],
)
async def test_ordinary_writes_do_not_resume_the_administrator_hold(manager, operation):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    paused = await hold(manager, await current(manager))
    if operation in {"same_source", "new_source"}:
        await manager.upsert_facts(
            event(), [fact()], source_memory_id=10 if operation == "same_source" else 11
        )
    elif operation == "sources":
        await manager.set_source_suppressed([10], True)
        await manager.set_source_suppressed([10], False)
    elif operation in {"label", "value"}:
        await manager.update_for_web(
            paused["profile_scope"],
            paused["profile_key"],
            {"display_label": "人工标题"}
            if operation == "label"
            else {"value": "人工确认的真实事实"},
        )
    elif operation == "automatic_replace":
        manager.config_manager.values["user_profile.review_changes"] = False
        await manager.upsert_facts(event(), [fact("新的自动事实")], source_memory_id=11)
    elif operation == "unlock":
        row = await manager.update_for_web(
            paused["profile_scope"], paused["profile_key"], {"value": "人工值"}
        )
        await manager.review_for_web(
            row["profile_scope"],
            row["profile_key"],
            action="unlock",
            expected_revision=row["revision"],
        )
    else:
        await manager.upsert_facts(event(), [fact("候选值")], source_memory_id=11)
        row = await current(manager)
        await manager.review_for_web(
            row["profile_scope"],
            row["profile_key"],
            action=operation,
            claim_id=row["pending"][0]["claim_id"],
            expected_revision=row["revision"],
        )
    row = await current(manager)
    assert (
        row["quarantined"] and row["quarantine_reason"] == paused["quarantine_reason"]
    )
    assert row["quarantined_at"] == paused["quarantined_at"]
    assert await manager.get_profile(event()) == []
    assert [entry["action"] for entry in row["adoption_history"]] == ["quarantined"]


@pytest.mark.asyncio
async def test_actual_memory_archive_restore_cannot_resume_a_hold(system):
    engine, profiles = system
    memory = await engine.add_memory(
        content="我明确说过喜欢茶",
        session_id=owner().unified_msg_origin,
        persona_id=None,
    )
    await profiles.upsert_facts(owner(), [fact()], source_memory_id=memory)
    row = (await profiles.list_for_web())["items"][0]
    await hold(profiles, row)
    assert await engine.update_memory(memory, {"status": "archived"})
    assert await engine.update_memory(memory, {"status": "active"})
    row = (await profiles.list_for_web())["items"][0]
    assert row["quarantined"] and row["live_evidence_count"] == 1
    assert await profiles.get_profile(owner()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["current_claim_id", "expected_revision"])
@pytest.mark.parametrize("value", [True, False, None, 0, -1, 1.0, "1", 2**63, [], {}])
async def test_hold_rejects_malformed_integer_fences_without_writes(
    manager, field, value
):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    row = await current(manager)
    params = {
        "current_claim_id": row["current_claim_id"],
        "expected_revision": row["revision"],
        "action": "pause",
        "actor": "webui:Alice",
    }
    params[field] = value
    assert (
        await manager.set_quarantine_for_web(
            row["profile_scope"], row["profile_key"], **params
        )
        is None
    )
    assert not (await current(manager))["quarantined"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_scope", True),
        ("profile_scope", []),
        ("profile_key", {}),
        ("profile_key", "drink%"),
        ("action", True),
        ("action", "delete"),
        ("reason", {}),
        ("reason", "x" * 501),
        ("actor", "automatic"),
        ("actor", "webui:"),
    ],
)
async def test_hold_rejects_invalid_selectors_action_reason_and_actor(
    manager, field, value
):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    row = await current(manager)
    params = {
        "profile_scope": row["profile_scope"],
        "profile_key": row["profile_key"],
        "current_claim_id": row["current_claim_id"],
        "expected_revision": row["revision"],
        "action": "pause",
        "actor": "webui:Alice",
    }
    params[field] = value
    assert await manager.set_quarantine_for_web(**params) is None
    assert not (await current(manager))["quarantined"]


@pytest.mark.asyncio
async def test_changed_claim_other_scope_and_delete_recreate_refuse_old_hold(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    original = await current(manager)
    await manager.upsert_facts(event("bob"), [fact()], source_memory_id=11)
    bob = await current(manager, event("bob"))
    assert (
        await manager.set_quarantine_for_web(
            bob["profile_scope"],
            bob["profile_key"],
            current_claim_id=original["current_claim_id"],
            expected_revision=bob["revision"],
            action="pause",
            actor="webui:Alice",
        )
        is None
    )
    changed = await manager.update_for_web(
        original["profile_scope"], original["profile_key"], {"value": "更正"}
    )
    assert (
        await manager.set_quarantine_for_web(
            changed["profile_scope"],
            changed["profile_key"],
            current_claim_id=original["current_claim_id"],
            expected_revision=changed["revision"],
            action="pause",
            actor="webui:Alice",
        )
        is None
    )
    await manager.delete_for_web(changed["profile_scope"], changed["profile_key"])
    assert await hold(manager, changed) is None
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    recreated = await current(manager)
    assert await hold(manager, original) is None
    assert recreated["current_claim_id"] != original["current_claim_id"]


@pytest.mark.asyncio
async def test_hold_transaction_failure_rolls_back_state_revision_and_transition(
    manager,
):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    row = await current(manager)
    manager._transition = AsyncMock(side_effect=RuntimeError("write failed"))
    with pytest.raises(RuntimeError, match="write failed"):
        await hold(manager, row)
    restored = await current(manager)
    assert not restored["quarantined"] and restored["revision"] == row["revision"]
    assert restored["adoption_history"] == []


@pytest.mark.asyncio
async def test_actual_sqlite_transition_failure_rolls_back_projection(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    before = await current(manager)
    await manager.store.connection.execute(
        "CREATE TRIGGER refuse_pause BEFORE INSERT ON user_profile_transitions "
        "WHEN NEW.action='quarantined' BEGIN SELECT RAISE(ABORT,'hold write refused'); END"
    )
    await manager.store.connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="hold write refused"):
        await hold(manager, before)
    after = await current(manager)
    assert not after["quarantined"] and after["revision"] == before["revision"]
    assert after["adoption_history"] == []


@pytest.mark.asyncio
async def test_integer_revision_ceiling_refuses_mutation_without_overflow(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    await manager.store.connection.execute(
        "UPDATE user_profiles SET revision=?", (2**63 - 1,)
    )
    await manager.store.connection.commit()
    row = await current(manager)
    assert await hold(manager, row) is None
    after = await current(manager)
    assert after["revision"] == 2**63 - 1 and not after["quarantined"]


@pytest.mark.asyncio
async def test_reopen_migrates_legacy_schema_and_preserves_pause(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    paused = await hold(manager, await current(manager))
    reopened = UserProfileManager(
        manager.config_manager, SimpleNamespace(store=manager.store)
    )
    assert (await current(reopened))["quarantined_at"] == paused["quarantined_at"]
    assert await reopened.get_profile(event()) == []
    for field in ("quarantined", "quarantine_reason", "quarantined_at"):
        await manager.store.connection.execute(
            f"ALTER TABLE user_profiles DROP COLUMN {field}"
        )
    await manager.store.connection.execute(
        "ALTER TABLE user_profile_transitions DROP COLUMN reason"
    )
    await manager.store.connection.commit()
    legacy = UserProfileManager(
        manager.config_manager, SimpleNamespace(store=manager.store)
    )
    row = await current(legacy)
    assert not row["quarantined"] and row["quarantine_reason"] == ""
    assert (await hold(legacy, row))["quarantined"]


@pytest.mark.asyncio
async def test_session_split_keeps_pause_from_row_instead_of_old_claim_snapshot(
    manager,
):
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    await manager.upsert_facts(
        event(session="qq:GroupMessage:99"), [fact()], source_memory_id=11
    )
    paused = await hold(manager, await current(manager))
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    assert await manager.get_profile(event()) == []
    migrated = await current(manager)
    assert (
        migrated["quarantined"]
        and migrated["quarantine_reason"] == paused["quarantine_reason"]
    )
    assert migrated["quarantined_at"] == paused["quarantined_at"]
    assert migrated["adoption_history"][0]["reason"] == paused["quarantine_reason"]
    assert migrated["adoption_history"][0]["actor"] == "webui:Alice"
    assert await manager.get_profile(event(session="qq:GroupMessage:99")) == []


@pytest.mark.asyncio
async def test_no_automatic_keyword_scanner_pauses_manual_facts(manager):
    await manager.upsert_facts(event(), [fact("未婚，喜欢茶")], source_memory_id=10)
    updated = await manager.update_for_web(
        manager.scope_for_event(event()), "drink", {"value": "已婚，喜欢咖啡"}
    )
    assert not updated["quarantined"] and updated["authority"] == "manual"
    assert (await manager.get_profile(event()))[0]["value"] == "已婚，喜欢咖啡"


@pytest.mark.asyncio
async def test_capacity_cleanup_cannot_remove_holds_or_allow_later_reactivation(manager):
    await manager.upsert_facts(
        event(), [fact(key="drink"), fact("喜欢书", key="books")], source_memory_id=10
    )
    original = (await manager.list_for_web())["items"]
    for row in original:
        await hold(manager, row)
    # Lowering the normal fact budget must still preserve every manual hold.
    manager.config_manager.values["user_profile.max_items"] = 1
    await manager.upsert_facts(event(), [fact("喜欢跑步", key="sport")], source_memory_id=11)
    rows = {row["profile_key"]: row for row in (await manager.list_for_web())["items"]}
    assert set(rows) == {"drink", "books", "sport"}
    assert rows["drink"]["quarantined"] and rows["books"]["quarantined"]
    assert [row["profile_key"] for row in await manager.get_profile(event())] == ["sport"]
    await manager.upsert_facts(event(), [fact(key="drink")], source_memory_id=12)
    await manager.upsert_facts(event(), [fact("喜欢音乐", key="music")], source_memory_id=13)
    rows = {row["profile_key"]: row for row in (await manager.list_for_web())["items"]}
    assert set(rows) == {"drink", "books", "music"}
    for before in original:
        row = rows[before["profile_key"]]
        assert row["quarantined"] and row["value"] == before["value"]
        assert row["current_claim_id"] == before["current_claim_id"]
        assert row["adoption_history"][0]["action"] == "quarantined"


@pytest.mark.asyncio
async def test_api_uses_trusted_quart_actor_ignores_json_actor_and_reports_failures(
    manager,
):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    row = await current(manager)
    api = PluginPageApi(SimpleNamespace(user_profile_manager=manager))
    api._ensure_plugin_ready = AsyncMock(return_value=({}, None))
    app = Quart(__name__)

    @app.before_request
    async def login():
        g.username = "trusted-admin"

    app.add_url_rule("/hold", view_func=api.quarantine_profile, methods=["POST"])
    payload = {
        "profile_scope": row["profile_scope"],
        "profile_key": row["profile_key"],
        "current_claim_id": row["current_claim_id"],
        "expected_revision": row["revision"],
        "action": "pause",
        "actor": "webui:forged",
        "reason": "核查来源",
    }
    client = app.test_client()
    response = await (await client.post("/hold", json=payload)).get_json()
    assert response["status"] == "ok"
    assert response["data"]["adoption_history"][0]["actor"] == "webui:trusted-admin"
    stale = await (await client.post("/hold", json=payload)).get_json()
    assert stale["status"] == "error"
    invalid = await (await client.post("/hold", json=[])).get_json()
    assert invalid["status"] == "error"
    manager.set_quarantine_for_web = AsyncMock(
        side_effect=RuntimeError("database locked")
    )
    failed = await (await client.post("/hold", json=payload)).get_json()
    assert failed["status"] == "error"
