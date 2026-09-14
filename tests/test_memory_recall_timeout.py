"""
记忆召回超时兜底测试（Bug B）。

覆盖两个层面：
1. MemoryRecall.handle_memory_recall 调用 search_memories 时必须有超时兜底，
   超时后静默跳过记忆注入（只 warning，不抛异常），且内层检索协程被真正取消。
2. MemoryEngineCrudMixin.search_memories 支持可选的 timeout 关键字参数，
   默认 None 时行为与旧版本完全一致（向后兼容）。
"""

import asyncio
import inspect
from unittest.mock import AsyncMock, Mock, patch

import pytest
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.event_handler import EventHandler
from astrbot_plugin_anamnesis.core.event_handler_modules.memory_recall import (
    DEFAULT_SEARCH_TIMEOUT_SECONDS,
)
from astrbot_plugin_anamnesis.core.managers.memory_engine_crud import (
    MemoryEngineCrudMixin,
)
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridResult

from astrbot.api.platform import MessageType

RECALL_MODULE = "astrbot_plugin_anamnesis.core.event_handler_modules.memory_recall"

# 用于区分"配置项缺失"和"配置项显式为 None"
_MISSING = object()


class _TimeoutConfigManager(ConfigManager):
    """仅覆写 recall_engine.search_timeout_seconds 的读取。

    这里保留覆写能力，便于测试运行时切换超时值（包括 <= 0 的不限时语义）。
    """

    def __init__(self, raw_config: dict, timeout_value=_MISSING):
        # 必须在 super().__init__ 之前赋值：父类构造过程中就会读配置
        self._timeout_value = timeout_value
        super().__init__(raw_config)

    def get(self, key: str, default=None):
        if key == "recall_engine.search_timeout_seconds":
            return default if self._timeout_value is _MISSING else self._timeout_value
        return super().get(key, default)


def _make_handler(
    memory_engine, timeout_value=_MISSING, recall_overrides=None
) -> EventHandler:
    recall_config = {"top_k": 3, "injection_method": "extra_user_content"}
    recall_config.update(recall_overrides or {})

    conversation_manager = Mock()
    conversation_manager.add_message_from_event = AsyncMock()
    conversation_manager.store = Mock()
    conversation_manager.store.connection = None

    return EventHandler(
        context=Mock(),
        config_manager=_TimeoutConfigManager(
            {
                "recall_engine": recall_config,
                "reflection_engine": {"summary_trigger_rounds": 1},
                "session_manager": {"max_messages_per_session": 100},
            },
            timeout_value,
        ),
        memory_engine=memory_engine,
        memory_processor=Mock(),
        conversation_manager=conversation_manager,
    )


def _make_req(prompt: str = "今天吃什么"):
    req = Mock()
    req.prompt = prompt
    req.system_prompt = ""
    req.contexts = []
    req.extra_user_content_parts = []
    return req


def _make_event():
    event = Mock()
    event.unified_msg_origin = "test:private:sid-timeout"
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


# ==================== MemoryRecall 层超时 ====================


@pytest.mark.asyncio
async def test_recall_search_timeout_skips_injection_silently():
    """检索超时时应静默跳过注入：不抛异常、不改 req、内层协程被取消。"""
    cancelled = asyncio.Event()

    async def _slow_search(**kwargs):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return [_make_memory()]

    engine = Mock()
    engine.search_memories = AsyncMock(side_effect=_slow_search)
    handler = _make_handler(engine, timeout_value=0.05)

    event = _make_event()
    req = _make_req()

    with (
        patch(f"{RECALL_MODULE}.get_persona_id", new_callable=AsyncMock) as get_persona,
        patch(f"{RECALL_MODULE}.logger") as mock_logger,
    ):
        get_persona.return_value = "p1"
        await handler.handle_memory_recall(event, req)

    # 请求对象完全没被污染
    assert req.prompt == "今天吃什么"
    assert req.system_prompt == ""
    assert req.contexts == []
    assert req.extra_user_content_parts == []

    # 内层检索协程被真正取消（不会遗留 "Task exception was never retrieved"）
    assert cancelled.is_set()

    # 超时属于预期降级路径，不应记 error
    assert mock_logger.error.call_args_list == []
    warnings = [str(call.args[0]) for call in mock_logger.warning.call_args_list]
    assert any("超时" in text for text in warnings), warnings


