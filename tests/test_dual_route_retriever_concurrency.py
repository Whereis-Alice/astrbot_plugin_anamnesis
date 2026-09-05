"""Bug J / Bug I 回归测试：dual_route_retriever 并发加载与多语言 intent 词表。

Bug J：原实现在合并循环里逐条 await memory_loader(doc_id)，N 条候选就是 N 次串行
DB 往返。改成受限并发（Semaphore(8)）后，返回顺序、异常类型与「loader 返回空则
跳过」的语义都必须与串行版完全一致。
Bug I：intent 关键词表原先只硬编码中英文，插件却带俄语 i18n，俄语查询的路由权重
调整完全失效。
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from astrbot_plugin_anamnesis.core.retrieval.dual_route_retriever import (
    INTENT_TERMS,
    DualRouteRetriever,
)
from astrbot_plugin_anamnesis.core.retrieval.graph_retriever import GraphResult
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import (
    HybridResult,
    HybridRetriever,
)


class _DocRoute:
    """文档路假实现；populated=False 时 content/metadata 为空以强制触发 loader。"""

    def __init__(
        self, scored_ids: list[tuple[int, float]], *, populated: bool = False
    ) -> None:
        self.scored_ids = scored_ids
        self.populated = populated

    async def search(self, query, k, session_id=None, persona_id=None):
        return [
            HybridResult(
                doc_id=doc_id,
                final_score=score,
                rrf_score=score,
                bm25_score=score,
                vector_score=None,
                content=f"doc {doc_id}" if self.populated else "",
                metadata={"source": "doc"} if self.populated else {},
            )
            for doc_id, score in self.scored_ids
        ]


class _GraphRoute:
    """图路假实现。"""

    def __init__(self, scored_ids: list[tuple[int, float]]) -> None:
        self.scored_ids = scored_ids

    async def search(self, query, k, session_id=None, persona_id=None):
        return [
            GraphResult(
                doc_id=doc_id,
                final_score=score,
                rrf_score=score,
                keyword_score=score,
                vector_score=None,
                content="",
                metadata={},
            )
            for doc_id, score in self.scored_ids
        ]


class _CountingLoader:
    """记录调用顺序与并发峰值的 memory_loader。"""

    def __init__(
        self,
        *,
        missing: set[int] | None = None,
        failing: set[int] | None = None,
        delay: float = 0.01,
    ) -> None:
        self.missing = missing or set()
        self.failing = failing or set()
        self.delay = delay
        self.calls: list[int] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, doc_id: int) -> dict[str, Any] | None:
        self.calls.append(doc_id)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if doc_id in self.failing:
                raise ValueError(f"loader boom {doc_id}")
            if doc_id in self.missing:
                return None
            return {"text": f"memory {doc_id}", "metadata": {"loaded": doc_id}}
        finally:
            self.in_flight -= 1


def _make_retriever(
    loader,
    doc_scores: list[tuple[int, float]],
    graph_scores: list[tuple[int, float]],
    *,
    populated: bool = False,
    config: dict[str, Any] | None = None,
) -> DualRouteRetriever:
    return DualRouteRetriever(
        document_retriever=cast(
            HybridRetriever, _DocRoute(doc_scores, populated=populated)
        ),
        graph_retriever=cast(Any, _GraphRoute(graph_scores)),
        memory_loader=loader,
        config={
            "document_route_weight": 0.65,
            "graph_route_weight": 0.35,
            "cross_route_bonus": 0,
            "dynamic_route_weighting": True,
            **(config or {}),
        },
    )


# ── Bug J：memory_loader 并发化 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_loader_calls_run_concurrently():
    """多条候选必须并发加载，而不是逐条串行 await。"""
    loader = _CountingLoader()
    retriever = _make_retriever(
        loader, [(1, 1.0), (2, 0.8), (3, 0.6), (4, 0.4)], [(1, 1.0)]
    )

    results = await retriever.search("query", k=10)

    assert sorted(loader.calls) == [1, 2, 3, 4]
    assert loader.max_in_flight > 1, "loader 仍在串行执行"
    assert [item.doc_id for item in results] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_memory_loader_concurrency_is_capped():
    """并发要有上限（默认 8），避免瞬间打爆 DB 连接池。"""
    loader = _CountingLoader()
    doc_scores = [(doc_id, 1.0 - doc_id * 0.01) for doc_id in range(1, 21)]
    retriever = _make_retriever(loader, doc_scores, [(1, 1.0)])

    await retriever.search("query", k=20)

    assert len(loader.calls) == 20
    assert 1 < loader.max_in_flight <= 8


@pytest.mark.asyncio
async def test_loader_returning_none_skips_document():
    """loader 返回空的候选必须被跳过（与串行版一致）。"""
    loader = _CountingLoader(missing={2})
    retriever = _make_retriever(loader, [(1, 1.0), (2, 0.8), (3, 0.6)], [(1, 1.0)])

    results = await retriever.search("query", k=10)

    assert [item.doc_id for item in results] == [1, 3]


@pytest.mark.asyncio
async def test_loader_exception_propagates_with_original_type():
    """并发化不得吞掉或包装 loader 抛出的异常。"""
    loader = _CountingLoader(failing={3})
    retriever = _make_retriever(loader, [(1, 1.0), (2, 0.8), (3, 0.6)], [(1, 1.0)])

    with pytest.raises(ValueError, match="loader boom 3"):
        await retriever.search("query", k=10)


@pytest.mark.asyncio
async def test_loaded_memory_replaces_empty_content_and_metadata():
    loader = _CountingLoader()
    retriever = _make_retriever(loader, [(1, 1.0)], [(1, 1.0)])

    results = await retriever.search("query", k=10)

    assert results[0].content == "memory 1"
    assert results[0].metadata == {"loaded": 1}


@pytest.mark.asyncio
async def test_no_loader_call_when_document_result_is_complete():
    """文档路已带 content+metadata 时不触发任何加载。"""
    loader = _CountingLoader()
    retriever = _make_retriever(
        loader, [(1, 1.0), (2, 0.8)], [(1, 1.0)], populated=True
    )

    results = await retriever.search("query", k=10)

    assert loader.calls == []
    assert [item.doc_id for item in results] == [1, 2]
    assert results[0].content == "doc 1"


@pytest.mark.asyncio
async def test_returns_empty_when_both_routes_empty():
    """删除死代码后行为不变：两路皆空仍返回空列表。"""
    loader = _CountingLoader()
    retriever = _make_retriever(loader, [], [])

    assert await retriever.search("query", k=5) == []
    assert loader.calls == []


@pytest.mark.asyncio
async def test_empty_graph_route_short_circuits_to_document_results():
    """图路为空时直接返回文档路结果，不做融合、不加载记忆。"""
    loader = _CountingLoader()
    retriever = _make_retriever(
        loader, [(1, 1.0), (2, 0.8), (3, 0.6)], [], populated=True
    )

    results = await retriever.search("query", k=2)

    assert [item.doc_id for item in results] == [1, 2]
    assert loader.calls == []


# ── Bug I：多语言 intent 词表 ───────────────────────────────────────────────


def _weights(query: str, dynamic: bool = True) -> tuple[float, float, str]:
    retriever = _make_retriever(
        _CountingLoader(), [], [], config={"dynamic_route_weighting": dynamic}
    )
    return retriever._route_weights_for_query(query)


def test_intent_terms_cover_all_shipped_languages():
    """插件带 zh/en/ru 三种 i18n，词表必须三语齐全。"""
    assert set(INTENT_TERMS) >= {"zh", "en", "ru"}
    for lang, groups in INTENT_TERMS.items():
        assert set(groups) == {"relation", "temporal", "factual"}, lang
        for name, terms in groups.items():
            assert terms, f"{lang}.{name} 词表为空"


def test_intent_terms_keep_original_chinese_and_english_entries():
    """提取常量不得丢失原有中英文词条。"""
    assert "关系" in INTENT_TERMS["zh"]["relation"]
    assert "什么时候" in INTENT_TERMS["zh"]["temporal"]
    assert "是什么" in INTENT_TERMS["zh"]["factual"]
    assert "partner" in INTENT_TERMS["en"]["relation"]
    assert "yesterday" in INTENT_TERMS["en"]["temporal"]
    assert "what is" in INTENT_TERMS["en"]["factual"]


def test_russian_relationship_query_promotes_graph_route():
    document_weight, graph_weight, intent = _weights("с кем я разговаривал")

    assert intent == "relationship"
    assert graph_weight > 0.35
    assert document_weight < 0.65


def test_russian_temporal_query_is_detected():
    _, graph_weight, intent = _weights("когда мы говорили в прошлый раз")

    assert intent == "temporal"
    assert graph_weight > 0.35


def test_russian_factual_query_promotes_document_route():
    document_weight, _, intent = _weights("что такое эмбеддинг")

    assert intent == "factual"
    assert document_weight > 0.65


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("我和张三是什么关系", "relationship"),
        ("who is my partner", "relationship"),
        ("what is rrf", "factual"),
        ("上次我们聊了什么", "temporal"),
        ("今天天气不错", "default"),
    ],
)
def test_existing_language_intents_unchanged(query: str, expected: str):
    """俄语词条并入后，中英文与无意图查询的判定结果必须保持原样。"""
    assert _weights(query)[2] == expected


def test_fixed_intent_when_dynamic_weighting_disabled():
    document_weight, graph_weight, intent = _weights(
        "我和张三是什么关系", dynamic=False
    )

    assert intent == "fixed"
    assert (document_weight, graph_weight) == (0.65, 0.35)
