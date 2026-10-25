"""Native authenticated recall endpoint shares the production time path."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.page_api_modules.recall_handler import RecallHandler
from astrbot_plugin_anamnesis.core.page_api_modules.utils import PageApiUtils
from quart import Quart

from tests.test_temporal_retrieval import (
    _put,
)
from tests.test_temporal_retrieval import (
    engine as _engine_fixture,
)

engine = _engine_fixture


@pytest.mark.asyncio
async def test_recall_page_accepts_empty_time_query_and_filters_scope_persona(engine):
    await _put(engine, 1)
    await _put(engine, 2, persona_id="other")
    await _put(engine, 3, session_id="other")
    handler, app = RecallHandler(PageApiUtils()), Quart(__name__)
    context = SimpleNamespace(get_config=lambda origin: {"timezone": "UTC"})
    async with app.test_request_context(
        "/recall/test",
        method="POST",
        json={
            "query": "",
            "mode": "time",
            "start_time": "100",
            "end_time": "200",
            "session_id": "scope",
            "persona_id": "p",
        },
    ):
        response = await handler.test_recall(engine, ConfigManager(), context)
    assert response["status"] == "ok"
    assert [item["memory_id"] for item in response["data"]["results"]] == [1]
    assert response["data"]["retrieval"]["time_range"]["timezone"] == "UTC"
    assert response["data"]["results"][0]["metadata"]["time_basis"] == "create_time"
    engine.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_blank_persona_queries_all_personas(engine):
    await _put(engine, 1)
    await _put(engine, 2, persona_id="other")
    handler, app = RecallHandler(PageApiUtils()), Quart(__name__)
    async with app.test_request_context(
        "/recall/test",
        method="POST",
        json={
            "query": "",
            "mode": "time",
            "start_time": "100",
            "end_time": "200",
            "session_id": "scope",
        },
    ):
        response = await handler.test_recall(engine, ConfigManager())
    assert {item["memory_id"] for item in response["data"]["results"]} == {1, 2}


@pytest.mark.asyncio
async def test_page_recall_preserves_graph_explanation_strings_and_tokens():
    from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridResult

    result = HybridResult(
        1,
        0.5,
        0.1,
        0.4,
        0.3,
        "coffee",
        {},
        {
            "graph_evidence_reason": "weak_literal_source_support",
            "graph_evidence_matched_tokens": ["<script>"],
            "graph_evidence_gate": 0.25,
        },
    )
    engine = SimpleNamespace(search_memories=AsyncMock(return_value=[result]))
    handler, app = RecallHandler(PageApiUtils()), Quart(__name__)
    async with app.test_request_context(
        "/recall/test", method="POST", json={"query": "coffee", "mode": "semantic"}
    ):
        response = await handler.test_recall(engine)
    breakdown = response["data"]["results"][0]["score_breakdown"]
    assert breakdown["graph_evidence_reason"] == "weak_literal_source_support"
    assert breakdown["graph_evidence_matched_tokens"] == ["<script>"]


@pytest.mark.asyncio
async def test_page_recall_reports_exclusive_invalid_boundary_and_rejects_nonobject():
    handler, app = RecallHandler(PageApiUtils()), Quart(__name__)
    async with app.test_request_context(
        "/recall/test",
        method="POST",
        json={"mode": "time", "start_time": "200", "end_time": "100"},
    ):
        response = await handler.test_recall(SimpleNamespace())
    assert response["status"] == "error" and "before" in response["message"]
    async with app.test_request_context("/recall/test", method="POST", json=["bad"]):
        response = await handler.test_recall(SimpleNamespace())
    assert response["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_k", [True, False, 5.1, "5", None, [], {}])
async def test_page_recall_rejects_k_coercion(invalid_k):
    handler, app = RecallHandler(PageApiUtils()), Quart(__name__)
    async with app.test_request_context(
        "/recall/test", method="POST", json={"query": "coffee", "k": invalid_k}
    ):
        response = await handler.test_recall(SimpleNamespace())
    assert response["status"] == "error" and "k" in response["message"]
