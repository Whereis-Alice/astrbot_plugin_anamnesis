"""Graph gating and independently labeled retrieval metric regressions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.retrieval.dual_route_retriever import (
    DualRouteRetriever,
)
from astrbot_plugin_anamnesis.core.retrieval.graph_evidence import graph_evidence
from astrbot_plugin_anamnesis.core.retrieval.graph_retriever import GraphResult
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_anamnesis.core.retrieval.retrieval_evaluation import (
    evaluate_queries,
    ranking_metrics,
)


def _memory(memory_id, content="Alice likes coffee"):
    return HybridResult(
        memory_id,
        1.0,
        0.1,
        0.9,
        0.9,
        content,
        {"session_id": "scope", "persona_id": "p", "status": "active"},
    )


def test_graph_literal_support_is_not_a_truth_probability():
    supported = graph_evidence(
        "Alice coffee", "Alice likes coffee", "Alice likes coffee and tea"
    )
    weak = graph_evidence("Alice coffee", "Alice married Bob", "Alice likes coffee")
    missing = graph_evidence("咖啡", "爱丽丝喜欢咖啡", "")
    assert supported["graph_evidence_gate"] == 1
    assert weak["graph_evidence_gate"] == 0.25
    assert missing["graph_evidence_reason"] == "source_unavailable"
    assert (
        "咖啡"
        in graph_evidence("咖啡", "爱丽丝喜欢咖啡", "她喜欢咖啡")[
            "graph_evidence_matched_tokens"
        ]
    )


@pytest.mark.asyncio
async def test_graph_only_candidate_uses_parent_evidence_instead_of_self_support():
    doc = _memory(1)
    graph = GraphResult(
        2,
        1,
        0.1,
        0.9,
        0.9,
        "Alice married Bob",
        {"session_id": "scope", "persona_id": "p"},
    )
    document_retriever = SimpleNamespace(search=AsyncMock(return_value=[doc]))
    graph_retriever = SimpleNamespace(search=AsyncMock(return_value=[graph]))
    loader = AsyncMock(
        return_value={"text": "Alice likes coffee", "metadata": doc.metadata}
    )
    retriever = DualRouteRetriever(
        document_retriever,
        graph_retriever,
        loader,
        {"graph_evidence_enabled": True, "dynamic_route_weighting": False},
    )
    results = await retriever.search("coffee", k=2, session_id="scope", persona_id="p")
    by_id = {item.doc_id: item for item in results}
    assert by_id[1].final_score == 0.65
    assert by_id[2].final_score == pytest.approx(0.35 * 0.25)
    assert (
        by_id[2].score_breakdown["graph_evidence_reason"]
        == "weak_literal_source_support"
    )
    assert loader.await_count == 1


@pytest.mark.asyncio
async def test_disabled_graph_evidence_preserves_existing_scores_and_loader_cost():
    doc = _memory(1)
    graph = GraphResult(1, 1, 0.1, 0.9, 0.9, "Alice married Bob", doc.metadata)
    loader = AsyncMock()
    retriever = DualRouteRetriever(
        SimpleNamespace(search=AsyncMock(return_value=[doc])),
        SimpleNamespace(search=AsyncMock(return_value=[graph])),
        loader,
        {"dynamic_route_weighting": False},
    )
    results = await retriever.search("coffee", k=2)
    assert results[0].final_score == 1
    assert "graph_evidence_gate" not in results[0].score_breakdown
    loader.assert_not_awaited()


def test_metrics_distinguish_hit_precision_recall_and_reciprocal_rank():
    value = ranking_metrics([99, 1, 2], [1, 2, 3, 4], 3)
    assert value["hit_at_k"] == 1
    assert value["precision_at_k"] == pytest.approx(2 / 3)
    assert value["recall_at_k"] == 0.5
    assert value["reciprocal_rank"] == 0.5
    assert ranking_metrics([1, 1, 1], [1, 2], 3)["recall_at_k"] == 0.5
    assert ranking_metrics([], [], 3)["correct_empty"] is True


@pytest.mark.asyncio
async def test_evaluation_uses_read_only_retrievers_and_includes_failures_in_quality():
    retriever = SimpleNamespace(
        search=AsyncMock(
            side_effect=[[_memory(99), _memory(1)], RuntimeError("secret failure"), []]
        )
    )
    access = AsyncMock()
    engine = SimpleNamespace(
        hybrid_retriever=retriever,
        config={},
        search_memories=access,
        _schedule_access_updates=access,
    )
    report = await evaluate_queries(
        engine,
        [
            {"query": "coffee", "expected_ids": [1, 2], "mode": "semantic"},
            {"query": "tea", "expected_ids": [2], "mode": "semantic"},
            {"query": "missing", "expected_ids": [], "mode": "semantic"},
        ],
        k=3,
        session_id="scope",
        persona_id="p",
    )
    assert report["summary"]["hit_at_k"] == 0.5
    assert report["summary"]["precision_at_k"] == pytest.approx(1 / 6)
    assert report["summary"]["recall_at_k"] == 0.25
    assert report["summary"]["mrr"] == 0.25
    assert report["summary"]["errors"] == 1
    assert report["summary"]["empty_accuracy"] == 1
    assert "secret failure" not in str(report)
    assert report["read_only"] is True and report["settings_applied"] is False
    access.assert_not_awaited()
    assert retriever.search.await_args.args[2:] == ("scope", "p")


@pytest.mark.asyncio
async def test_evaluation_cancellation_settles_active_case():
    settled = asyncio.Event()

    async def slow(*args):
        try:
            await asyncio.sleep(10)
        finally:
            settled.set()

    engine = SimpleNamespace(config={}, hybrid_retriever=SimpleNamespace(search=slow))
    task = asyncio.create_task(
        evaluate_queries(engine, [{"query": "coffee", "expected_ids": [1]}])
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert settled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cases", [[], [{"query": "x"}], [{"query": "x", "expected_ids": [True]}]]
)
async def test_evaluation_rejects_unlabeled_or_invalid_cases(cases):
    with pytest.raises(ValueError):
        await evaluate_queries(SimpleNamespace(config={}), cases)
