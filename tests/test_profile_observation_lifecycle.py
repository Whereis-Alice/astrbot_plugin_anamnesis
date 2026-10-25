"""Published 3.5.8 profile observation/lifecycle follow-up audit."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.managers import user_profile_manager
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore

from tests.test_memory_transfer_runtime_safety import _engine
from tests.test_profile_migration_followup import Config, current, fact, owner


@pytest_asyncio.fixture
async def system(tmp_path):
    engine = await _engine(tmp_path / "memories.db")
    engine.atom_enabled = False
    store = ConversationStore(str(tmp_path / "profiles.db"))
    await store.initialize()
    engine.store = store
    profiles = UserProfileManager(Config(), engine)
    profiles.memory_engine = engine
    engine.user_profile_manager = profiles
    try:
        yield engine, profiles
    finally:
        await engine.close()
        await store.close()


async def source(engine, value="喜欢茶"):
    return await engine.add_memory(
        content=fact(value)["evidence"],
        session_id=owner().unified_msg_origin,
        persona_id=None,
    )


def extracting(profiles, memory_id, observation=None):
    observation = observation or fact()
    entered, release = asyncio.Event(), asyncio.Event()

    async def llm(*args, **kwargs):
        entered.set()
        await release.wait()
        return json.dumps({"facts": [observation]}, ensure_ascii=False)

    task = asyncio.create_task(
        profiles.update_from_messages(
            owner(),
            [
                {
                    "role": "user",
                    "sender_id": "alice",
                    "content": observation["evidence"],
                }
            ],
            SimpleNamespace(_call_llm_with_retry=llm),
            source_memory_id=memory_id,
        )
    )
    return task, entered, release


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["current", "pending", "unlocked"])
@pytest.mark.parametrize("manual_expiry", [None, 1000000.0 + 3 * 86400])
async def test_later_llm_observation_preserves_protected_manual_choices_and_respects_unlock(
    system, monkeypatch, manual_expiry, target
):
    engine, profiles = system
    clock = [1000000.0]
    monkeypatch.setattr(user_profile_manager.time, "time", lambda: clock[0])
    coffee, tea = await source(engine, "喜欢咖啡"), await source(engine)
    if target == "pending":
        await profiles.upsert_facts(
            owner(), [fact("喜欢咖啡")], source_memory_id=coffee
        )
    await profiles.upsert_facts(owner(), [fact()], source_memory_id=tea)
    profiles.config_manager.values["user_profile.scope_mode"] = "session"
    await profiles.get_profile(owner())
    profiles.config_manager.values["user_profile.scope_mode"] = "user"
    clock[0] += 86400
    shared_source = await source(engine)
    await profiles.upsert_facts(owner(), [fact()], source_memory_id=shared_source)
    shared = await current(profiles)
    selected = await profiles.update_for_web(
        shared["profile_scope"],
        "drink",
        {
            "category": "status",
            "display_label": "人工选择",
            "confidence": 0.82,
            "expires_at": manual_expiry,
        },
    )
    profiles.config_manager.values["user_profile.scope_mode"] = "session"
    await profiles.get_profile(owner())
    row = await current(profiles)
    selected_row = row["pending"][0] if target == "pending" else row
    assert (
        selected_row["authority"] == "manual"
        and selected_row["expires_at"] == manual_expiry
    )
    if target == "unlocked":
        await profiles.review_for_web(
            row["profile_scope"],
            "drink",
            action="unlock",
            expected_revision=row["revision"],
        )
    clock[0] += 86400
    late_source = await source(engine)
    before_observation = await current(profiles)
    task, entered, release = extracting(
        profiles, late_source, {**fact(), "category": "status"}
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        release.set()
        assert await asyncio.wait_for(task, 5) == 1
        row = await current(profiles)
        assert row["revision"] == before_observation["revision"] + 1
        assert (
            await profiles.update_for_web(
                row["profile_scope"],
                "drink",
                {
                    "display_label": "过期页面编辑",
                    "expected_revision": before_observation["revision"],
                },
            )
            is None
        )
        if target == "unlocked":
            assert not row["manual_override"]
            assert row["expires_at"] == clock[0] + 7 * 86400
            assert row["confidence"] == 0.95 and row["display_label"] is None
            return
        if target == "pending":
            pending = row["pending"][0]
            assert row["value"] == "喜欢咖啡" and not row["manual_override"]
            assert (
                pending["authority"] == "manual"
                and pending["expires_at"] == manual_expiry
            )
            assert (
                await profiles.review_for_web(
                    row["profile_scope"],
                    "drink",
                    claim_id=pending["claim_id"],
                    action="accept",
                    expected_revision=before_observation["revision"],
                )
                is None
            )
            accepted = await profiles.review_for_web(
                row["profile_scope"],
                "drink",
                claim_id=pending["claim_id"],
                action="accept",
                expected_revision=row["revision"],
            )
        else:
            accepted = row
            assert accepted["manual_override"] and accepted["authority"] == "manual"
        assert {
            field: accepted[field]
            for field in ("category", "display_label", "confidence", "expires_at")
        } == {
            field: selected[field]
            for field in ("category", "display_label", "confidence", "expires_at")
        }
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_archive_restore_cancels_old_extraction_but_allows_fresh_restored_source(
    system,
):
    engine, profiles = system
    memory_id = await source(engine)
    task, entered, release = extracting(profiles, memory_id)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await engine.archive_memories([memory_id]) == 1
        assert await engine.restore_memory(memory_id)
        release.set()
        assert await asyncio.wait_for(task, 5) == 0
        assert await profiles.get_profile(owner()) == []
        new, new_entered, new_release = extracting(profiles, memory_id)
        try:
            await asyncio.wait_for(new_entered.wait(), 5)
            new_release.set()
            assert await asyncio.wait_for(new, 5) == 1
            assert (await profiles.get_profile(owner()))[0]["value"] == "喜欢茶"
        finally:
            new_release.set()
            await asyncio.gather(new, return_exceptions=True)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_completed_source_deletion_cannot_be_rebuilt_by_later_extraction_admission(
    system,
):
    engine, profiles = system
    memory_id = await source(engine)
    assert await engine.delete_memory(memory_id)
    await profiles.delete_by_source_memory(memory_id)
    assert await engine.get_memory(memory_id) is None
    task, entered, release = extracting(profiles, memory_id)
    try:
        # Source deletion can finish during the storage pipeline's last
        # revision await, before update_from_messages registers its ticket.
        release.set()
        assert await asyncio.wait_for(task, 5) == 0
        assert not entered.is_set()
        assert (await profiles.list_for_web())["total"] == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_archive_restore_invalidates_stale_review_revision_without_renewing_profile_ttl(
    system, monkeypatch
):
    engine, profiles = system
    clock = [1000000.0]
    monkeypatch.setattr(user_profile_manager.time, "time", lambda: clock[0])
    coffee, tea = await source(engine, "喜欢咖啡"), await source(engine)
    await profiles.upsert_facts(owner(), [fact("喜欢咖啡")], source_memory_id=coffee)
    await profiles.upsert_facts(
        owner(), [{**fact(), "category": "status"}], source_memory_id=tea
    )
    before = await current(profiles)
    proposed = before["pending"][0]
    assert await engine.archive_memories([tea]) == 1
    clock[0] = proposed["expires_at"] + 1
    assert await engine.restore_memory(tea)
    row = await current(profiles)
    assert row["pending"][0]["expires_at"] == proposed["expires_at"]
    assert not row["pending"][0]["can_accept"]
    assert (
        await profiles.review_for_web(
            row["profile_scope"],
            "drink",
            claim_id=proposed["claim_id"],
            action="accept",
            expected_revision=before["revision"],
        )
        is None
    )


@pytest.mark.asyncio
async def test_source_deleted_during_extraction_without_profile_hook_cannot_add_late_facts(
    system,
):
    engine, profiles = system
    memory_id = await source(engine)
    task, entered, release = extracting(profiles, memory_id)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Core deletion can precede the caller's optional profile cleanup.
        assert await engine.delete_memory(memory_id)
        release.set()
        assert await asyncio.wait_for(task, 5) == 0
        assert (await profiles.list_for_web())["total"] == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_source_archived_before_ticket_skips_llm_and_restore_allows_fresh_extraction(
    system,
):
    engine, profiles = system
    memory_id = await source(engine)
    assert await engine.archive_memories([memory_id]) == 1
    task, entered, release = extracting(profiles, memory_id)
    try:
        release.set()
        assert await asyncio.wait_for(task, 5) == 0
        assert not entered.is_set()
        assert (await profiles.list_for_web())["total"] == 0
        assert await engine.restore_memory(memory_id)
        new, new_entered, new_release = extracting(profiles, memory_id)
        try:
            new_release.set()
            assert await asyncio.wait_for(new, 5) == 1
            assert new_entered.is_set()
        finally:
            new_release.set()
            await asyncio.gather(new, return_exceptions=True)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unbound_minimal_host_keeps_original_extraction_contract(system):
    _engine, bound_profiles = system
    legacy = UserProfileManager(Config(), SimpleNamespace(store=bound_profiles.store))
    task, entered, release = extracting(legacy, 171)
    try:
        release.set()
        assert await asyncio.wait_for(task, 5) == 1
        assert entered.is_set()
        assert (await legacy.get_profile(owner()))[0]["source_memory_id"] == 171
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["entry", "write"])
async def test_bound_engine_closed_connection_cannot_admit_or_write_extraction(
    system, phase
):
    engine, profiles = system
    memory_id = await source(engine)
    task = None
    if phase == "write":
        task, entered, release = extracting(profiles, memory_id)
        await asyncio.wait_for(entered.wait(), 5)
    await engine.db_connection.close()
    engine.db_connection = None
    if task is None:
        task, entered, release = extracting(profiles, memory_id)
    try:
        release.set()
        assert await asyncio.wait_for(task, 5) == 0
        if phase == "entry":
            assert not entered.is_set()
        assert (await profiles.list_for_web())["total"] == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["entry", "write"])
@pytest.mark.parametrize("error", [sqlite3.OperationalError, asyncio.CancelledError])
async def test_source_read_failure_or_cancellation_leaves_no_facts_and_can_retry(
    system, monkeypatch, phase, error
):
    engine, profiles = system
    memory_id = await source(engine)
    task = None
    if phase == "write":
        task, entered, release = extracting(profiles, memory_id)
        await asyncio.wait_for(entered.wait(), 5)
    execute = engine.db_connection.execute

    def interrupted(sql, *args, **kwargs):
        if sql.startswith("SELECT id FROM documents WHERE id IN"):
            raise error("source read interrupted")
        return execute(sql, *args, **kwargs)

    monkeypatch.setattr(engine.db_connection, "execute", interrupted)
    if task is None:
        task, entered, release = extracting(profiles, memory_id)
    try:
        release.set()
        with pytest.raises(error):
            await asyncio.wait_for(task, 5)
        if phase == "entry":
            assert not entered.is_set()
        assert (await profiles.list_for_web())["total"] == 0
        for table in ("user_profile_claims", "user_profile_evidence"):
            async with profiles.store.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ) as cursor:
                assert (await cursor.fetchone())[0] == 0
        monkeypatch.setattr(engine.db_connection, "execute", execute)
        new, _new_entered, new_release = extracting(profiles, memory_id)
        try:
            new_release.set()
            assert await asyncio.wait_for(new, 5) == 1
        finally:
            new_release.set()
            await asyncio.gather(new, return_exceptions=True)
    finally:
        monkeypatch.setattr(engine.db_connection, "execute", execute)
        release.set()
        await asyncio.gather(task, return_exceptions=True)
