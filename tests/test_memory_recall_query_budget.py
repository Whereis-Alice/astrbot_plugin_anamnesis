"""Automatic recall accepts long messages without changing stored/user text."""

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from astrbot.api.platform import MessageType
from astrbot_plugin_anamnesis.core.retrieval.temporal_retrieval import search_with_time

from tests.test_memory_recall_timeout import (
    RECALL_MODULE,
    _make_event,
    _make_handler,
    _make_memory,
    _make_req,
)

TEMPORAL_MODULE = "astrbot_plugin_anamnesis.core.retrieval.temporal_retrieval"
UTILS_MODULE = "astrbot_plugin_anamnesis.core.utils"


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("size", [2001, 6000])
async def test_long_message_reaches_real_retrieval_and_injection(group, size):
    current = "开头：我的咖啡偏好。" + "背景" * size + "末尾：我平常喜欢喝什么？"
    engine = Mock(search_memories=AsyncMock(return_value=[_make_memory()]))
    engine.memory_administration = Mock(episode_candidates=AsyncMock(return_value=[]))
    handler = _make_handler(engine)
    # Enable the optional second recall channel without changing query semantics.
    get_option = handler._memory_recall.config_manager.get
    handler._memory_recall.config_manager.get = Mock(side_effect=lambda key, default=None:
        True if key == "episodic_memory.enabled" else get_option(key, default))
    event, req = _make_event(), _make_req(current)
    event.get_message_str.return_value = current
    event.get_message_type.return_value = (
        MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    )
    with (
        patch(f"{RECALL_MODULE}.get_persona_id", new=AsyncMock(return_value="p1")),
        patch(f"{RECALL_MODULE}.logger") as logger,
    ):
        await handler.handle_memory_recall(event, req)

    engine.search_memories.assert_awaited_once()
    query = engine.search_memories.await_args.kwargs["query"]
    assert len(query) <= 2000
    assert query.startswith("开头：我的咖啡偏好。")
    assert query.endswith("末尾：我平常喜欢喝什么？")
    assert engine.search_memories.await_args.kwargs["persona_id"] == "p1"
    assert engine.search_memories.await_args.kwargs["session_id"] == event.unified_msg_origin
    assert len(req.extra_user_content_parts) == 1
    assert req.prompt == current
    assert event.get_message_str() == current
    if not group:
        stored = handler._memory_recall.conversation_manager.add_message_from_event
        assert stored.await_args.kwargs["content"] == current
    engine.memory_administration.episode_candidates.assert_awaited_once()
    assert engine.memory_administration.episode_candidates.await_args.args[0] == current
    logger.error.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("current", ["我喜欢什么咖啡？", "问" * 1800, "问" * 1999, "问" * 2000])
async def test_context_expansion_reserves_space_for_entire_current_message(current):
    engine = Mock(search_memories=AsyncMock(return_value=[_make_memory()]))
    handler = _make_handler(engine, recall_overrides={"inject_with_recent_context": True})
    manager = handler._memory_recall.conversation_manager
    manager.get_context = AsyncMock(return_value=[
        {"content": current, "timestamp": time.time()},
        {"content": "近" * 3000 + "最近的话题", "timestamp": time.time() - 1},
        {"content": "更旧的话题" * 800, "timestamp": time.time() - 2},
    ])
    event, req = _make_event(), _make_req(current)
    event.get_message_str.return_value = current
    with (
        patch(f"{RECALL_MODULE}.get_persona_id", new=AsyncMock(return_value="p1")),
        patch(f"{RECALL_MODULE}.logger") as logger,
    ):
        await handler.handle_memory_recall(event, req)

    engine.search_memories.assert_awaited_once()
    query = engine.search_memories.await_args.kwargs["query"]
    assert len(query) <= 2000
    assert query.endswith(current)
    assert "更旧的话题" not in query
    if len(current) <= 1800:
        assert "最近的话题" in query
    assert len(req.extra_user_content_parts) == 1
    assert req.prompt == current
    assert manager.add_message_from_event.await_args.kwargs["content"] == current
    logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_time_routing_uses_complete_message_even_when_middle_is_omitted():
    current = "咖啡。" + "背景" * 1000 + "昨天聊了什么咖啡" + "背景" * 1000 + "帮我找找记录。"
    reference = datetime(2026, 10, 26, 5, tzinfo=timezone.utc)
    memory = _make_memory()
    memory.metadata.update({"create_time": reference.timestamp() - 86400,
                            "session_id": "test:private:sid-timeout", "persona_id": "p1"})
    outside = _make_memory()
    outside.doc_id = 100
    outside.content = "今天的记录不应注入昨天的查询"
    outside.metadata = {**memory.metadata, "create_time": reference.timestamp()}
    engine = SimpleNamespace(search_memories=AsyncMock(return_value=[memory, outside]))
    handler = _make_handler(engine)
    event, req = _make_event(), _make_req(current)
    event.get_message_str.return_value = current
    with (
        patch(f"{RECALL_MODULE}.get_persona_id", new=AsyncMock(return_value="p1")),
        patch(f"{UTILS_MODULE}.get_now_datetime", return_value=reference),
        patch(f"{TEMPORAL_MODULE}.search_with_time", wraps=search_with_time) as retrieve,
    ):
        await handler.handle_memory_recall(event, req)

    assert retrieve.await_args.kwargs["time_query"] == current
    assert "昨天" not in engine.search_memories.await_args.kwargs["query"]
    assert len(req.extra_user_content_parts) == 1
    assert outside.content not in req.extra_user_content_parts[0].text
    assert req.prompt == current


