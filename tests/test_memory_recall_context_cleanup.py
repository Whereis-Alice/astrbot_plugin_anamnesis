"""
记忆注入清理测试（Bug C / Bug K / Bug D）。

- Bug C：注入到 req.prompt 的记忆块会被 AstrBot 持久化进会话历史，
  而清理函数原先只扫 system_prompt 和 extra_user_content_parts，
  导致 req.contexts 里的 <Anamnesis-Memory> 残留随轮数累积。
- Bug K：_normalize_text_only_context_parts 不能破坏宿主传来的多模态结构，
  也不能把宿主标记为临时（_no_save）的 part 折叠成永久文本。
- Bug D：伪造工具调用消息必须能被精确回收（含孤儿 tool 消息、
  与真实工具调用混在同一条 assistant 里的情况），且提供同请求内回收入口。
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.base.constants import (
    FAKE_TOOL_CALL_ID_PREFIX,
    FAKE_TOOL_CALL_NAME,
    MEMORY_INJECTION_FOOTER,
    MEMORY_INJECTION_HEADER,
)
from astrbot_plugin_anamnesis.core.event_handler import EventHandler
from astrbot_plugin_anamnesis.core.event_handler_modules.memory_recall import (
    INJECTION_MARKER_PAIRS,
    LEGACY_INJECTION_MARKERS,
)

from astrbot.api.platform import MessageType
from astrbot.core.agent.message import Message, TextPart

RECALL_MODULE = "astrbot_plugin_anamnesis.core.event_handler_modules.memory_recall"

MEMORY_BLOCK = (
    MEMORY_INJECTION_HEADER + "\n1. 用户喜欢吃火锅\n" + MEMORY_INJECTION_FOOTER
)
LEGACY_HEADER, LEGACY_FOOTER = LEGACY_INJECTION_MARKERS[0]
LEGACY_BLOCK = LEGACY_HEADER + "\n1. 旧插件遗留记忆\n" + LEGACY_FOOTER

STUB_ID = FAKE_TOOL_CALL_ID_PREFIX + "abc123def456"
REAL_ID = "call_real_1"


def _make_handler(recall_overrides=None) -> EventHandler:
    recall_config = {"top_k": 3, "injection_method": "extra_user_content"}
    recall_config.update(recall_overrides or {})

    engine = Mock()
    engine.search_memories = AsyncMock(return_value=[])

    conversation_manager = Mock()
    conversation_manager.add_message_from_event = AsyncMock()
    conversation_manager.store = Mock()
    conversation_manager.store.connection = None

    return EventHandler(
        context=Mock(),
        config_manager=ConfigManager(
            {
                "recall_engine": recall_config,
                "reflection_engine": {"summary_trigger_rounds": 1},
                "session_manager": {"max_messages_per_session": 100},
            }
        ),
        memory_engine=engine,
        memory_processor=Mock(),
        conversation_manager=conversation_manager,
    )


def _make_recall(recall_overrides=None):
    return _make_handler(recall_overrides)._memory_recall


def _make_req(prompt: str = "今天吃什么"):
    req = Mock()
    req.prompt = prompt
    req.system_prompt = ""
    req.contexts = []
    req.extra_user_content_parts = []
    return req


def _make_event():
    event = Mock()
    event.unified_msg_origin = "test:private:sid-cleanup"
    event.get_message_type = Mock(return_value=MessageType.FRIEND_MESSAGE)
    event.get_sender_id = Mock(return_value="user-1")
    event.get_self_id = Mock(return_value="bot-1")
    event.get_sender_name = Mock(return_value="Tester")
    event.get_message_str = Mock(return_value="今天吃什么")
    event.get_messages = Mock(return_value=[])
    event.get_platform_name = Mock(return_value="test")
    return event


def _make_memory():
    memory = Mock(
        content="用户喜欢吃火锅",
        final_score=0.88,
        metadata={"importance": 0.9, "create_time": 1700000000},
    )
    memory.doc_id = 99
    return memory


def _fake_tool_call(call_id: str = STUB_ID) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": FAKE_TOOL_CALL_NAME, "arguments": "{}"},
    }


# ==================== Bug C：req.contexts 清理 ====================


def test_contexts_str_content_injection_is_removed():
    """字符串型 content 中的注入块应被清掉，其余正文保留。"""
    recall = _make_recall()
    req = _make_req()
    req.contexts = [
        {"role": "user", "content": MEMORY_BLOCK + "\n\n今天吃什么"},
        {"role": "assistant", "content": "火锅不错"},
    ]

    removed = recall._remove_injected_memories_from_context(req, "s")

    assert removed == 1
    assert len(req.contexts) == 2
    assert req.contexts[0]["content"] == "今天吃什么"
    assert req.contexts[1]["content"] == "火锅不错"


def test_contexts_fully_injected_message_keeps_empty_string():
    """content 被清空后保留空串占位，避免破坏 user/assistant 交替。"""
    recall = _make_recall()
    req = _make_req()
    req.contexts = [
        {"role": "user", "content": MEMORY_BLOCK},
        {"role": "assistant", "content": "好的"},
    ]

    removed = recall._remove_injected_memories_from_context(req, "s")

    assert removed == 1
    assert len(req.contexts) == 2
    assert req.contexts[0]["role"] == "user"
    assert req.contexts[0]["content"] == ""


def test_contexts_multimodal_content_keeps_image_part():
    """多模态 content 里的图片 part 必须原样保留（不能被拍平/丢弃）。"""
    recall = _make_recall()
    req = _make_req()
    image_part = {"type": "image_url", "image_url": {"url": "http://example/a.png"}}
    req.contexts = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": MEMORY_BLOCK + "\n\n看图"},
                image_part,
            ],
        }
    ]

    removed = recall._remove_injected_memories_from_context(req, "s")

    content = req.contexts[0]["content"]
    assert removed == 1
    assert isinstance(content, list)
    assert len(content) == 2
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "看图"
    assert content[1] is image_part


def test_contexts_multimodal_all_text_cleared_becomes_empty_string():
    """多模态里所有文本 part 都被清空且无其它 part 时，退化为空串占位。"""
    recall = _make_recall()
    req = _make_req()
    req.contexts = [{"role": "user", "content": [{"type": "text", "text": MEMORY_BLOCK}]}]

    removed = recall._remove_injected_memories_from_context(req, "s")

    assert removed == 1
    assert len(req.contexts) == 1
    assert req.contexts[0]["content"] == ""


def test_contexts_object_text_part_injection_is_removed():
    """ContentPart 对象形式的 part 也要被清理，且保持对象 identity。"""
    recall = _make_recall()
    req = _make_req()
    part = TextPart(text=MEMORY_BLOCK + "\n\n你好")
    req.contexts = [{"role": "user", "content": [part]}]

    removed = recall._remove_injected_memories_from_context(req, "s")

    content = req.contexts[0]["content"]
    assert removed == 1
    assert content[0] is part
    assert part.text == "你好"


def test_contexts_legacy_rag_faiss_marker_is_removed():
    """上游 livingmemory 的旧 marker 也要能清理（迁移过来的老会话历史）。"""
    recall = _make_recall()
    req = _make_req()
    req.system_prompt = "你是助手\n\n" + LEGACY_BLOCK
    req.contexts = [{"role": "user", "content": LEGACY_BLOCK + "\n\n今天吃什么"}]

    removed = recall._remove_injected_memories_from_context(req, "s")

    assert removed == 2
    assert "RAG-Faiss-Memory" not in req.system_prompt
    assert req.system_prompt == "你是助手"
    assert req.contexts[0]["content"] == "今天吃什么"


def test_legacy_marker_list_covers_upstream_livingmemory_marker():
    """历史 marker 兼容列表必须包含上游 livingmemory 的标记。"""
    assert ("<RAG-Faiss-Memory>", "</RAG-Faiss-Memory>") in LEGACY_INJECTION_MARKERS
    assert (MEMORY_INJECTION_HEADER, MEMORY_INJECTION_FOOTER) in INJECTION_MARKER_PAIRS
    for header, footer in LEGACY_INJECTION_MARKERS:
        assert (header, footer) in INJECTION_MARKER_PAIRS


def test_contexts_without_injection_is_untouched():
    """没有注入残留时不应改动任何内容。"""
    recall = _make_recall()
    req = _make_req()
    original = {"role": "user", "content": "今天吃什么"}
    req.contexts = [original]

    removed = recall._remove_injected_memories_from_context(req, "s")

    assert removed == 0
    assert req.contexts[0] is original
    assert req.contexts[0]["content"] == "今天吃什么"


@pytest.mark.asyncio
async def test_handle_memory_recall_cleans_contexts_before_injection():
    """完整流程：注入新记忆前会先清掉历史里的旧注入残留。"""
    handler = _make_handler()
    handler.memory_engine.search_memories = AsyncMock(return_value=[_make_memory()])
    req = _make_req()
    req.contexts = [
        {"role": "user", "content": MEMORY_BLOCK + "\n\n上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
    ]

    with patch(
        f"{RECALL_MODULE}.get_persona_id", new_callable=AsyncMock
    ) as get_persona:
        get_persona.return_value = "p1"
        await handler.handle_memory_recall(_make_event(), req)

    assert req.contexts[0]["content"] == "上一轮问题"
    assert MEMORY_INJECTION_HEADER not in req.contexts[0]["content"]
    assert len(req.extra_user_content_parts) == 1


@pytest.mark.asyncio
async def test_handle_memory_recall_respects_auto_remove_injected_false():
    """auto_remove_injected=False 时保持旧语义：不清理历史残留。"""
    handler = _make_handler({"auto_remove_injected": False})
    handler.memory_engine.search_memories = AsyncMock(return_value=[_make_memory()])
    req = _make_req()
    req.contexts = [{"role": "user", "content": MEMORY_BLOCK + "\n\n上一轮问题"}]

    with patch(
        f"{RECALL_MODULE}.get_persona_id", new_callable=AsyncMock
    ) as get_persona:
        get_persona.return_value = "p1"
        await handler.handle_memory_recall(_make_event(), req)

    assert MEMORY_INJECTION_HEADER in req.contexts[0]["content"]


# ==================== Bug K：多模态结构不被折叠 ====================


def test_normalize_keeps_multimodal_structure_with_image():
    """含图片 part 的 content 不能被折叠成字符串。"""
    recall = _make_recall()
    req = _make_req()
    image_part = {"type": "image_url", "image_url": {"url": "http://example/a.png"}}
    content = [{"type": "text", "text": "看图"}, image_part]
    req.contexts = [{"role": "user", "content": content}]

    normalized = recall._normalize_text_only_context_parts(req, "s")

    assert normalized == 0
    assert req.contexts[0]["content"] is content
    assert req.contexts[0]["content"][1] is image_part


def test_normalize_does_not_collapse_no_save_text_parts():
    """宿主标记为临时（_no_save）的文本 part 不能被折叠成永久文本。"""
    recall = _make_recall()
    req = _make_req()
    req.contexts = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "你好"},
                {"type": "text", "text": MEMORY_BLOCK, "_no_save": True},
            ],
        }
    ]

    normalized = recall._normalize_text_only_context_parts(req, "s")

    assert normalized == 0
    assert isinstance(req.contexts[0]["content"], list)


def test_normalize_collapses_pure_text_parts():
    """全是普通文本 part 时仍折叠为字符串（原有行为保持不变）。"""
    recall = _make_recall()
    req = _make_req()
    req.contexts = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "你"},
                {"type": "text", "text": "好"},
            ],
        }
    ]

    normalized = recall._normalize_text_only_context_parts(req, "s")

    assert normalized == 1
    assert req.contexts[0]["content"] == "你好"


def test_normalize_handles_object_content_parts():
    """ContentPart 对象形式的纯文本 content 也应被折叠。"""
    recall = _make_recall()
    req = _make_req()
    req.contexts = [{"role": "user", "content": [TextPart(text="你"), TextPart(text="好")]}]

    normalized = recall._normalize_text_only_context_parts(req, "s")

    assert normalized == 1
    assert req.contexts[0]["content"] == "你好"


def test_normalize_keeps_object_parts_marked_no_save():
    """对象形式的临时 part 同样不折叠。"""
    recall = _make_recall()
    req = _make_req()
    content = [TextPart(text="你好").mark_as_temp()]
    req.contexts = [{"role": "user", "content": content}]

    normalized = recall._normalize_text_only_context_parts(req, "s")

    assert normalized == 0
    assert req.contexts[0]["content"] is content


def test_normalize_ignores_non_user_roles():
    """只处理 user 角色（保持原有语义）。"""
    recall = _make_recall()
    req = _make_req()
    req.contexts = [
        {"role": "assistant", "content": [{"type": "text", "text": "你好"}]}
    ]

    assert recall._normalize_text_only_context_parts(req, "s") == 0
    assert isinstance(req.contexts[0]["content"], list)


# ==================== Bug D：伪造工具调用回收 ====================


def test_remove_fake_tool_call_recovers_orphan_tool_message():
    """只剩 tool 半边的孤儿伪造消息也要能回收（否则会炸 Gemini）。"""
    recall = _make_recall()
    req = _make_req()
    req.contexts = [
        {"role": "user", "content": "你好"},
        {"role": "tool", "tool_call_id": STUB_ID, "name": FAKE_TOOL_CALL_NAME, "content": "{}"},
    ]

    removed = recall._remove_fake_tool_call_from_context(req, "s")

    assert removed == 1
    assert len(req.contexts) == 1
    assert req.contexts[0]["role"] == "user"


def test_remove_fake_tool_call_keeps_real_calls_in_mixed_assistant():
    """同一条 assistant 里混有真实工具调用时，只摘掉伪造的那个。"""
    recall = _make_recall()
    req = _make_req()
    req.contexts = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                _fake_tool_call(),
                {
                    "id": REAL_ID,
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": STUB_ID, "content": "{}"},
        {"role": "tool", "tool_call_id": REAL_ID, "content": "{}"},
    ]

    removed = recall._remove_fake_tool_call_from_context(req, "s")

    assert removed == 2
    assert len(req.contexts) == 2
    assistant_msg = req.contexts[0]
    assert assistant_msg["role"] == "assistant"
    assert [tc["id"] for tc in assistant_msg["tool_calls"]] == [REAL_ID]
    assert req.contexts[1]["tool_call_id"] == REAL_ID


def test_remove_fake_tool_call_does_not_match_by_tool_name():
    """伪造调用复用了真实工具名，只能按 ID 前缀匹配，不能按 name 匹配。"""
    recall = _make_recall()
    req = _make_req()
    req.contexts = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_fake_tool_call("call_genuine_1")],
        },
        {
            "role": "tool",
            "tool_call_id": "call_genuine_1",
            "name": FAKE_TOOL_CALL_NAME,
            "content": "{}",
        },
    ]

    removed = recall._remove_fake_tool_call_from_context(req, "s")

    assert removed == 0
    assert len(req.contexts) == 2


def test_remove_fake_tool_call_keeps_assistant_with_text_content():
    """assistant 自身还有正文时只摘掉 tool_calls 键，不删整条消息。"""
    recall = _make_recall()
    req = _make_req()
    req.contexts = [
        {"role": "assistant", "content": "好的", "tool_calls": [_fake_tool_call()]},
        {"role": "tool", "tool_call_id": STUB_ID, "content": "{}"},
    ]

    removed = recall._remove_fake_tool_call_from_context(req, "s")

    assert removed == 2
    assert len(req.contexts) == 1
    assert req.contexts[0]["content"] == "好的"
    assert "tool_calls" not in req.contexts[0]


def test_remove_fake_tool_call_logs_debug_on_unexpected_error():
    """异常路径不再静默 pass，而是记 debug 日志（带 exc_info）。"""

    class _ExplodingList(list):
        def pop(self, index=-1):
            raise RuntimeError("boom")

    recall = _make_recall()
    req = _make_req()
    req.contexts = _ExplodingList(
        [{"role": "tool", "tool_call_id": STUB_ID, "content": "{}"}]
    )

    with patch(f"{RECALL_MODULE}.logger") as mock_logger:
        removed = recall._remove_fake_tool_call_from_context(req, "s")

    assert removed == 0
    assert mock_logger.debug.called
    assert any(
        call.kwargs.get("exc_info") for call in mock_logger.debug.call_args_list
    )


def test_remove_fake_tool_call_from_agent_messages_purges_message_objects():
    """同请求内回收入口：直接就地清理 Agent run_context 的 Message 列表。"""
    recall = _make_recall()
    messages = [
        Message(role="user", content="你好"),
        Message(role="assistant", content=None, tool_calls=[_fake_tool_call()]),
        Message(role="tool", tool_call_id=STUB_ID, content="{}"),
    ]

    removed = recall.remove_fake_tool_call_from_agent_messages(messages, "s")

    assert removed == 2
    assert len(messages) == 1
    assert messages[0].role == "user"


def test_remove_fake_tool_call_from_agent_messages_accepts_run_context():
    """也接受带 .messages 属性的 run_context 包装对象。"""
    recall = _make_recall()
    messages = [
        Message(role="assistant", content=None, tool_calls=[_fake_tool_call()]),
        Message(role="tool", tool_call_id=STUB_ID, content="{}"),
    ]
    run_context = Mock()
    run_context.messages = messages

    removed = recall.remove_fake_tool_call_from_agent_messages(run_context, "s")

    assert removed == 2
    assert messages == []


def test_remove_fake_tool_call_from_agent_messages_keeps_real_tool_calls():
    """真实工具调用不受影响。"""
    recall = _make_recall()
    messages = [
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                {
                    "id": REAL_ID,
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        ),
        Message(role="tool", tool_call_id=REAL_ID, content="{}"),
    ]

    removed = recall.remove_fake_tool_call_from_agent_messages(messages, "s")

    assert removed == 0
    assert len(messages) == 2


def test_remove_fake_tool_call_from_agent_messages_handles_empty():
    """空/非法输入安全返回 0。"""
    recall = _make_recall()

    assert recall.remove_fake_tool_call_from_agent_messages([], "s") == 0
    assert recall.remove_fake_tool_call_from_agent_messages(None, "s") == 0
    assert recall.remove_fake_tool_call_from_agent_messages(Mock(spec=[]), "s") == 0
