"""记忆库定期整合管理器。

把零散的低价值记忆聚合、整理、总结为更精炼的记忆，从源头控制记忆库规模。
聚合粒度（同会话 / 语义聚类）、旧记忆处理（归档 / 删除）、触发方式（每日 / 反思）
均由 memory_consolidation 配置控制。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext
from copy import deepcopy
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
        self._run_lock = asyncio.Lock()

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
        if self._run_lock.locked():
            return {"skipped": True, "reason": "in_progress"}
        async with self._run_lock:
            return await self._run_consolidation_once(force)

    async def _run_consolidation_once(self, force: bool) -> dict[str, Any]:
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
            semantic_groups = await self._group_semantic(candidates, cfg)
            groups = [
                scope_group
                for semantic_group in semantic_groups
                for scope_group in self._group_by_session(semantic_group)
            ]
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
        grouped: dict[tuple[str, Any, str], list[dict[str, Any]]] = {}
        for mem in candidates:
            session_id = mem["metadata"].get("session_id")
            if not session_id:
                continue
            key = (
                str(session_id),
                mem["metadata"].get("persona_id"),
                self._source_session(mem["metadata"]),
            )
            grouped.setdefault(key, []).append(mem)
        return list(grouped.values())

    @staticmethod
    def _source_session(metadata: dict[str, Any]) -> str:
        window = metadata.get("source_window")
        window_session = window.get("session_id") if isinstance(window, dict) else None
        return str(
            metadata.get("source_session_id")
            or window_session
            or metadata.get("session_id")
            or ""
        )

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
            pairs = await vector_retriever.find_similar_pairs(candidate_ids, threshold)
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
        scope_groups = self._group_by_session(group)
        if len(scope_groups) != 1 or len(scope_groups[0]) != len(group):
            raise RuntimeError("整合组必须包含同一会话、同一来源会话、同一人格的记忆")

        # LLM 等待期间不持有变更锁；前后核验同一快照，避免清理被改写/迁移的来源。
        async with self._mutation_guard():
            snapshots = await self._capture_sources(group)
        merged = await self.memory_processor.merge_memories(group)

        summary = merged["summary"]
        if not isinstance(summary, str) or not summary.strip():
            raise RuntimeError("合并结果缺少有效 summary")
        summary = summary.strip()
        key_facts = merged["key_facts"]
        rich_content = summary
        if key_facts:
            rich_content = f"{summary} | {'；'.join(key_facts)}"

        # merge_memories 可能因输入字符预算跳过部分记忆，这些记忆并没有被合并进
        # 新摘要，绝不能跟着一起归档/删除，否则信息真的丢了。旧版返回值没有
        # skipped_ids 键时退化为「全部参与」，保持向后兼容。
        skipped_ids = set(merged.get("skipped_ids") or [])
        truncated_ids = set(merged.get("truncated_ids") or [])
        kept = [mem for mem in group if mem["id"] not in skipped_ids | truncated_ids]
        if not kept:
            raise RuntimeError("本组没有实际参与合并且完整参与的记忆，取消整合")
        if skipped_ids:
            logger.warning(
                f"[记忆整合] 本组有 {len(skipped_ids)} 条记忆因输入预算未参与合并，"
                f"保持原状不做归档/删除: {sorted(skipped_ids, key=str)}"
            )
        if truncated_ids:
            logger.warning(
                f"[记忆整合] 输入被截断的来源保持活跃: {sorted(truncated_ids, key=str)}"
            )

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
        source_messages = self._combine_sources(kept, snapshots, metadata)

        # 最终核验、新增和来源清理作为一个变更单元完成。调用方取消时等待该单元
        # 收尾再传播取消，避免 add 已提交但尚未返回 ID 时留下无法补偿的结果。
        write_task = asyncio.create_task(
            self._finalize_group(
                group=group,
                snapshots=snapshots,
                old_ids=old_ids,
                cfg=cfg,
                write_kwargs={
                    "content": rich_content,
                    "session_id": session_id,
                    "persona_id": persona_id,
                    "importance": merged["importance"],
                    "metadata": metadata,
                    "source_messages": source_messages or None,
                },
            )
        )
        try:
            return await asyncio.shield(write_task)
        except asyncio.CancelledError:
            while not write_task.done():
                try:
                    await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not write_task.cancelled():
                try:
                    write_task.result()
                except Exception:
                    logger.warning("[记忆整合] 取消时最终写入失败", exc_info=True)
            raise

    def _mutation_guard(self):
        factory = getattr(self.memory_engine, "memory_mutation_guard", None)
        return factory() if callable(factory) else nullcontext()

    def _document_snapshot(self, memory: dict[str, Any]) -> dict[str, Any]:
        metadata = self.memory_engine._safe_json_dict(memory.get("metadata"))
        # 正常召回会更新这两个统计字段，不改变参与整合的事实和归属。
        metadata = {
            key: value
            for key, value in metadata.items()
            if key not in {"last_access_time", "access_count"}
        }
        return deepcopy(
            {
                "content": memory.get("text", memory.get("content", "")),
                "metadata": metadata,
            }
        )

    async def _capture_sources(
        self,
        group: list[dict[str, Any]],
    ) -> dict[int, dict[str, Any]]:
        await self._assert_writes_finished([memory["id"] for memory in group])
        snapshots = {}
        for memory in group:
            memory_id = memory["id"]
            current = await self.memory_engine.get_memory(memory_id)
            if not isinstance(current, dict) or not current:
                raise RuntimeError(f"整合来源 {memory_id} 不存在或读取失败")
            snapshot = self._document_snapshot(current)
            if (
                snapshot != self._document_snapshot(memory)
                or str(snapshot["metadata"].get("status") or "active") != "active"
            ):
                raise RuntimeError(f"整合来源 {memory_id} 已发生变化，取消整合")
            source = await self.memory_engine.get_memory_source(memory_id)
            if not isinstance(source, list) or any(
                not isinstance(row, dict) for row in source
            ):
                raise RuntimeError(f"整合来源 {memory_id} 原文读取失败")
            if snapshot["metadata"].get("has_source") and not source:
                raise RuntimeError(f"整合来源 {memory_id} 原文缺失，取消整合")
            snapshots[memory_id] = {**snapshot, "source_messages": deepcopy(source)}
        return snapshots

    async def _assert_writes_finished(self, memory_ids: list[int]) -> None:
        db = getattr(self.memory_engine, "db_connection", None)
        if db is None or not memory_ids:
            return
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_write_ops'"
        )
        try:
            exists = await cursor.fetchone()
        finally:
            await cursor.close()
        if exists is None:
            return
        for start in range(0, len(memory_ids), 500):
            batch = memory_ids[start : start + 500]
            cursor = await db.execute(
                "SELECT 1 FROM memory_write_ops WHERE status IN ('pending','needs_repair') "
                f"AND memory_id IN ({','.join('?' for _ in batch)}) LIMIT 1",
                batch,
            )
            try:
                pending = await cursor.fetchone()
            finally:
                await cursor.close()
            if pending is not None:
                raise RuntimeError("整合来源或新记忆的写入/修复未完成，稍后重试")

    async def _validate_sources(
        self,
        group: list[dict[str, Any]],
        snapshots: dict[int, dict[str, Any]],
    ) -> None:
        if await self._capture_sources(group) != snapshots:
            raise RuntimeError("整合来源原文已发生变化，取消整合")

    def _combine_sources(
        self,
        kept: list[dict[str, Any]],
        snapshots: dict[int, dict[str, Any]],
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        combined = []
        seen = set()
        sessions = set()
        message_ids = []
        for memory in kept:
            snapshot = snapshots[memory["id"]]
            source_metadata = snapshot["metadata"]
            source_session = self._source_session(source_metadata)
            if source_session:
                sessions.add(str(source_session))
            source_ids = source_metadata.get("source_message_ids")
            if isinstance(source_ids, list):
                message_ids.extend(source_ids)
            for row in snapshot["source_messages"]:
                row = deepcopy(row)
                if not row.get("session_id") and source_session:
                    row["session_id"] = str(source_session)
                if row.get("session_id"):
                    sessions.add(str(row["session_id"]))
                # 完全相同的消息去重，同 ID 的不同原文版本仍保留。
                key = json.dumps(row, sort_keys=True, ensure_ascii=False)
                if key not in seen:
                    seen.add(key)
                    combined.append(row)
                    message_ids.append(row.get("id"))
        if len(sessions) > 1:
            raise RuntimeError("整合来源原文跨越多个会话，请分别整合")
        if len(sessions) == 1:
            metadata["source_session_id"] = next(iter(sessions))
            if message_ids and all(
                isinstance(value, int) and not isinstance(value, bool) and value > 0
                for value in message_ids
            ):
                metadata["source_message_ids"] = list(dict.fromkeys(message_ids))
        return combined

    async def _finalize_group(
        self,
        *,
        group: list[dict[str, Any]],
        snapshots: dict[int, dict[str, Any]],
        old_ids: list[int],
        cfg: dict[str, Any],
        write_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._mutation_guard():
            await self._validate_sources(group, snapshots)
            new_id = await self.memory_engine.add_memory(**write_kwargs)
            if not isinstance(new_id, int) or isinstance(new_id, bool) or new_id <= 0:
                raise RuntimeError("整合新记忆写入未返回有效 ID，保留来源")
            try:
                await self._assert_writes_finished([new_id])
                if cfg.get("keep_original", "archive") == "archive":
                    removed = await self.memory_engine.archive_memories(old_ids)
                else:
                    removed = await self.memory_engine.batch_delete_memories(old_ids)
                if removed != len(old_ids):
                    raise RuntimeError(
                        f"整合来源清理不完整: expected={len(old_ids)}, removed={removed}"
                    )
            except (Exception, asyncio.CancelledError):
                await self._rollback_if_sources_intact(new_id, old_ids)
                raise

        logger.info(
            f"[记忆整合] 整合 {len(old_ids)} 条记忆 -> 新记忆 {new_id}，"
            f"{cfg.get('keep_original', 'archive')} {removed} 条旧记忆"
        )
        return {"new_id": new_id, "merged": len(old_ids), "removed": removed}

    async def _rollback_if_sources_intact(
        self, new_id: int, source_ids: list[int]
    ) -> None:
        """仅在所有来源仍可召回时撤销新结果，避免部分清理后丢失唯一副本。"""
        try:
            for source_id in source_ids:
                source = await self.memory_engine.get_memory(source_id)
                if not source:
                    logger.warning(
                        f"[记忆整合] 来源 {source_id} 已移除，保留合并记忆 {new_id}"
                    )
                    return
                metadata = self.memory_engine._safe_json_dict(source.get("metadata"))
                if str(metadata.get("status") or "active") != "active":
                    logger.warning(
                        f"[记忆整合] 来源 {source_id} 已归档，保留合并记忆 {new_id}"
                    )
                    return
            if not await self.memory_engine.delete_memory(new_id):
                logger.error(f"[记忆整合] 撤销合并记忆 {new_id} 失败")
        except Exception:
            logger.error(
                f"[记忆整合] 无法确认来源完整性，保留合并记忆 {new_id}", exc_info=True
            )


__all__ = ["MemoryConsolidationManager"]
