"""Fuse document-route and graph-route retrieval into one result list."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

from .graph_retriever import GraphRetriever
from .hybrid_retriever import HybridResult, HybridRetriever

# Bug I：intent 关键词表原先以局部元组硬编码在 `_route_weights_for_query` 内，
# 只覆盖中英文；插件却随包发布了俄语 i18n（core/i18n/ru.json），俄语用户的意图
# 路由权重调整完全失效。这里外提成模块级常量，方便补充语种与单测直接断言。
INTENT_TERMS: dict[str, dict[str, tuple[str, ...]]] = {
    "zh": {
        "relation": (
            "谁",
            "和谁",
            "关系",
            "认识",
            "朋友",
            "同事",
            "家人",
            "父母",
            "妈妈",
            "爸爸",
            "老师",
            "同学",
        ),
        "temporal": (
            "上次",
            "昨天",
            "前天",
            "刚才",
            "之前",
            "什么时候",
            "哪天",
            "最近",
        ),
        "factual": (
            "是什么",
            "什么是",
            "解释",
            "定义",
            "怎么",
            "如何",
        ),
    },
    "en": {
        "relation": (
            "partner",
            "friend",
            "relationship",
            "with whom",
        ),
        "temporal": (
            "last time",
            "yesterday",
            "recently",
            "when",
        ),
        "factual": (
            "why",
            "what is",
            "explain",
            "define",
            "how to",
        ),
    },
    # 俄语词条统一取词干（如 "отношени" 覆盖 отношение/отношения/отношений），
    # 与中英文一致地走子串匹配。刻意不收录裸词 "друг"：它是 "другой/другие"
    # （其他/别的）的前缀，会在大量无关查询上误命中，改用 "друзья"/"друж"/"подруг"。
    "ru": {
        "relation": (
            "кто",
            "с кем",
            "отношени",
            "знаком",
            "друзья",
            "друж",
            "подруг",
            "коллег",
            "семья",
            "семьи",
            "родител",
            "мама",
            "папа",
            "учител",
            "одноклассник",
            "партнёр",
            "партнер",
        ),
        "temporal": (
            "в прошлый раз",
            "вчера",
            "позавчера",
            "только что",
            "раньше",
            "когда",
            "недавно",
        ),
        "factual": (
            "что такое",
            "объясни",
            "определение",
            "как",
            "почему",
            "зачем",
        ),
    },
}


def _merge_intent_terms(group: str) -> tuple[str, ...]:
    """求某一意图分组的跨语言并集（按声明顺序去重）。"""
    merged: list[str] = []
    for language_terms in INTENT_TERMS.values():
        for term in language_terms.get(group, ()):
            if term not in merged:
                merged.append(term)
    return tuple(merged)


# core/i18n_backend.py 只维护模块私有的 `_current_lang`，没有公开 getter；而且插件
# 语言设置决定的是「bot 回复用什么语言」，并不等于「用户此刻用什么语言提问」。
# 因此这里对全部语种求并集匹配：不同语种词表字符集互不重叠（西里尔词条不可能命中
# 中英查询，反之亦然），既拿到俄语覆盖，又严格不劣于只有中英词表的现状。
_RELATION_TERMS: tuple[str, ...] = _merge_intent_terms("relation")
_TEMPORAL_TERMS: tuple[str, ...] = _merge_intent_terms("temporal")
_FACTUAL_TERMS: tuple[str, ...] = _merge_intent_terms("factual")


class DualRouteRetriever:
    """Coordinate document and graph retrieval routes."""

    def __init__(
        self,
        document_retriever: HybridRetriever,
        graph_retriever: GraphRetriever,
        memory_loader: Callable[[int], Awaitable[dict[str, Any] | None]],
        config: dict[str, Any] | None = None,
    ):
        self.document_retriever = document_retriever
        self.graph_retriever = graph_retriever
        self.memory_loader = memory_loader
        self.config = config or {}
        self.document_route_weight = float(
            self.config.get("document_route_weight", 0.65)
        )
        self.graph_route_weight = float(self.config.get("graph_route_weight", 0.35))
        self.cross_route_bonus = float(self.config.get("cross_route_bonus", 0.08))
        self.dynamic_route_weighting = bool(
            self.config.get("dynamic_route_weighting", True)
        )

    async def search(
        self,
        query: str,
        k: int = 10,
        session_id: str | None = None,
        persona_id: str | None = None,
    ) -> list[HybridResult]:
        """Run both retrieval routes and merge their memory candidates."""
        doc_results, graph_results = await asyncio.gather(
            self.document_retriever.search(
                query, max(k * 2, k), session_id, persona_id
            ),
            self.graph_retriever.search(query, max(k * 2, k), session_id, persona_id),
        )

        if not graph_results:
            return doc_results[:k]

        document_weight, graph_weight, intent = self._route_weights_for_query(query)

        document_max = (
            max((item.final_score for item in doc_results), default=1.0) or 1.0
        )
        graph_max = (
            max((item.final_score for item in graph_results), default=1.0) or 1.0
        )

        doc_map = {item.doc_id: item for item in doc_results}
        graph_map = {item.doc_id: item for item in graph_results}
        # Bug J：把候选顺序固化成一个 list，预扫描、并发加载与合并循环共用同一
        # 迭代顺序（即原实现的 set 迭代顺序），保证合并结果与串行版逐位一致。
        all_doc_ids = list(set(doc_map) | set(graph_map))

        # Bug J：原实现在合并循环里逐条 `await self.memory_loader(doc_id)`，N 条
        # 候选就是 N 次串行 DB 往返。先算出每条候选自带的正文/元数据，挑出真正
        # 缺失的那些，再一次性受限并发加载。
        payloads: dict[int, tuple[str, dict[str, Any]]] = {}
        pending_ids: list[int] = []
        for doc_id in all_doc_ids:
            candidate = doc_map.get(doc_id)
            candidate_content = candidate.content if candidate is not None else ""
            candidate_metadata = (
                dict(candidate.metadata)
                if candidate is not None and isinstance(candidate.metadata, dict)
                else {}
            )
            payloads[doc_id] = (candidate_content, candidate_metadata)
            if not candidate_content or not candidate_metadata:
                pending_ids.append(doc_id)

        loaded = await self._load_memories_concurrently(pending_ids)

        merged_results: list[HybridResult] = []
        for doc_id in all_doc_ids:
            doc_result = doc_map.get(doc_id)
            graph_result = graph_map.get(doc_id)

            doc_signal = (
                doc_result.final_score / document_max if doc_result is not None else 0.0
            )
            graph_signal = (
                graph_result.final_score / graph_max
                if graph_result is not None
                else 0.0
            )
            route_bonus = (
                self.cross_route_bonus
                if doc_result is not None and graph_result is not None
                else 0.0
            )

            memory_content, memory_metadata = payloads[doc_id]

            if not memory_content or not memory_metadata:
                memory = loaded.get(doc_id)
                if not memory:
                    continue
                memory_content = str(memory.get("text") or memory_content)
                raw_metadata = memory.get("metadata") or memory_metadata
                memory_metadata = raw_metadata if isinstance(raw_metadata, dict) else {}

            final_score = min(
                1.0,
                document_weight * doc_signal
                + graph_weight * graph_signal
                + route_bonus,
            )

            score_breakdown: dict[str, float] = {}
            if doc_result and doc_result.score_breakdown:
                score_breakdown.update(doc_result.score_breakdown)
            if graph_result and graph_result.score_breakdown:
                score_breakdown.update(graph_result.score_breakdown)
            score_breakdown.update(
                {
                    "document_route_score": round(doc_signal, 4),
                    "graph_route_score": round(graph_signal, 4),
                    "document_route_weight": round(document_weight, 4),
                    "graph_route_weight": round(graph_weight, 4),
                    "cross_route_bonus": round(route_bonus, 4),
                    "dual_route_final_score": round(final_score, 4),
                }
            )
            if intent:
                score_breakdown["query_intent"] = intent
            if doc_result is not None:
                score_breakdown["document_keyword_score"] = round(
                    float(doc_result.bm25_score or 0.0),
                    4,
                )
                score_breakdown["document_vector_score"] = round(
                    float(doc_result.vector_score or 0.0),
                    4,
                )
            if graph_result is not None:
                score_breakdown["graph_keyword_score"] = round(
                    float(graph_result.keyword_score or 0.0),
                    4,
                )
                score_breakdown["graph_vector_score"] = round(
                    float(graph_result.vector_score or 0.0),
                    4,
                )

            merged_results.append(
                HybridResult(
                    doc_id=doc_id,
                    final_score=final_score,
                    rrf_score=max(
                        doc_result.rrf_score if doc_result is not None else 0.0,
                        graph_result.rrf_score if graph_result is not None else 0.0,
                    ),
                    bm25_score=doc_result.bm25_score
                    if doc_result is not None
                    else None,
                    vector_score=(
                        doc_result.vector_score if doc_result is not None else None
                    ),
                    content=memory_content,
                    metadata=memory_metadata,
                    score_breakdown=score_breakdown,
                )
            )

        merged_results.sort(key=lambda item: item.final_score, reverse=True)
        return merged_results[:k]

    async def _load_memories_concurrently(
        self, doc_ids: list[int], max_concurrency: int = 8
    ) -> dict[int, dict[str, Any] | None]:
        """并发补齐缺失的记忆正文，返回 doc_id -> 记忆映射。

        Bug J：替代原来在合并循环里逐条 await 的 N+1 串行加载。用 Semaphore 限制
        同时在飞的加载数，避免一次召回就把 DB 连接池打满。语义与串行版保持一致：
        loader 返回空的候选照旧会被主循环跳过，loader 抛出的异常按候选顺序取第一个
        原样重抛（不包装、不改类型）。"""
        if not doc_ids:
            return {}

        semaphore = asyncio.Semaphore(max(1, max_concurrency))

        async def _load(doc_id: int) -> dict[str, Any] | None:
            async with semaphore:
                return await self.memory_loader(doc_id)

        results = await asyncio.gather(
            *(_load(doc_id) for doc_id in doc_ids), return_exceptions=True
        )

        loaded: dict[int, dict[str, Any] | None] = {}
        first_error: BaseException | None = None
        for doc_id, result in zip(doc_ids, results, strict=True):
            if isinstance(result, BaseException):
                if first_error is None:
                    first_error = result
                logger.debug(
                    f"[DualRouteRetriever] 记忆加载失败 (doc_id={doc_id})",
                    exc_info=result,
                )
                continue
            loaded[doc_id] = result

        if first_error is not None:
            raise first_error
        return loaded

    def _route_weights_for_query(self, query: str) -> tuple[float, float, str]:
        """Adjust document/graph weights with lightweight query intent rules."""
        base_document = self.document_route_weight
        base_graph = self.graph_route_weight
        if not self.dynamic_route_weighting:
            return base_document, base_graph, "fixed"

        normalized = query.casefold()
        relation_hit = any(term in normalized for term in _RELATION_TERMS)
        temporal_hit = any(term in normalized for term in _TEMPORAL_TERMS)
        factual_hit = any(term in normalized for term in _FACTUAL_TERMS)

        document_weight = base_document
        graph_weight = base_graph
        intent = "default"

        if relation_hit:
            graph_weight += 0.2
            document_weight -= 0.2
            intent = "relationship"
        if temporal_hit:
            graph_weight += 0.1
            document_weight -= 0.1
            intent = "temporal" if intent == "default" else f"{intent}+temporal"
        if factual_hit and not relation_hit:
            document_weight += 0.15
            graph_weight -= 0.15
            intent = "factual" if intent == "default" else f"{intent}+factual"

        document_weight = max(0.15, min(0.9, document_weight))
        graph_weight = max(0.1, min(0.85, graph_weight))
        total = document_weight + graph_weight
        if total <= 0:
            return base_document, base_graph, "fixed"
        return document_weight / total, graph_weight / total, intent


__all__ = ["INTENT_TERMS", "DualRouteRetriever"]
