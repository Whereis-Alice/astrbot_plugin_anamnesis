"""User profiles must remain user-scoped, corrigible and independent of search."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.event_handler_modules.memory_recall import (
    MemoryRecall,
)
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore


def _event(sender="alice", platform="qq", session="qq:GroupMessage:42"):
    event = Mock()
    event.get_sender_id.return_value = sender
    event.get_sender_name.return_value = sender
    event.get_platform_name.return_value = platform
    event.get_message_type.return_value = MessageType.GROUP_MESSAGE
    event.unified_msg_origin = session
    return event


def _fact(key="washer_runtime", value="洗衣机运行 35 分钟", **kwargs):
    return {
        "key": key,
        "value": value,
        "category": kwargs.pop("category", "preference"),
        "confidence": kwargs.pop("confidence", 0.95),
        "explicit": kwargs.pop("explicit", True),
        **kwargs,
    }


@pytest_asyncio.fixture
async def profile_manager(tmp_path):
    store = ConversationStore(str(tmp_path / "conversations.db"))
    await store.initialize()
    config = ConfigManager({"user_profile": {"enabled": True}})
    manager = UserProfileManager(config, SimpleNamespace(store=store))
    try:
        yield manager
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_profile_upsert_corrects_value_and_isolates_users(profile_manager):
    alice = _event("alice")
    bob = _event("bob")
    assert (
        await profile_manager.upsert_facts(alice, [_fact()], source_memory_id=10) == 1
    )
    await profile_manager.upsert_facts(
        alice, [_fact(value="洗衣机运行 45 分钟")], source_memory_id=11
    )
    await profile_manager.upsert_facts(
        bob, [_fact(value="洗衣机运行 20 分钟")], source_memory_id=12
    )

    alice_facts = await profile_manager.get_profile(alice)
    assert len(alice_facts) == 1
    assert alice_facts[0]["value"] == "洗衣机运行 45 分钟"
    assert alice_facts[0]["source_memory_id"] == 11
    assert (await profile_manager.get_profile(bob))[0]["value"] == "洗衣机运行 20 分钟"

    assert await profile_manager.delete_by_source_memory(10) == 0
    assert await profile_manager.delete_by_source_memory(11) == 1
    assert await profile_manager.get_profile(alice) == []
    assert len(await profile_manager.get_profile(bob)) == 1


@pytest.mark.asyncio
async def test_batch_source_delete_clears_only_selected_fact_origins(profile_manager):
    alice = _event("alice")
    await profile_manager.upsert_facts(
        alice, [_fact("first", "第一条")], source_memory_id=10
    )
    await profile_manager.upsert_facts(
        alice, [_fact("second", "第二条")], source_memory_id=11
    )
    await profile_manager.upsert_facts(
        alice, [_fact("third", "第三条")], source_memory_id=12
    )

    assert await profile_manager.delete_by_source_memories([10, 12, 999]) == 2
    assert [
        fact["profile_key"] for fact in await profile_manager.get_profile(alice)
    ] == ["second"]


@pytest.mark.asyncio
async def test_web_listing_filters_scope_session_key_and_exact_delete(profile_manager):
    first = _event("alice", "qq", "qq:GroupMessage:42")
    second = _event("bob", "qq", "qq:GroupMessage:43")
    await profile_manager.upsert_facts(
        first, [_fact("washer_runtime", "35 分钟")], source_memory_id=1
    )
    await profile_manager.upsert_facts(
        second, [_fact("dryer_runtime", "20 分钟")], source_memory_id=2
    )

    scope = profile_manager.scope_for_event(first)
    result = await profile_manager.list_for_web(
        profile_scope=scope,
        source_session_id=first.unified_msg_origin,
        key_query="washer",
        limit=10,
        offset=0,
    )
    assert result["total"] == 1
    assert result["items"][0]["profile_key"] == "washer_runtime"
    assert set(result["session_ids"]) == {
        first.unified_msg_origin,
        second.unified_msg_origin,
    }
    assert result["scope_mode"] == "session"
    assert (await profile_manager.list_for_web(limit=1, offset=1))["total"] == 2
    assert await profile_manager.delete_for_web(scope, "not_present") == 0
    assert await profile_manager.delete_for_web(scope, "washer_runtime") == 1
    assert (await profile_manager.list_for_web(profile_scope=scope))["total"] == 0
    assert (await profile_manager.list_for_web())["total"] == 1


@pytest.mark.asyncio
async def test_profile_scope_platform_and_isolated_session(profile_manager):
    qq = _event("same-id", "qq", "qq:GroupMessage:42")
    telegram = _event("same-id", "telegram", "telegram:GroupMessage:42")
    assert profile_manager.scope_for_event(qq) != profile_manager.scope_for_event(
        telegram
    )
    assert profile_manager.scope_for_event(qq) != profile_manager.scope_for_event(
        _event("same-id", "qq", "qq:GroupMessage:43")
    )
    user_config = ConfigManager({"user_profile": {"enabled": True, "scope_mode": "user"}})
    shared = UserProfileManager(
        user_config, SimpleNamespace(store=profile_manager.store)
    )
    assert shared.scope_for_event(qq) == shared.scope_for_event(
        _event("same-id", "qq", "qq:GroupMessage:43")
    )
    isolated_config = ConfigManager(
        {
            "user_profile": {"enabled": True, "scope_mode": "user"},
            "filtering_settings": {"isolated_sessions": "qq:GroupMessage:42"},
        }
    )
    isolated = UserProfileManager(
        isolated_config, SimpleNamespace(store=profile_manager.store)
    )
    assert isolated.scope_for_event(qq) == profile_manager.scope_for_event(qq)
    assert isolated.scope_for_event(qq) != shared.scope_for_event(qq)
    assert isolated.scope_for_event(
        _event("same-id", "qq", "qq:GroupMessage:43")
    ) != isolated.scope_for_event(qq)
    assert profile_manager.scope_for_event(_event(session="")) is None


@pytest.mark.asyncio
async def test_new_conversation_in_same_chat_retains_profile(profile_manager):
    first = _event("alice", session="qq:GroupMessage:42")
    first.conversation_id = "old-conversation"
    after_new = _event("alice", session="qq:GroupMessage:42")
    after_new.conversation_id = "new-conversation"
    await profile_manager.upsert_facts(first, [_fact()], source_memory_id=10)

    assert profile_manager.scope_for_event(first) == profile_manager.scope_for_event(
        after_new
    )
    assert [row["profile_key"] for row in await profile_manager.get_profile(after_new)] == [
        "washer_runtime"
    ]


@pytest.mark.asyncio
async def test_legacy_user_scope_migrates_only_its_source_session(profile_manager):
    current = _event("alice", "qq", "qq:GroupMessage:42")
    another = _event("alice", "qq", "qq:GroupMessage:43")
    legacy_config = ConfigManager({"user_profile": {"enabled": True, "scope_mode": "user"}})
    legacy = UserProfileManager(
        legacy_config, SimpleNamespace(store=profile_manager.store)
    )
    await legacy.upsert_facts(current, [_fact("washer_runtime", "35 分钟")], source_memory_id=10)
    await legacy.upsert_facts(another, [_fact("other_fact", "另一群的事实")], source_memory_id=11)

    assert [row["profile_key"] for row in await profile_manager.get_profile(current)] == [
        "washer_runtime"
    ]
    assert [row["profile_key"] for row in await profile_manager.get_profile(another)] == [
        "other_fact"
    ]
    assert await legacy.get_profile(current) == []


@pytest.mark.asyncio
async def test_expired_status_and_unverified_claims_are_not_injected(profile_manager):
    alice = _event()
    count = await profile_manager.upsert_facts(
        alice,
        [
            _fact("mood", "今天身体不舒服", category="status"),
            _fact("guessed_age", "20 岁", explicit=False),
            _fact("uncertain", "可能喜欢咖啡", confidence=0.4),
        ],
    )
    assert count == 1
    connection = profile_manager.store.connection
    await connection.execute(
        "UPDATE user_profiles SET expires_at = ? WHERE profile_key = 'mood'",
        (time.time() - 1,),
    )
    await connection.commit()
    assert await profile_manager.get_profile(alice) == []
    assert await profile_manager.format_for_injection(alice) == ""


@pytest.mark.asyncio
async def test_group_extraction_uses_only_current_sender(profile_manager):
    alice = _event("alice")
    seen_prompt = None

    async def fake_llm(prompt, system_prompt, max_retries):
        nonlocal seen_prompt
        seen_prompt = prompt
        return json.dumps(
            {"facts": [_fact(evidence="我的洗衣机运行 35 分钟")]}, ensure_ascii=False
        )

    processor = Mock()
    processor._call_llm_with_retry = AsyncMock(side_effect=fake_llm)
    messages = [
        SimpleNamespace(
            role="user", sender_id="alice", content="我的洗衣机运行 35 分钟"
        ),
        SimpleNamespace(role="user", sender_id="bob", content="我的密码是 secret-bob"),
        SimpleNamespace(role="assistant", sender_id="bot", content="我有超级权限"),
    ]
    assert (
        await profile_manager.update_from_messages(
            alice, messages, processor, source_memory_id=99
        )
        == 1
    )
    assert "洗衣机运行 35 分钟" in seen_prompt
    assert "secret-bob" not in seen_prompt
    assert "超级权限" not in seen_prompt


@pytest.mark.asyncio
async def test_profile_extraction_rejects_facts_without_verbatim_evidence(
    profile_manager,
):
    event = _event("alice")
    processor = Mock()
    processor._call_llm_with_retry = AsyncMock(
        return_value=json.dumps(
            {
                "facts": [
                    _fact("unsupported", "喜欢咖啡", evidence="用户喜欢咖啡"),
                    _fact(
                        "supported", "洗衣机运行 35 分钟", evidence="洗衣机运行 35 分钟"
                    ),
                ]
            },
            ensure_ascii=False,
        )
    )
    messages = [
        SimpleNamespace(
            role="user", sender_id="alice", content="我的洗衣机运行 35 分钟"
        )
    ]

    assert (
        await profile_manager.update_from_messages(
            event, messages, processor, source_memory_id=77
        )
        == 1
    )
    facts = await profile_manager.get_profile(event)
    assert [fact["profile_key"] for fact in facts] == ["supported"]


@pytest.mark.asyncio
async def test_profile_injected_even_when_top_k_zero(profile_manager):
    event = _event("alice", session="qq:FriendMessage:alice")
    event.get_message_type.return_value = MessageType.FRIEND_MESSAGE
    await profile_manager.upsert_facts(event, [_fact()], source_memory_id=99)
    config = ConfigManager(
        {
            "user_profile": {"enabled": True},
            "recall_engine": {"top_k": 0},
        }
    )
    engine = Mock()
    engine.search_memories = AsyncMock()
    conversation = Mock()
    conversation.add_message_from_event = AsyncMock()
    message_utils = Mock()
    message_utils.get_event_message_str = AsyncMock(return_value="你好")
    message_utils.enforce_message_limit = AsyncMock()
    recall = MemoryRecall(
        Mock(), config, engine, conversation, message_utils, Mock(), profile_manager
    )
    request = ProviderRequest(prompt="你好")

    await recall.handle_memory_recall(event, request)

    engine.search_memories.assert_not_called()
    assert len(request.extra_user_content_parts) == 1
    assert "洗衣机运行 35 分钟" in request.extra_user_content_parts[0].text
    assert request.extra_user_content_parts[0]._no_save is True


@pytest.mark.asyncio
async def test_profile_survives_ordinary_search_timeout(profile_manager):
    event = _event("alice")
    await profile_manager.upsert_facts(event, [_fact()], source_memory_id=99)
    config = ConfigManager(
        {
            "user_profile": {"enabled": True},
            "recall_engine": {"top_k": 1, "search_timeout_seconds": 0.01},
        }
    )

    async def slow_search(**_kwargs):
        await asyncio.sleep(0.1)
        return []

    engine = Mock()
    engine.search_memories = AsyncMock(side_effect=slow_search)
    message_utils = Mock()
    message_utils.get_event_message_str = AsyncMock(return_value="你好")
    recall = MemoryRecall(
        Mock(), config, engine, Mock(), message_utils, Mock(), profile_manager
    )
    request = ProviderRequest(prompt="你好")

    await recall.handle_memory_recall(event, request)

    assert len(request.extra_user_content_parts) == 1
    assert "洗衣机运行 35 分钟" in request.extra_user_content_parts[0].text


@pytest.mark.asyncio
async def test_disabled_profile_does_not_create_schema_or_call_llm(tmp_path):
    store = ConversationStore(str(tmp_path / "conversations.db"))
    await store.initialize()
    try:
        manager = UserProfileManager(ConfigManager(), SimpleNamespace(store=store))
        event = _event()
        processor = Mock()
        processor._call_llm_with_retry = AsyncMock()

        assert await manager.format_for_injection(event) == ""
        assert (
            await manager.update_from_messages(event, [], processor, source_memory_id=1)
            == 0
        )
        assert manager._schema_ready is False
        processor._call_llm_with_retry.assert_not_awaited()
    finally:
        await store.close()
