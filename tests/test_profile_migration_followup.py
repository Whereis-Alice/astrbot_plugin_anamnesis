"""Normal API regressions after profile migration and repeated scope changes."""

import asyncio
from types import MethodType, SimpleNamespace

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.managers import user_profile_manager
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore


class Config:
    def __init__(self):
        self.values = {"user_profile.enabled": True, "user_profile.scope_mode": "user",
                       "user_profile.verify_real_world_facts": False}

    def get(self, key, default=None):
        return self.values.get(key, default)


def owner(group="1"):
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


def fact(value="喜欢茶"):
    return {
        "key": "drink",
        "value": value,
        "evidence": f"我明确说过{value}",
        "explicit": True,
        "confidence": 0.95,
        "category": "preference",
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


async def current(manager, group="1"):
    return (
        await manager.list_for_web(profile_scope=manager.scope_for_event(owner(group)))
    )["items"][0]


async def legacy_migrate_355(self, event, scope):
    """Reproduce the published 67f270e5 whole-claim migration via its normal API.

    Kept locally so a shallow CI checkout needs neither history nor network.
    The faulty version copied the source-selected row and moved all its claims
    while leaving each evidence's original source_context unchanged.
    """
    session = event.unified_msg_origin
    previous = scope[: -len(":session:" + session)]
    db = self.store.connection
    async with self.store._write_lock:
        try:
            async with db.execute(
                "SELECT * FROM user_profiles WHERE profile_scope=? AND source_session_id=?",
                (previous, session),
            ) as cursor:
                rows = await cursor.fetchall()
            for record in rows:
                row = dict(record)
                row["profile_scope"] = scope
                await self._project_claim(
                    db,
                    row,
                    row["current_claim_id"],
                    row["authority"],
                    revision=row["revision"],
                    manual=bool(row["manual_override"]),
                )
                await db.execute(
                    "UPDATE user_profile_claims SET profile_scope=?,snapshot=json_set(snapshot,'$.profile_scope',?) WHERE profile_scope=? AND profile_key=?",
                    (scope, scope, previous, row["profile_key"]),
                )
                await db.execute(
                    "UPDATE user_profile_transitions SET profile_scope=? WHERE profile_scope=? AND profile_key=?",
                    (scope, previous, row["profile_key"]),
                )
                await db.execute(
                    "DELETE FROM user_profiles WHERE profile_scope=? AND profile_key=?",
                    (previous, row["profile_key"]),
                )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def old_misplaced_state(manager, monkeypatch):
    await manager.upsert_facts(owner("1"), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    monkeypatch.setattr(
        manager,
        "_migrate_legacy_scope_for_event",
        MethodType(legacy_migrate_355, manager),
    )
    assert len((await manager.get_profile(owner("2")))[0]["evidence"]) == 2
    monkeypatch.delattr(manager, "_migrate_legacy_scope_for_event")


@pytest.mark.asyncio
async def test_scope_roundtrip_same_value_new_source_keeps_current_support(manager):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    assert len(await manager.get_profile(owner())) == 1
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact()], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    assert len(await manager.get_profile(owner())) == 1
    await manager.delete_by_source_memory(10)
    assert [row["value"] for row in await manager.get_profile(owner())] == ["喜欢茶"]


@pytest.mark.asyncio
async def test_scope_roundtrip_conflicting_new_current_stays_reviewable(manager):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    assert any(
        claim["value"] == "喜欢咖啡" and claim["can_accept"] for claim in row["pending"]
    )


@pytest.mark.asyncio
async def test_scope_roundtrip_same_pending_value_joins_one_alternative(manager):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact()], source_memory_id=12)
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=13)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    pending = [
        claim
        for claim in (await current(manager))["pending"]
        if claim["value"] == "喜欢咖啡"
    ]
    assert len(pending) == 1
    assert {item["source_memory_id"] for item in pending[0]["evidence"]} == {11, 13}


@pytest.mark.asyncio
async def test_upgrade_repairs_foreign_sources_migrated_by_published_355(
    manager, monkeypatch
):
    # Replay the published version's normal migration API to create actual
    # legacy state, rather than writing an invented inconsistent SQLite row.
    await old_misplaced_state(manager, monkeypatch)
    await manager.delete_by_source_memory(11)
    assert await manager.get_profile(owner("2")) == []
    assert [
        row["source_memory_id"] for row in await manager.get_profile(owner("1"))
    ] == [10]