@pytest.mark.asyncio
async def test_public_retrieval_still_rejects_overlong_manual_query():
    engine = Mock(search_memories=AsyncMock(return_value=[]))
    with pytest.raises(ValueError, match="at most 2000"):
        await search_with_time(engine, "x" * 2001)
    engine.search_memories.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,size", [(256, 6000), (8000, 6000), (8000, 12000), (32000, 40000)])
async def test_configured_budget_reaches_document_and_graph_vector_backends(limit, size):
    from astrbot_plugin_anamnesis.core.retrieval.vector_retriever import VectorRetriever
    from astrbot_plugin_anamnesis.core.retrieval.graph_vector_retriever import GraphVectorRetriever

    current = "问题开头。" + "字" * size + "问题结尾。"
    documents, graph = Mock(), Mock()
    documents.retrieve = AsyncMock(return_value=[])
    graph.retrieve = AsyncMock(return_value=[])
    document_retriever = VectorRetriever(documents, {"max_query_chars": limit})
    graph_retriever = GraphVectorRetriever(graph, {"max_query_chars": limit})

    async def search(**kwargs):
        await document_retriever.search(**kwargs)
        await graph_retriever.search(**kwargs)
        return [_make_memory()]

    engine = Mock(search_memories=AsyncMock(side_effect=search))
    handler = _make_handler(engine, recall_overrides={"max_query_chars": limit})
    event, req = _make_event(), _make_req(current)
    event.get_message_str.return_value = current
    with patch(f"{RECALL_MODULE}.get_persona_id", new=AsyncMock(return_value="p1")):
        await handler.handle_memory_recall(event, req)

    query = engine.search_memories.await_args.kwargs["query"]
    assert len(query) <= limit
    assert query.startswith("问题开头。") and query.endswith("问题结尾。")
    if len(current) <= limit:
        assert query == current
    assert documents.retrieve.await_args.kwargs["query"] == query
    assert graph.retrieve.await_args.kwargs["query"] == query
    assert req.prompt == current
    assert len(req.extra_user_content_parts) == 1


@pytest.mark.asyncio
async def test_higher_limit_is_shared_by_webui_agentic_tool_and_evaluation():
    import json
    from quart import Quart
    from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
    from astrbot_plugin_anamnesis.core.page_api_modules.recall_handler import RecallHandler
    from astrbot_plugin_anamnesis.core.page_api_modules.utils import PageApiUtils
    from astrbot_plugin_anamnesis.core.retrieval.retrieval_evaluation import evaluate_queries
    from astrbot_plugin_anamnesis.core.tools import agentic_recall

    config = ConfigManager({"recall_engine": {"max_query_chars": 8000}})
    engine = SimpleNamespace(config={"max_query_chars": 8000},
        search_memories=AsyncMock(return_value=[]),
        hybrid_retriever=SimpleNamespace(search=AsyncMock(return_value=[])))
    query = "coffee " * 800
    app, page = Quart(__name__), RecallHandler(PageApiUtils())
    async with app.test_request_context("/recall/test", method="POST",
                                        json={"query": query, "mode": "semantic"}):
        response = await page.test_recall(engine, config)
    assert response["status"] == "ok"
    assert engine.search_memories.await_args.kwargs["query"] == query.strip()

    event, extras = _make_event(), {}
    event.get_extra.side_effect = lambda key, default=None: extras.get(key, default)
    event.set_extra.side_effect = lambda key, value: extras.update({key: value})
    tool = SimpleNamespace(config_manager=config, memory_engine=engine, context=object())
    with patch.object(agentic_recall, "get_persona_id", new=AsyncMock(return_value="p1")):
        result = json.loads(await agentic_recall.agentic_search(tool, event, query, 1, mode="semantic"))
    assert "error" not in result
    report = await evaluate_queries(engine, [{"query": query, "expected_ids": [], "mode": "semantic"}],
                                    config_manager=config)
    assert engine.hybrid_retriever.search.await_args.args[0] == query.strip()
    assert report


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 255, 32001, "bad", float("inf")])
def test_invalid_query_budget_uses_safe_default(value):
    from astrbot_plugin_anamnesis.core.retrieval.query_text import query_char_limit

    assert query_char_limit({"max_query_chars": value}) == 2000