@pytest.mark.asyncio
async def test_recall_search_timeout_disabled_when_non_positive():
    """search_timeout_seconds <= 0 表示不限时，不应包 asyncio.wait_for。"""
    observed: list = []
    real_wait_for = asyncio.wait_for

    async def _spy_wait_for(awaitable, timeout=None, **kwargs):
        observed.append(timeout)
        return await real_wait_for(awaitable, timeout, **kwargs)

    async def _slow_enough_search(**kwargs):
        await asyncio.sleep(0.15)
        return [_make_memory()]

    engine = Mock()
    engine.search_memories = AsyncMock(side_effect=_slow_enough_search)
    handler = _make_handler(engine, timeout_value=0)

    event = _make_event()
    req = _make_req()

    with (
        patch("asyncio.wait_for", _spy_wait_for),
        patch(f"{RECALL_MODULE}.get_persona_id", new_callable=AsyncMock) as get_persona,
    ):
        get_persona.return_value = "p1"
        await handler.handle_memory_recall(event, req)

    assert observed == []
    assert len(req.extra_user_content_parts) == 1


@pytest.mark.asyncio
async def test_recall_search_within_timeout_still_injects():
    """未超时的正常检索行为不变，注入照旧。"""
    engine = Mock()
    engine.search_memories = AsyncMock(return_value=[_make_memory()])
    handler = _make_handler(engine, timeout_value=5.0)

    event = _make_event()
    req = _make_req()

    with patch(
        f"{RECALL_MODULE}.get_persona_id", new_callable=AsyncMock
    ) as get_persona:
        get_persona.return_value = "p1"
        await handler.handle_memory_recall(event, req)

    parts = req.extra_user_content_parts
    assert len(parts) == 1
    assert "<Anamnesis-Memory>" in parts[0].text
    assert engine.search_memories.await_args.kwargs["k"] == 3
    assert engine.search_memories.await_args.kwargs["query"] == "今天吃什么"


@pytest.mark.asyncio
async def test_recall_search_runtime_error_is_logged_not_raised():
    """检索抛普通异常时仍由顶层兜底捕获，不向宿主冒泡。"""
    engine = Mock()
    engine.search_memories = AsyncMock(side_effect=RuntimeError("boom"))
    handler = _make_handler(engine, timeout_value=5.0)

    event = _make_event()
    req = _make_req()

    with (
        patch(f"{RECALL_MODULE}.get_persona_id", new_callable=AsyncMock) as get_persona,
        patch(f"{RECALL_MODULE}.logger") as mock_logger,
    ):
        get_persona.return_value = "p1"
        await handler.handle_memory_recall(event, req)

    assert mock_logger.error.called
    assert req.extra_user_content_parts == []


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (_MISSING, DEFAULT_SEARCH_TIMEOUT_SECONDS),
        (2.5, 2.5),
        ("3", 3.0),
        (0, None),
        (0.0, None),
        (-1, None),
        ("abc", DEFAULT_SEARCH_TIMEOUT_SECONDS),
        (None, DEFAULT_SEARCH_TIMEOUT_SECONDS),
        ("nan", DEFAULT_SEARCH_TIMEOUT_SECONDS),
        ("inf", DEFAULT_SEARCH_TIMEOUT_SECONDS),
        (True, DEFAULT_SEARCH_TIMEOUT_SECONDS),
    ],
)
def test_resolve_search_timeout(configured, expected):
    """_resolve_search_timeout 的取值/容错语义。"""
    engine = Mock()
    engine.search_memories = AsyncMock(return_value=[])
    handler = _make_handler(engine, timeout_value=configured)

    assert handler._memory_recall._resolve_search_timeout() == expected


def test_resolve_search_timeout_defaults_to_five_seconds():
    """真实 ConfigManager 下未配置时应回退到 5.0 秒。"""
    engine = Mock()
    engine.search_memories = AsyncMock(return_value=[])
    handler = EventHandler(
        context=Mock(),
        config_manager=ConfigManager({"recall_engine": {"top_k": 3}}),
        memory_engine=engine,
        memory_processor=Mock(),
        conversation_manager=Mock(),
    )

    assert DEFAULT_SEARCH_TIMEOUT_SECONDS == 5.0
    assert handler._memory_recall._resolve_search_timeout() == 5.0


# ==================== MemoryEngineCrudMixin.search_memories 超时 ====================