@pytest.mark.asyncio
async def test_fresh_manager_web_first_read_repairs_old_misplaced_sources(
    manager, monkeypatch
):
    await old_misplaced_state(manager, monkeypatch)
    fresh = UserProfileManager(
        manager.config_manager, SimpleNamespace(store=manager.store)
    )
    rows = (await fresh.list_for_web())["items"]
    assert {row["profile_scope"] for row in rows} == {
        fresh.scope_for_event(owner("1")),
        fresh.scope_for_event(owner("2")),
    }
    for row in rows:
        assert all(
            row["profile_scope"].endswith(":session:" + item["source_session_id"])
            for item in row["evidence"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_schema_repair_failure_or_cancellation_rolls_back_and_can_retry(
    manager, monkeypatch, error
):
    await old_misplaced_state(manager, monkeypatch)
    fresh = UserProfileManager(
        manager.config_manager, SimpleNamespace(store=manager.store)
    )
    execute_many = manager.store.connection.executemany

    async def interrupted(sql, parameters):
        result = await execute_many(sql, parameters)
        if sql.startswith("UPDATE user_profile_evidence SET claim_id"):
            raise error("repair interrupted")
        return result

    monkeypatch.setattr(manager.store.connection, "executemany", interrupted)
    with pytest.raises(error):
        await fresh.list_for_web()
    assert not fresh._schema_ready
    old = (await manager.list_for_web())["items"]
    assert len(old) == 1 and len(old[0]["evidence"]) == 2
    monkeypatch.setattr(manager.store.connection, "executemany", execute_many)
    assert len((await fresh.list_for_web())["items"]) == 2


@pytest.mark.asyncio
async def test_rejected_same_source_quote_is_not_reproposed_by_scope_roundtrip(manager):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    await manager.review_for_web(
        row["profile_scope"],
        "drink",
        claim_id=row["pending"][0]["claim_id"],
        action="reject",
        expected_revision=row["revision"],
    )
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    assert (await current(manager))["pending"] == []
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=12)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    assert {
        item["source_memory_id"]
        for item in (await current(manager))["pending"][0]["evidence"]
    } == {12}


@pytest.mark.asyncio
async def test_same_value_merge_uses_saved_observation_expiry_without_migration_clock(
    manager, monkeypatch
):
    clock = [1000000.0]
    monkeypatch.setattr(user_profile_manager.time, "time", lambda: clock[0])
    volatile = {**fact("正在读书"), "category": "status"}
    await manager.upsert_facts(owner(), [volatile], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    original = (await current(manager))["expires_at"]
    clock[0] += 86400
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [volatile], source_memory_id=11)
    incoming = (await current(manager))["expires_at"]
    assert incoming > original
    clock[0] += 86400
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    assert (await current(manager))["expires_at"] == incoming
    clock[0] += 86400
    await manager.get_profile(owner())
    assert (await current(manager))["expires_at"] == incoming


@pytest.mark.asyncio
async def test_same_source_scope_replay_does_not_renew_saved_expiry(
    manager, monkeypatch
):
    clock = [1000000.0]
    monkeypatch.setattr(user_profile_manager.time, "time", lambda: clock[0])
    volatile = {**fact("正在读书"), "category": "status"}
    await manager.upsert_facts(owner(), [volatile], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    original = (await current(manager))["expires_at"]
    clock[0] += 86400
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [volatile], source_memory_id=10)
    clock[0] += 86400
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    assert (await current(manager))["expires_at"] == original


@pytest.mark.asyncio
async def test_same_value_merge_preserves_protected_manual_expiry_and_support(manager):
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    await manager.update_for_web(
        row["profile_scope"], "drink", {"expires_at": 9999999999.0}
    )
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact()], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    assert (
        row["manual_override"]
        and row["authority"] == "manual"
        and row["expires_at"] == 9999999999.0
    )
    assert {item["source_memory_id"] for item in row["evidence"]} == {None, 11}
    await manager.delete_by_source_memory(11)
    assert (await manager.get_profile(owner()))[0]["manual_override"]


