"""记忆库定期整合管理器。

把零散的低价值记忆聚合、整理、总结为更精炼的记忆，从源头控制记忆库规模。
聚合粒度（同会话 / 语义聚类）、旧记忆处理（归档 / 删除）、触发方式（每日 / 反思）
均由 memory_consolidation 配置控制。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger


class MemoryConsolidationManager:
    """按配置定期整合记忆库。"""

    # reflection 触发模式下的默认最小运行间隔（小时）
    DEFAULT_MIN_RUN_INTERVAL_HOURS = 6.0
    # 单轮候选查询的默认上限：没有 LIMIT 时 fetchall() 会把整张表读进内存
    DEFAULT_MAX_CANDIDATES = 500
    # 单组记忆条数默认上限：语义聚类的并查集传递闭包会把弱相关记忆串成巨大组，
    # 超限组必须先切分再送进 LLM，否则一次请求会塞入巨量文本
    DEFAULT_MAX_GROUP_SIZE = 10

    def __init__(self, memory_engine, memory_processor, config_manager):
        self.memory_engine = memory_engine
        self.memory_processor = memory_processor
        self.config_manager = config_manager
        # reflection 触发模式下的最小运行间隔（秒），避免每条消息都触发
        self._last_run_at = 0.0
        self._min_run_interval = self._resolve_min_run_interval()

    @property
    def config(self) -> dict[str, Any]:
        return self.config_manager.get_section("memory_consolidation")

    def _resolve_min_run_interval(self, cfg: dict[str, Any] | None = None) -> float:
        """读取 reflection 冷却间隔（秒）；配置缺失或非法时回退默认 6 小时。"""
        default = self.DEFAULT_MIN_RUN_INTERVAL_HOURS * 3600.0
        try:
            section = self.config if cfg is None else cfg
            hours = float(
                section.get(
                    "min_run_interval_hours", self.DEFAULT_MIN_RUN_INTERVAL_HOURS
                )
            )
        except (TypeError, ValueError, AttributeError, KeyError) as e:
            logger.debug(
                f"[记忆整合] min_run_interval_hours 读取失败，回退默认值: {e}",
                exc_info=True,
            )
            return default
        # 显式配 0 表示关闭冷却（每次 reflection 都跑），负数按 0 处理
        return max(hours, 0.0) * 3600.0

    def _resolve_max_candidates(self, cfg: dict[str, Any]) -> int:
        """读取单轮候选条数上限；配置缺失或非法时回退默认 500。"""
        try:
            value = int(cfg.get("max_candidates", self.DEFAULT_MAX_CANDIDATES))
        except (TypeError, ValueError, AttributeError, KeyError) as e:
            logger.debug(
                f"[记忆整合] max_candidates 读取失败，回退默认值: {e}", exc_info=True
            )
            return self.DEFAULT_MAX_CANDIDATES
        return value if value > 0 else self.DEFAULT_MAX_CANDIDATES

    def _resolve_max_group_size(self, cfg: dict[str, Any]) -> int:
        """读取单组条数上限；配置缺失或非法时回退默认 10。

        下限取 2：只有 1 条记忆的组做「合并」没有意义。
        """
        try:
            value = int(cfg.get("max_group_size", self.DEFAULT_MAX_GROUP_SIZE))
        except (TypeError, ValueError, AttributeError, KeyError) as e:
            logger.debug(
                f"[记忆整合] max_group_size 读取失败，回退默认值: {e}", exc_info=True
            )
            return self.DEFAULT_MAX_GROUP_SIZE
        return max(value, 2)

    async def maybe_run(self, trigger: str) -> dict[str, Any]:
        """按触发方式执行整合（不匹配或未启用时跳过）。

        Args:
            trigger: "daily" 或 "reflection"。
        """
        cfg = self.config
        if not cfg.get("enabled", False) or cfg.get("trigger", "daily") != trigger:
            return {"skipped": True}
        return await self.run_consolidation(force=(trigger == "daily"))

    async def run_consolidation(self, force: bool = False) -> dict[str, Any]:
        """执行一轮记忆整合。返回统计信息。

        Args:
            force: 是否忽略最小运行间隔（每日定时触发时传入 True）。
        """
        cfg = self.config
        if not cfg.get("enabled", False):
            return {"skipped": True}
        if not self.memory_engine or not self.memory_processor:
            logger.warning("[记忆整合] 组件未就绪，跳过")
            return {"skipped": True, "reason": "components not ready"}

        # 每轮按当前配置重算冷却间隔，WebUI 改配置后无需重启即时生效
        self._min_run_interval = self._resolve_min_run_interval(cfg)
        now = time.time()
        if not force and now - self._last_run_at < self._min_run_interval:
            return {"skipped": True, "reason": "cooldown"}
        self._last_run_at = now

        try:
            candidates = await self._query_candidates(cfg)
            if not candidates:
                return {"candidates": 0, "groups": 0, "merged": 0}

            groups = await self._build_groups(candidates, cfg)
            if not groups:
                return {"candidates": len(candidates), "groups": 0, "merged": 0}

            max_groups = int(cfg.get("max_groups_per_run", 5))
            keep_original = cfg.get("keep_original", "archive")
            stats = {
                "candidates": len(candidates),
                "groups": 0,
                "merged": 0,
                "archived": 0,
                "deleted": 0,
                "failed": 0,
            }
            for group in groups[:max_groups]:
                try:
                    result = await self._consolidate_group(group, cfg)
                    stats["groups"] += 1
                    stats["merged"] += result["merged"]
                    if keep_original == "archive":
                        stats["archived"] += result["removed"]
                    else:
                        stats["deleted"] += result["removed"]
                except Exception as e:
                    stats["failed"] += 1
                    logger.error(f"[记忆整合] 整合组失败: {e}", exc_info=True)

            logger.info(f"[记忆整合] 完成: {stats}")
            return stats
        except Exception as e:
            logger.error(f"[记忆整合] 运行失败: {e}", exc_info=True)
            return {"error": str(e)}

    async def _query_candidates(self, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """查询符合整合条件的候选记忆（低重要度 + 足够旧 + 活跃状态）。"""
        db = getattr(self.memory_engine, "db_connection", None)
        if db is None:
            return []

        max_importance = float(cfg.get("max_importance", 0.5))
        cutoff = time.time() - int(cfg.get("min_age_days", 7)) * 86400.0
        max_candidates = self._resolve_max_candidates(cfg)

        # LIMIT 防止 fetchall() 把全表读进内存；ORDER BY 保证 LIMIT 结果确定，
        # 并优先取最适合整合的候选（重要度最低、最旧），末尾按 id 兜底去随机性。
        cursor = await db.execute(
            """
            SELECT id, text, metadata
            FROM documents
            WHERE COALESCE(json_extract(metadata, '$.status'), 'active') = 'active'
              AND CAST(COALESCE(json_extract(metadata, '$.importance'), '0.5') AS REAL) < ?
              AND CAST(COALESCE(json_extract(metadata, '$.create_time'), '0') AS REAL) < ?
            ORDER BY
              CAST(COALESCE(json_extract(metadata, '$.importance'), '0.5') AS REAL) ASC,
              CAST(COALESCE(json_extract(metadata, '$.create_time'), '0') AS REAL) ASC,
              id ASC
            LIMIT ?
            """,
            (max_importance, cutoff, max_candidates),
        )
        rows = await cursor.fetchall()

        safe_json_dict = self.memory_engine._safe_json_dict
        candidates: list[dict[str, Any]] = []
        for row in rows:
            candidates.append(
                {
                    "id": int(row["id"]),
                    "content": row["text"],
                    "metadata": safe_json_dict(row["metadata"]),
                }
            )
        return candidates

    async def _build_groups(
        self, candidates: list[dict[str, Any]], cfg: dict[str, Any]
    ) -> list[list[dict[str, Any]]]:
        granularity = cfg.get("granularity", "session")
        if granularity == "semantic":
            groups = await self._group_semantic(candidates, cfg)
        else:
            groups = self._group_by_session(candidates)

        min_per = int(cfg.get("min_memories_per_group", 3))
        groups = [g for g in groups if len(g) >= min_per]
        # 先按 min_per 过滤、再切分超限组：切分出来的子组不再做二次过滤，
        # 否则会把已经通过门槛的候选凭空丢掉。
        groups = self._split_oversized_groups(groups, cfg)
        groups.sort(key=len, reverse=True)
        return groups

    def _split_oversized_groups(
        self, groups: list[list[dict[str, Any]]], cfg: dict[str, Any]
    ) -> list[list[dict[str, Any]]]:
        """把超过 max_group_size 的组均分成多个子组，不丢弃任何候选。

        `_group_semantic` 用并查集求连通分量，相似阈值只有 0.7，传递闭包
        （A~B、B~C 就把 A 和 C 也拉进同一组）会让弱相关记忆雪球成一个巨大组。
        这种组直接进 `merge_memories` 会把巨量文本塞进单次 LLM 请求，
        因此这里按上限切分止损；候选一条都不丢，只是分几轮合并。
        """
        max_size = self._resolve_max_group_size(cfg)
        result: list[list[dict[str, Any]]] = []
        for group in groups:
            total = len(group)
            if total <= max_size:
                result.append(group)
                continue
            # 均分成 ceil(total / max_size) 份，避免切出「几个满组 + 1 条零头」
            chunks = -(-total // max_size)
            base, extra = divmod(total, chunks)
            start = 0
            for index in range(chunks):
                size = base + (1 if index < extra else 0)
                result.append(group[start : start + size])
                start += size
            logger.debug(
                f"[记忆整合] 组过大（{total} 条 > 上限 {max_size}），"
                f"已均分为 {chunks} 个子组"
            )
        return result

    def _group_by_session(
        self, candidates: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for mem in candidates:
            session_id = mem["metadata"].get("session_id")
            if not session_id:
                continue
            grouped.setdefault(str(session_id), []).append(mem)
        return list(grouped.values())

    async def _group_semantic(
        self, candidates: list[dict[str, Any]], cfg: dict[str, Any]
    ) -> list[list[dict[str, Any]]]:
        """跨会话语义聚类：复用索引内向量批量查找相似对，再做连通分量合并。

        可扩展到上万条候选：不逐条调用 Embedding API，而是批量 reconstruct + 批量
        Faiss 搜索，内存峰值由 vector_retriever 的分块控制。
        """
        threshold = float(cfg.get("semantic_similarity_threshold", 0.7))
        candidate_ids = [mem["id"] for mem in candidates]

        pairs: list[tuple[int, int, float]] = []
        try:
            vector_retriever = getattr(self.memory_engine, "vector_retriever", None)
            if vector_retriever is None:
                return self._group_by_session(candidates)
            pairs = await vector_retriever.find_similar_pairs(
                candidate_ids, threshold
            )
        except Exception as e:
            logger.warning(f"[记忆整合] 语义聚类失败，回退到同会话聚合: {e}")
            return self._group_by_session(candidates)

        if not pairs:
            return []

        parent: dict[int, int] = {mem["id"]: mem["id"] for mem in candidates}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for a, b, _sim in pairs:
            union(a, b)

        groups: dict[int, list[dict[str, Any]]] = {}
        for mem in candidates:
            root = find(mem["id"])
            groups.setdefault(root, []).append(mem)
        return list(groups.values())

    async def _consolidate_group(
        self, group: list[dict[str, Any]], cfg: dict[str, Any]
    ) -> dict[str, Any]:
        merged = await self.memory_processor.merge_memories(group)

        summary = merged["summary"]
        key_facts = merged["key_facts"]
        rich_content = summary
        if key_facts:
            rich_content = f"{summary} | {'；'.join(key_facts)}"

        # merge_memories 可能因输入字符预算跳过部分记忆，这些记忆并没有被合并进
        # 新摘要，绝不能跟着一起归档/删除，否则信息真的丢了。旧版返回值没有
        # skipped_ids 键时退化为「全部参与」，保持向后兼容。
        skipped_ids = set(merged.get("skipped_ids") or [])
        kept = [mem for mem in group if mem["id"] not in skipped_ids] or list(group)
        if skipped_ids:
            logger.warning(
                f"[记忆整合] 本组有 {len(skipped_ids)} 条记忆因输入预算未参与合并，"
                f"保持原状不做归档/删除: {sorted(skipped_ids, key=str)}"
            )

        session_id = None
        persona_id = None
        if cfg.get("granularity", "session") == "session":
            session_id = kept[0]["metadata"].get("session_id")
            persona_id = kept[0]["metadata"].get("persona_id")

        old_ids = [mem["id"] for mem in kept]
        metadata = {
            "topics": merged["topics"],
            "key_facts": key_facts,
            "persona_summary": summary,
            "canonical_summary": rich_content,
            "summary_schema_version": "v2",
            "consolidated_from": old_ids,
            "consolidated_at": time.time(),
        }

        new_id = await self.memory_engine.add_memory(
            content=rich_content,
            session_id=session_id,
            persona_id=persona_id,
            importance=merged["importance"],
            metadata=metadata,
        )

        if cfg.get("keep_original", "archive") == "archive":
            removed = await self.memory_engine.archive_memories(old_ids)
        else:
            removed = await self.memory_engine.batch_delete_memories(old_ids)

        logger.info(
            f"[记忆整合] 整合 {len(old_ids)} 条记忆 -> 新记忆 {new_id}，"
            f"{cfg.get('keep_original', 'archive')} {removed} 条旧记忆"
        )
        return {"new_id": new_id, "merged": len(old_ids), "removed": removed}


__all__ = ["MemoryConsolidationManager"]
