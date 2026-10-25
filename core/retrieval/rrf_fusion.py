"""
RRF融合器 - Reciprocal Rank Fusion
实现纯Python的结果融合算法,用于合并BM25和向量检索结果
"""

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class BM25Result:
    """BM25检索结果"""

    doc_id: int
    score: float
    content: str
    metadata: dict[str, Any]


@dataclass
class VectorResult:
    """向量检索结果"""

    doc_id: int
    score: float
    content: str
    metadata: dict[str, Any]


@dataclass
class FusedResult:
    """融合后的检索结果"""

    doc_id: int
    rrf_score: float  # RRF融合分数
    bm25_score: float | None  # 原始BM25分数
    vector_score: float | None  # 原始向量分数
    content: str
    metadata: dict[str, Any]
    rerank_score: float | None = None  # Rerank重排序分数(已归一化到[0,1],未重排序时为None)


class RRFFusion:
    """
    RRF结果融合器

    使用Reciprocal Rank Fusion算法融合多路检索结果
    RRF公式: score(d) = Σ 1/(k + rank_i(d))

    参考文献:
    Cormack, G. V., Clarke, C. L., & Buettcher, S. (2009).
    Reciprocal rank fusion outperforms condorcet and individual rank learning methods.
    """

    def __init__(self, k: int = 60):
        """
        初始化RRF融合器

        Args:
            k: RRF参数,控制排名衰减速度(默认60,论文推荐值)
        """
        self.k = k

    def fuse(
        self,
        bm25_results: list[BM25Result],
        vector_results: list[VectorResult],
        top_k: int,
    ) -> list[FusedResult]:
        """
        融合两路检索结果

        Args:
            bm25_results: BM25检索结果列表(按分数降序排列)
            vector_results: 向量检索结果列表(按分数降序排列)
            top_k: 返回的结果数量

        Returns:
            List[FusedResult]: 融合后的结果,按RRF分数降序排列
        """
        # 处理空列表情况
        if not bm25_results and not vector_results:
            return []

        # 如果只有一路结果,直接转换并返回
        if not bm25_results:
            return self._convert_vector_only(vector_results, top_k)
        if not vector_results:
            return self._convert_bm25_only(bm25_results, top_k)

        # 构建文档索引:收集所有出现的文档ID
        all_doc_ids = set()
        for result in bm25_results:
            all_doc_ids.add(result.doc_id)
        for result in vector_results:
            all_doc_ids.add(result.doc_id)

        # 构建排名映射
        bm25_rank_map = {
            result.doc_id: rank for rank, result in enumerate(bm25_results)
        }
        vector_rank_map = {
            result.doc_id: rank for rank, result in enumerate(vector_results)
        }

        # 构建文档内容和分数映射
        doc_content_map = {}
        doc_metadata_map = {}
        bm25_score_map = {}
        vector_score_map = {}

        for result in bm25_results:
            doc_content_map[result.doc_id] = result.content
            doc_metadata_map[result.doc_id] = result.metadata
            bm25_score_map[result.doc_id] = result.score

        for result in vector_results:
            if result.doc_id not in doc_content_map:
                doc_content_map[result.doc_id] = result.content
                doc_metadata_map[result.doc_id] = result.metadata
            vector_score_map[result.doc_id] = result.score

        # 计算每个文档的RRF分数
        fused_scores = {}
        for doc_id in all_doc_ids:
            rrf_score = 0.0

            # 来自BM25的贡献
            if doc_id in bm25_rank_map:
                rank = bm25_rank_map[doc_id]
                rrf_score += 1.0 / (self.k + rank + 1)  # rank从0开始,+1转换为1-based

            # 来自向量检索的贡献
            if doc_id in vector_rank_map:
                rank = vector_rank_map[doc_id]
                rrf_score += 1.0 / (self.k + rank + 1)

            fused_scores[doc_id] = rrf_score

        # 按RRF分数降序排序
        sorted_doc_ids = sorted(
            all_doc_ids, key=lambda doc_id: fused_scores[doc_id], reverse=True
        )

        # 构建融合结果
        fused_results = []
        for doc_id in sorted_doc_ids[:top_k]:
            fused_results.append(
                FusedResult(
                    doc_id=doc_id,
                    rrf_score=fused_scores[doc_id],
                    bm25_score=bm25_score_map.get(doc_id),
                    vector_score=vector_score_map.get(doc_id),
                    content=doc_content_map[doc_id],
                    metadata=doc_metadata_map[doc_id],
                )
            )

        return fused_results

    def fuse_scores(
        self, bm25_results: list[BM25Result], vector_results: list[VectorResult],
        top_k: int, bm25_weight: float = 0.5, agreement_bonus: float = 0.0,
    ) -> list[FusedResult]:
        """Fuse normalized relevance and reward close cross-route ranks."""
        if not bm25_results and not vector_results:
            return []

        def finite(value, default=0.0):
            try:
                number = float(value)
                return number if math.isfinite(number) else default
            except (TypeError, ValueError, OverflowError):
                return default

        weight = max(0.0, min(1.0, finite(bm25_weight, 0.5)))
        bonus = max(0.0, min(1.0, finite(agreement_bonus)))
        bm25_rank = {r.doc_id: rank for rank, r in enumerate(bm25_results)}
        vector_rank = {r.doc_id: rank for rank, r in enumerate(vector_results)}

        def normalize(results):
            values = {r.doc_id: finite(r.score) for r in results}
            if not values:
                return {}
            low, high = min(values.values()), max(values.values())
            if high-low < 1e-9:
                return dict.fromkeys(values, 1.0)
            return {doc_id: (score-low)/(high-low) for doc_id, score in values.items()}

        bm25_norm, vector_norm = normalize(bm25_results), normalize(vector_results)
        bm25_scores = {r.doc_id: r.score for r in bm25_results}
        vector_scores = {r.doc_id: r.score for r in vector_results}
        payloads = {}
        for result in [*bm25_results, *vector_results]:
            payloads.setdefault(result.doc_id, (result.content, result.metadata))
        scores, best_rank = {}, {}
        span = max(len(bm25_results), len(vector_results), 1)
        for doc_id in set(bm25_norm) | set(vector_norm):
            total, denominator = 0.0, 0.0
            if doc_id in bm25_norm:
                total += weight * bm25_norm[doc_id]
                denominator += weight
            if doc_id in vector_norm:
                total += (1.0-weight) * vector_norm[doc_id]
                denominator += 1.0-weight
            score = total/denominator if denominator else 0.0
            if doc_id in bm25_rank and doc_id in vector_rank:
                score += bonus * (1.0-abs(bm25_rank[doc_id]-vector_rank[doc_id])/span)
            scores[doc_id] = score
            best_rank[doc_id] = min(
                bm25_rank.get(doc_id, len(bm25_results)+1),
                vector_rank.get(doc_id, len(vector_results)+1),
            )
        ordered = sorted(scores, key=lambda doc_id: (-scores[doc_id], best_rank[doc_id], doc_id))[:top_k]
        return [FusedResult(
            doc_id=doc_id, rrf_score=scores[doc_id], bm25_score=bm25_scores.get(doc_id),
            vector_score=vector_scores.get(doc_id), content=payloads[doc_id][0],
            metadata=payloads[doc_id][1],
        ) for doc_id in ordered]

    def _convert_bm25_only(
        self, bm25_results: list[BM25Result], top_k: int
    ) -> list[FusedResult]:
        """仅有BM25结果时的转换"""
        return [
            FusedResult(
                doc_id=result.doc_id,
                rrf_score=1.0 / (self.k + rank + 1),
                bm25_score=result.score,
                vector_score=None,
                content=result.content,
                metadata=result.metadata,
            )
            for rank, result in enumerate(bm25_results[:top_k])
        ]

    def _convert_vector_only(
        self, vector_results: list[VectorResult], top_k: int
    ) -> list[FusedResult]:
        """仅有向量结果时的转换"""
        return [
            FusedResult(
                doc_id=result.doc_id,
                rrf_score=1.0 / (self.k + rank + 1),
                bm25_score=None,
                vector_score=result.score,
                content=result.content,
                metadata=result.metadata,
            )
            for rank, result in enumerate(vector_results[:top_k])
        ]
