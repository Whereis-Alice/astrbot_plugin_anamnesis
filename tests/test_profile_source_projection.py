"""Profile source headers and metadata edits preserve the real supporting evidence."""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore

SOURCE_KEYS = (
    "source_memory_id",
    "source_session_id",
    "source_group_id",
    "source_group_name",
    "source_sender_name",
)


class Config:
    def __init__(self):
        self.values = {"user_profile.enabled": True, "user_profile.scope_mode": "user"}

    def get(self, key, default=None):
        return self.values.get(key, default)


def owner(group_id, *, group_name="读书群", sender_name="Alice"):
    return SimpleNamespace(
        unified_msg_origin="qq:GroupMessage:" + group_id,
        get_sender_id=lambda: "alice",
        get_sender_name=lambda: sender_name,
        get_platform_name=lambda: "qq",
        get_group_id=lambda: group_id,
        message_obj=SimpleNamespace(
            group=SimpleNamespace(group_id=group_id, group_name=group_name)
        ),
    )


def fact(value="喜欢茶", quote="我喜欢喝茶"):
    return {
        "key": "drink",
        "value": value,
        "category": "preference",
        "explicit": True,
        "confidence": 0.95,
        "evidence": quote,
    }


@pytest_asyncio.fixture
async def profiles(tmp_path):
    store = ConversationStore(str(tmp_path / "profiles.db"))
    await store.initialize()
    manager = UserProfileManager(Config(), SimpleNamespace(store=store))
    try:
        yield manager
    finally:
        await store.close()


async def current(profiles):
    return (await profiles.list_for_web())["items"][0]


async def remove_source(profiles, action, memory_id):
    if action == "delete":
        return await profiles.delete_by_source_memory(memory_id)
    return await profiles.set_source_suppressed([memory_id], True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "suppress"])
async def test_remaining_evidence_projects_all_source_fields(profiles, action):
    first = owner("1", group_name="原来的群", sender_name="旧昵称")
    second = owner("2", group_name="最近的群", sender_name="新昵称")
    await profiles.upsert_facts(first, [fact()], source_memory_id=10)
    await profiles.upsert_facts(second, [fact()], source_memory_id=11)
    await remove_source(profiles, action, 11)
    row = await current(profiles)
    assert {key: row[key] for key in SOURCE_KEYS} == {
        "source_memory_id": 10,
        "source_session_id": first.unified_msg_origin,
        "source_group_id": "1",
        "source_group_name": "原来的群",
        "source_sender_name": "旧昵称",
    }
    assert row["context_session_id"] == second.unified_msg_origin
    assert (
        row["context_group_name"] == "最近的群"
        and row["context_sender_name"] == "新昵称"
    )
    assert (await profiles.list_for_web(source_session_id=first.unified_msg_origin))[
        "total"
    ] == 1
    assert (await profiles.list_for_web(source_session_id=second.unified_msg_origin))[
        "total"
    ] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "suppress"])
async def test_missing_source_labels_clear_stale_labels_instead_of_falling_back(
    profiles, action
):
    first = owner("1", group_name=None, sender_name=None)
    second = owner("2", group_name="最近的群", sender_name="新昵称")
    await profiles.upsert_facts(first, [fact()], source_memory_id=10)
    await profiles.upsert_facts(second, [fact()], source_memory_id=11)
    await remove_source(profiles, action, 11)
    row = await current(profiles)
    assert (
        row["source_memory_id"] == 10
        and row["source_session_id"] == first.unified_msg_origin
    )
    assert row["source_group_id"] == "1"
    assert row["source_group_name"] is None and row["source_sender_name"] is None


@pytest.mark.asyncio
async def test_all_suppressed_sources_clear_projection_and_restore_rehydrates_it(
    profiles,
):
    first, second = owner("1"), owner("2")
    await profiles.upsert_facts(first, [fact()], source_memory_id=10)
    await profiles.upsert_facts(second, [fact()], source_memory_id=11)
    await profiles.set_source_suppressed([10, 11], True)
    row = await current(profiles)
    assert row["suppressed"] == 1 and all(row[key] is None for key in SOURCE_KEYS)
    assert row["context_session_id"] == second.unified_msg_origin
    assert await profiles.get_profile(first) == []
    await profiles.set_source_suppressed([10], False)
    row = await current(profiles)
    assert row["suppressed"] == 0 and row["source_memory_id"] == 10
    assert (
        row["source_session_id"] == first.unified_msg_origin
        and row["source_group_id"] == "1"
    )


@pytest.mark.asyncio
async def test_deleted_current_source_clears_placeholder_provenance_without_hiding_proposal(
    profiles,
):
    await profiles.upsert_facts(owner("1"), [fact()], source_memory_id=10)
    await profiles.upsert_facts(
        owner("2"), [fact("喜欢咖啡", "我喜欢喝咖啡")], source_memory_id=11
    )
    await profiles.delete_by_source_memory(10)
    row = await current(profiles)
    assert row["value"] == "[来源已删除]" and row["suppressed"] == 1
    assert all(row[key] is None for key in SOURCE_KEYS)
    assert row["pending"][0]["value"] == "喜欢咖啡" and row["pending"][0]["can_accept"]