class _StubRetriever:
    """可控制耗时的检索器桩，记录调用次数与是否被取消。"""

    def __init__(self, results, delay: float = 0.0):
        self._results = results
        self._delay = delay
        self.calls = 0
        self.cancelled = asyncio.Event()

    async def search(self, query, k, session_id, persona_id):
        self.calls += 1
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return list(self._results)


class _StubCrudEngine(MemoryEngineCrudMixin):
    """只实现 search_memories 依赖的协作方法的轻量 mixin 宿主。"""

    def __init__(self, retriever: _StubRetriever):
        self.dual_route_retriever = None
        self.hybrid_retriever = retriever
        self.cached_writes: list = []
        self.migrations: list = []
        self.access_time_updates: list = []

    def _search_cache_key(self, query, k, session_id, persona_id):
        return (query, k, session_id, persona_id)

    def _get_cached_search_results(self, cache_key):
        return None

    def _set_cached_search_results(self, cache_key, results):
        self.cached_writes.append((cache_key, results))

    def _create_tracked_task(self, coro):
        # 测试里不真正调度后台任务；关闭协程避免 "coroutine was never awaited" 告警
        coro.close()
        return None

    def _filter_by_retrieval_policy(self, results):
        return results

    async def _merge_recent_memories(self, results, k, session_id, persona_id):
        return results

    async def _update_access_times_internal(self, doc_ids):
        self.access_time_updates.append(list(doc_ids))

    async def _migrate_session_data_if_needed(self, session_id):
        self.migrations.append(session_id)


def _make_hybrid_result(doc_id: int = 1) -> HybridResult:
    return HybridResult(
        doc_id=doc_id,
        final_score=0.9,
        rrf_score=0.5,
        bm25_score=0.4,
        vector_score=0.8,
        content="用户喜欢吃火锅",
        metadata={"importance": 0.9},
    )


@pytest.mark.asyncio
async def test_crud_search_memories_without_timeout_is_unchanged():
    """不传 timeout 时行为与旧版本一致（向后兼容）。"""
    retriever = _StubRetriever([_make_hybrid_result()], delay=0.05)
    engine = _StubCrudEngine(retriever)

    results = await engine.search_memories("火锅", k=2, session_id="test:private:s1")

    assert len(results) == 1
    assert retriever.calls == 1
    assert not retriever.cancelled.is_set()
    assert engine.cached_writes


@pytest.mark.asyncio
async def test_crud_search_memories_timeout_raises_and_cancels_inner_search():
    """timeout 生效时应抛 TimeoutError 并取消内层检索，且不写入缓存。"""
    retriever = _StubRetriever([_make_hybrid_result()], delay=5)
    engine = _StubCrudEngine(retriever)

    with pytest.raises(asyncio.TimeoutError):
        await engine.search_memories("火锅", k=2, timeout=0.05)

    assert retriever.calls == 1
    assert retriever.cancelled.is_set()
    assert engine.cached_writes == []


@pytest.mark.asyncio
async def test_crud_search_memories_timeout_non_positive_disables_timeout():
    """timeout <= 0 表示不限时。"""
    retriever = _StubRetriever([_make_hybrid_result()], delay=0.05)
    engine = _StubCrudEngine(retriever)

    results = await engine.search_memories("火锅", k=2, timeout=0)

    assert len(results) == 1
    assert not retriever.cancelled.is_set()


@pytest.mark.asyncio
async def test_crud_search_memories_empty_query_shortcut_with_timeout():
    """空查询短路应发生在超时包装之前。"""
    retriever = _StubRetriever([_make_hybrid_result()], delay=5)
    engine = _StubCrudEngine(retriever)

    assert await engine.search_memories("   ", timeout=0.05) == []
    assert retriever.calls == 0


def test_crud_search_memories_signature_is_backward_compatible():
    """公开签名只允许新增带默认值的关键字参数。"""
    sig = inspect.signature(MemoryEngineCrudMixin.search_memories)
    names = [p.name for p in sig.parameters.values()]

    assert names[:5] == ["self", "query", "k", "session_id", "persona_id"]
    assert sig.parameters["k"].default == 5
    assert sig.parameters["session_id"].default is None
    assert sig.parameters["persona_id"].default is None

    timeout_param = sig.parameters["timeout"]
    assert timeout_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert timeout_param.default is None