@pytest.mark.asyncio
async def test_unknown_legacy_origin_is_kept_without_guessing_from_recent_context(
    manager,
):
    event = owner("2")
    event.unified_msg_origin = ""
    await manager.upsert_facts(event, [fact()], source_memory_id=None)
    row = await current(manager, "2")
    await manager.update_for_web(row["profile_scope"], "drink", {"value": "人工资料"})
    # A user-scope adapter may omit its origin; later context only updates
    # the directory and cannot authorize this fact for the observed group.
    await manager.touch_source_context(owner("2"))
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    fresh = UserProfileManager(
        manager.config_manager, SimpleNamespace(store=manager.store)
    )
    rows = (await fresh.list_for_web())["items"]
    assert len(rows) == 1 and rows[0]["profile_scope"] == row["profile_scope"]
    assert rows[0]["manual_override"]
    assert (await fresh.list_for_web(source_session_id=owner("2").unified_msg_origin))[
        "total"
    ] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("newer", [False, True])
async def test_conflicting_current_uses_saved_observation_order_not_migration_time(
    manager, monkeypatch, newer
):
    clock = [1000000.0]
    monkeypatch.setattr(user_profile_manager.time, "time", lambda: clock[0])
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    if newer:
        await manager.get_profile(owner())
        manager.config_manager.values["user_profile.scope_mode"] = "user"
        clock[0] += 86400
        await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=11)
        manager.config_manager.values["user_profile.scope_mode"] = "session"
    else:
        clock[0] += 86400
        await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=11)
    clock[0] += 86400
    await manager.get_profile(owner())
    row = await current(manager)
    if newer:
        assert row["value"] == "喜欢茶" and row["pending"][0]["value"] == "喜欢咖啡"
    else:
        assert row["value"] == "喜欢咖啡" and row["pending"] == []
        assert any(claim["value"] == "喜欢茶" for claim in row["history"])


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_expiry", [None, 1000000.0 + 3 * 86400])
async def test_same_value_shared_manual_support_inherits_grant_and_saved_expiry(
    manager, monkeypatch, manual_expiry
):
    clock = [1000000.0]
    monkeypatch.setattr(user_profile_manager.time, "time", lambda: clock[0])
    volatile = {**fact("正在读书"), "category": "status"}
    await manager.upsert_facts(owner(), [volatile], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    assert not (await current(manager))["manual_override"]
    clock[0] += 86400
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [volatile], source_memory_id=11)
    shared = await current(manager)
    locked = await manager.update_for_web(
        shared["profile_scope"], "drink", {"expires_at": manual_expiry}
    )
    assert locked["manual_override"] and locked["authority"] == "manual"
    clock[0] += 86400
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    assert row["manual_override"] and row["authority"] == "manual"
    assert row["expires_at"] == manual_expiry
    assert any(item["source_memory_id"] is None for item in row["evidence"])
    # A finite administrator expiry may be shorter than the original automatic
    # TTL; unlimited is also an explicit choice, never a migration-time renewal.
    manager.config_manager.values["user_profile.review_changes"] = False
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=12)
    row = await current(manager)
    assert row["value"] == "正在读书" and row["manual_override"]
    assert row["expires_at"] == manual_expiry
    assert any(claim["value"] == "喜欢咖啡" for claim in row["pending"])
    await manager.delete_by_source_memory(10)
    await manager.delete_by_source_memory(11)
    assert (await manager.get_profile(owner()))[0]["manual_override"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_choice", ["protected", "unlocked", "unprotected"])
@pytest.mark.parametrize("incoming_expiry", [None, 1000000.0 + 6 * 86400])
async def test_same_value_manual_merge_preserves_existing_administrator_choice(
    manager, monkeypatch, target_choice, incoming_expiry
):
    clock = [1000000.0]
    monkeypatch.setattr(user_profile_manager.time, "time", lambda: clock[0])
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    if target_choice == "unprotected":
        manager.config_manager.values["user_profile.manual_override_enabled"] = False
    chosen = await manager.update_for_web(
        row["profile_scope"], "drink", {"expires_at": 1000000.0 + 4 * 86400}
    )
    manager.config_manager.values["user_profile.manual_override_enabled"] = True
    if target_choice == "unlocked":
        chosen = await manager.review_for_web(
            chosen["profile_scope"],
            "drink",
            action="unlock",
            expected_revision=chosen["revision"],
        )
    clock[0] += 86400
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact()], source_memory_id=11)
    shared = await current(manager)
    await manager.update_for_web(
        shared["profile_scope"], "drink", {"expires_at": incoming_expiry}
    )
    clock[0] += 86400
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    assert row["authority"] == "manual"
    assert bool(row["manual_override"]) is (target_choice == "protected")
    assert row["expires_at"] == chosen["expires_at"]
    if target_choice != "protected":
        # A genuinely new automatic observation may still refresh an unlocked
        # volatile statement after a migration, just as the normal writer does.
        await manager.delete_for_web(shared["profile_scope"], "drink")
        manager.config_manager.values["user_profile.scope_mode"] = "user"
        volatile = {**fact(), "category": "status"}
        await manager.upsert_facts(owner(), [volatile], source_memory_id=13)
        automatic = await current(manager)
        manager.config_manager.values["user_profile.scope_mode"] = "session"
        await manager.get_profile(owner())
        assert (await current(manager))["expires_at"] == automatic["expires_at"]
    manager.config_manager.values["user_profile.review_changes"] = False
    await manager.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=12)
    assert (await current(manager))["value"] == (
        "喜欢茶" if target_choice == "protected" else "喜欢咖啡"
    )