@pytest.mark.asyncio
async def test_unprotected_title_edit_preserves_all_sources_quotes_and_remaining_support(
    profiles,
):
    profiles.config_manager.values["user_profile.manual_override_enabled"] = False
    await profiles.upsert_facts(
        owner("1"), [fact(quote="我喜欢喝绿茶")], source_memory_id=10
    )
    await profiles.upsert_facts(owner("2"), [fact()], source_memory_id=11)
    row = await current(profiles)
    updated = await profiles.update_for_web(
        row["profile_scope"],
        "drink",
        {
            "display_label": "饮品偏好",
            "expected_revision": row["revision"],
        },
    )
    assert not updated["manual_override"] and updated["value"] == "喜欢茶"
    assert {
        (item["source_memory_id"], item["quote"]) for item in updated["evidence"]
    } == {
        (10, "我喜欢喝绿茶"),
        (11, "我喜欢喝茶"),
    }
    assert await profiles.delete_by_source_memory(11) == 0
    remaining = (await profiles.get_profile(owner("1")))[0]
    assert (
        remaining["source_memory_id"] == 10 and remaining["display_label"] == "饮品偏好"
    )


@pytest.mark.asyncio
async def test_unprotected_metadata_edit_retains_suppression_and_uses_only_live_projection(
    profiles,
):
    profiles.config_manager.values["user_profile.manual_override_enabled"] = False
    await profiles.upsert_facts(
        owner("1"), [fact(quote="我喜欢喝绿茶")], source_memory_id=10
    )
    await profiles.upsert_facts(owner("2"), [fact()], source_memory_id=11)
    await profiles.set_source_suppressed([11], True)
    row = await current(profiles)
    updated = await profiles.update_for_web(
        row["profile_scope"], "drink", {"display_label": "饮品偏好"}
    )
    assert {
        (item["source_memory_id"], item["suppressed"]) for item in updated["evidence"]
    } == {(10, 0), (11, 1)}
    assert (
        updated["source_memory_id"] == 10
        and updated["source_session_id"] == owner("1").unified_msg_origin
    )
    assert await profiles.delete_by_source_memory(10) == 0
    assert await profiles.get_profile(owner("1")) == []
    await profiles.set_source_suppressed([11], False)
    restored = (await profiles.get_profile(owner("2")))[0]
    assert (
        restored["source_memory_id"] == 11 and restored["display_label"] == "饮品偏好"
    )
    assert restored["evidence"][0]["quote"] == "我喜欢喝茶"


@pytest.mark.asyncio
async def test_metadata_edits_preserve_evidence_age_and_order_after_source_limit_is_lowered(
    profiles,
):
    profiles.config_manager.values["user_profile.manual_override_enabled"] = False
    await profiles.upsert_facts(
        owner("1"), [fact(quote="我喜欢喝绿茶")], source_memory_id=10
    )
    await profiles.upsert_facts(owner("2"), [fact()], source_memory_id=11)
    row = await current(profiles)
    fields = ("source_memory_id", "quote", "suppressed", "created_at")
    expected = [tuple(item[field] for field in fields) for item in row["evidence"]]
    profiles.config_manager.values["user_profile.evidence_max_sources"] = 1
    for label in ("饮品偏好", "常喝的饮品"):
        row = await profiles.update_for_web(
            row["profile_scope"], "drink", {"display_label": label}
        )
        assert [
            tuple(item[field] for field in fields) for item in row["evidence"]
        ] == expected
        assert row["source_memory_id"] == 11
    assert await profiles.delete_by_source_memory(11) == 0
    assert (await profiles.get_profile(owner("1")))[0]["source_memory_id"] == 10


@pytest.mark.asyncio
async def test_unprotected_title_edit_with_no_evidence_does_not_invent_independent_support(
    profiles,
):
    profiles.config_manager.values["user_profile.manual_override_enabled"] = False
    await profiles.upsert_facts(owner("1"), [fact()], source_memory_id=10)
    await profiles.upsert_facts(
        owner("2"), [fact("喜欢咖啡", "我喜欢喝咖啡")], source_memory_id=11
    )
    await profiles.delete_by_source_memory(10)
    row = await current(profiles)
    updated = await profiles.update_for_web(
        row["profile_scope"], "drink", {"display_label": "待审资料"}
    )
    assert updated["evidence"] == [] and updated["live_evidence_count"] == 0
    assert all(updated[key] is None for key in SOURCE_KEYS)
    assert await profiles.get_profile(owner("1")) == []
    assert updated["pending"][0]["can_accept"]


@pytest.mark.asyncio
async def test_actual_unprotected_value_change_keeps_existing_source_bound_semantics(
    profiles,
):
    profiles.config_manager.values["user_profile.manual_override_enabled"] = False
    await profiles.upsert_facts(owner("1"), [fact()], source_memory_id=10)
    await profiles.upsert_facts(owner("2"), [fact()], source_memory_id=11)
    row = await current(profiles)
    updated = await profiles.update_for_web(
        row["profile_scope"], "drink", {"value": "喜欢咖啡"}
    )
    assert updated["value"] == "喜欢咖啡" and not updated["manual_override"]
    assert [
        (item["source_memory_id"], item["quote"]) for item in updated["evidence"]
    ] == [(11, "")]
    assert await profiles.delete_by_source_memory(11) == 1
    assert await profiles.get_profile(owner("1")) == []


@pytest.mark.asyncio
async def test_protected_manual_edit_retains_independent_support(profiles):
    await profiles.upsert_facts(owner("1"), [fact()], source_memory_id=10)
    await profiles.upsert_facts(owner("2"), [fact()], source_memory_id=11)
    row = await current(profiles)
    updated = await profiles.update_for_web(
        row["profile_scope"], "drink", {"display_label": "管理员确认"}
    )
    assert (
        updated["manual_override"]
        and updated["evidence"][0]["source_memory_id"] is None
    )
    await profiles.delete_by_source_memories([10, 11])
    assert (await profiles.get_profile(owner("1")))[0]["display_label"] == "管理员确认"
