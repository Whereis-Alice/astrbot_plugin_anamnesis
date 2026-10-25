"""
召回测试处理模块
"""

import asyncio
import math
import time
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from quart import request

from ..retrieval.temporal_query import resolve_context_timezone
from ..retrieval.temporal_retrieval import retrieval_time_bounds, search_with_time

if TYPE_CHECKING:
    from .utils import PageApiUtils


class RecallHandler:
    """召回测试处理器"""

    def __init__(self, utils: "PageApiUtils"):
        """
        初始化召回测试处理器

        Args:
            utils: PageApiUtils 工具实例
        """
        self.utils = utils

    async def test_recall(
        self, memory_engine, config_manager=None, context=None
    ) -> dict[str, Any]:
        """
        测试记忆召回功能

        Payload:
            - query: 查询文本（必需）
            - k: 返回结果数量（默认5，最大50）
            - session_id: 会话ID过滤（可选）

        Returns:
            包含召回结果和性能指标的字典
        """
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return self.utils.error("请求参数必须为 JSON 对象")
        query_text = payload.get("query", "")
        if not isinstance(query_text, str) or len(query_text) > 2000:
            return self.utils.error("查询必须为不超过 2000 字的文本")
        query_text = query_text.strip()
        if (
            not query_text
            and payload.get("start_time") in (None, "")
            and payload.get("end_time") in (None, "")
        ):
            return self.utils.error("查询内容不能为空")

        k = payload.get("k", 5)
        if isinstance(k, bool) or not isinstance(k, int):
            return self.utils.error("k 必须是整数")
        k = min(50, max(1, k))

        if any(
            value is not None and not isinstance(value, str)
            for value in (payload.get("session_id"), payload.get("persona_id"))
        ):
            return self.utils.error("作用域和人格必须为文本或空")
        session_id = self.utils.optional_text(payload.get("session_id"))
        persona_id = self.utils.optional_text(payload.get("persona_id"))
        if config_manager is None:
            config_manager = getattr(
                getattr(memory_engine, "memory_administration", None), "config", None
            )
            if not hasattr(config_manager, "filtering_settings"):
                config_manager = None

        try:
            started = time.perf_counter()
            raw_timeout = (
                config_manager.get("recall_engine.search_timeout_seconds", 5)
                if config_manager is not None
                else 5
            )
            try:
                timeout = float(raw_timeout)
                if isinstance(raw_timeout, bool) or not math.isfinite(timeout):
                    timeout = 5.0
            except (TypeError, ValueError, OverflowError):
                timeout = 5.0
            results, retrieval = await search_with_time(
                memory_engine,
                query=query_text,
                k=k,
                session_id=session_id,
                persona_id=persona_id,
                persona_filtered=persona_id is not None,
                config_manager=config_manager,
                mode=payload.get("mode", "auto"),
                start_time=payload.get("start_time"),
                end_time=payload.get("end_time"),
                timezone_name=resolve_context_timezone(
                    context, config_manager, session_id
                ),
                timeout=timeout if timeout > 0 else None,
            )
            elapsed_time = (time.perf_counter() - started) * 1000
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return self.utils.error(
                "检索超过配置的总超时预算，请缩小范围或调整检索超时"
            )
        except (ValueError, TypeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:  # noqa: BLE001 -- report adapter failures at the authenticated API boundary
            logger.error(f"[PageAPI] 召回测试失败: {exc}", exc_info=True)
            return self.utils.error(str(exc))

        formatted_results = []
        for result in results:
            raw_breakdown = getattr(result, "score_breakdown", None)
            raw_breakdown = raw_breakdown if isinstance(raw_breakdown, dict) else {}
            score_breakdown = {
                key: round(float(value), 6)
                for key, value in raw_breakdown.items()
                if isinstance(value, (int, float)) and math.isfinite(value)
            }
            for key in ("graph_evidence_reason", "graph_evidence_basis"):
                if isinstance(raw_breakdown.get(key), str):
                    score_breakdown[key] = raw_breakdown[key][:100]
            tokens = raw_breakdown.get("graph_evidence_matched_tokens")
            if isinstance(tokens, list):
                score_breakdown["graph_evidence_matched_tokens"] = [
                    value[:100] for value in tokens[:12] if isinstance(value, str)
                ]
            result_metadata = self.utils.normalize_metadata(
                getattr(result, "metadata", None)
            )
            metadata = {
                "session_id": result_metadata.get("session_id"),
                "persona_id": result_metadata.get("persona_id"),
                "importance": result_metadata.get("importance", 0.5),
                "memory_type": result_metadata.get("memory_type", "GENERAL"),
                "status": result_metadata.get("status", "active"),
                "create_time": result_metadata.get("create_time"),
            }
            if retrieval.get("time_range"):
                start, end, basis = retrieval_time_bounds(result_metadata)
                metadata.update(time_start=start, time_end=end, time_basis=basis)
            metadata.update(score_breakdown)
            formatted_results.append(
                {
                    "memory_id": result.doc_id,
                    "content": result.content,
                    "similarity_score": round(float(result.final_score), 4),
                    "score_percentage": round(float(result.final_score) * 100, 2),
                    "metadata": metadata,
                    "score_breakdown": score_breakdown,
                }
            )

        return self.utils.ok(
            {
                "results": formatted_results,
                "total": len(formatted_results),
                "query": query_text,
                "k": k,
                "session_id_filter": session_id,
                "persona_id_filter": persona_id,
                "retrieval": retrieval,
                "elapsed_time_ms": round(elapsed_time, 2),
            }
        )