@pytest.mark.asyncio
async def test_same_value_automatic_target_does_not_inherit_foreign_manual_grant(
    manager,
):
    await manager.upsert_facts(owner("1"), [fact()], source_memory_id=10)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner("1"))
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner("2"), [fact()], source_memory_id=11)
    shared = await current(manager, "2")
    await manager.update_for_web(shared["profile_scope"], "drink", {"value": "喜欢茶"})
    await manager.upsert_facts(owner("1"), [fact()], source_memory_id=12)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner("1"))
    first = await current(manager, "1")
    assert not first["manual_override"] and first["authority"] == "direct_user"
    assert {item["source_memory_id"] for item in first["evidence"]} == {10, 12}
    second = (await manager.get_profile(owner("2")))[0]
    assert second["manual_override"] and second["authority"] == "manual"
    manager.config_manager.values["user_profile.review_changes"] = False
    await manager.upsert_facts(owner("1"), [fact("喜欢咖啡")], source_memory_id=13)
    assert (await current(manager, "1"))["value"] == "喜欢咖啡"
    assert (await current(manager, "2"))["value"] == "喜欢茶"


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_expiry", [None, 1000000.0 + 3 * 86400])
async def test_same_value_manual_pending_merge_keeps_its_saved_lifetime_and_authority(
    manager, monkeypatch, manual_expiry
):
    clock = [1000000.0]
    monkeypatch.setattr(user_profile_manager.time, "time", lambda: clock[0])
    volatile = {**fact("正在读书"), "category": "status"}
    await manager.upsert_facts(owner(), [fact()], source_memory_id=10)
    await manager.upsert_facts(owner(), [volatile], source_memory_id=11)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    existing = (await current(manager))["pending"][0]
    clock[0] += 86400
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [volatile], source_memory_id=12)
    shared = await current(manager)
    await manager.update_for_web(
        shared["profile_scope"], "drink", {"expires_at": manual_expiry}
    )
    clock[0] += 86400
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    row = await current(manager)
    assert row["value"] == "喜欢茶" and not row["manual_override"]
    assert row["authority"] == "direct_user" and row["expires_at"] is None
    proposal = row["pending"][0]
    assert proposal["claim_id"] == existing["claim_id"]
    assert proposal["authority"] == "manual" and proposal["can_accept"]
    assert proposal["expires_at"] == manual_expiry
    assert any(item["source_memory_id"] is None for item in proposal["evidence"])
    # Further automatic support merges into the same proposal, but cannot
    # replace the saved administrator expiry with a fresh automatic TTL.
    await manager.delete_for_web(shared["profile_scope"], "drink")
    manager.config_manager.values["user_profile.scope_mode"] = "user"
    await manager.upsert_facts(owner(), [fact()], source_memory_id=13)
    await manager.upsert_facts(owner(), [volatile], source_memory_id=14)
    manager.config_manager.values["user_profile.scope_mode"] = "session"
    await manager.get_profile(owner())
    proposal = (await current(manager))["pending"][0]
    assert proposal["authority"] == "manual" and proposal["expires_at"] == manual_expiry
    assert {item["source_memory_id"] for item in proposal["evidence"]} == {None, 11, 14}
    clock[0] = (existing["expires_at"] if manual_expiry is None else manual_expiry) + 1
    row = await current(manager)
    assert row["pending"][0]["can_accept"] is (manual_expiry is None)
    assert row["value"] == "喜欢茶" and not row["manual_override"]
