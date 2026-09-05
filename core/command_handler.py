"""
命令处理器
负责处理插件命令
"""

import os
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult

from .base.config_manager import ConfigManager
from .i18n_backend import t, t_list
from .identity_repair import normalise_platform
from .managers.conversation_manager import ConversationManager
from .managers.memory_engine import MemoryEngine
from .memory_scope import is_event_memory_allowed, resolve_memory_scope
from .memory_source import serialize_source_messages
from .validators.index_validator import IndexValidator

# /anam migrate 的模式别名 → 规范化动作
_MIGRATE_MODES: dict[str, str] = {
    "": "preview",
    "preview": "preview",
    "dry": "preview",
    "dry-run": "preview",
    "dryrun": "preview",
    "check": "preview",
    "plan": "preview",
    "exec": "exec",
    "run": "exec",
    "apply": "exec",
    "yes": "exec",
    "confirm": "exec",
    "force": "force",
    "overwrite": "force",
}

# /anam fix-identity 的模式别名 → 规范化动作
_IDENTITY_FIX_MODES: dict[str, str] = {
    "": "preview",
    "preview": "preview",
    "dry": "preview",
    "dry-run": "preview",
    "dryrun": "preview",
    "check": "preview",
    "plan": "preview",
    "exec": "exec",
    "run": "exec",
    "apply": "exec",
    "yes": "exec",
    "confirm": "exec",
    "fix": "exec",
    "rollback": "rollback",
    "undo": "rollback",
    "revert": "rollback",
    "restore": "rollback",
}


def _human_bytes(num_bytes: int | float | None) -> str:
    """把字节数格式化成人类可读文本。"""
    if not num_bytes:
        return "0 B"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


class CommandHandler:
    """命令处理器"""

    def __init__(
        self,
        context,
        config_manager: ConfigManager,
        memory_engine: MemoryEngine | None,
        conversation_manager: ConversationManager | None,
        index_validator: IndexValidator | None,
        memory_processor=None,
        initialization_status_callback=None,
        data_dir: str | None = None,
    ):
        """
        初始化命令处理器

        Args:
            context: AstrBot Context
            config_manager: 配置管理器
            memory_engine: 记忆引擎
            conversation_manager: 会话管理器
            index_validator: 索引验证器
            memory_processor: 记忆处理器（用于手动总结）
            initialization_status_callback: 初始化状态回调函数
            data_dir: 插件数据目录（迁移与存储维护命令使用）
        """
        self.context = context
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.conversation_manager = conversation_manager
        self.index_validator = index_validator
        self._memory_processor = memory_processor
        self.get_initialization_status = initialization_status_callback
        self.data_dir = data_dir

    @staticmethod
    def _format_error_message(
        action: str, error: Exception, suggestions: list[str] | None = None
    ) -> str:
        """Format user-facing error message with actionable hints."""
        message = [
            t("error.format.action_failed", action=action),
            t("error.format.details", error=error),
        ]
        if suggestions:
            message.append("")
            message.append(t("error.format.suggestions"))
            for index, suggestion in enumerate(suggestions, start=1):
                message.append(
                    t(
                        "error.format.suggestion_item",
                        index=index,
                        suggestion=suggestion,
                    )
                )
        return "\n".join(message)

    @staticmethod
    def _component_not_ready_message(component: str, command: str) -> str:
        """Build a consistent component-not-ready response."""
        return t("error.component_not_ready", component=component, command=command)

    async def handle_status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam status 命令"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("记忆引擎", "/anam status")
            )
            return

        try:
            stats = await self.memory_engine.get_statistics()

            # 格式化时间
            last_update = t("common.never")
            if stats.get("newest_memory"):
                last_update = datetime.fromtimestamp(stats["newest_memory"]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

            # 计算数据库大小
            db_size = 0.0
            if os.path.exists(self.memory_engine.db_path):
                db_size = os.path.getsize(self.memory_engine.db_path) / (1024 * 1024)

            session_count = len(stats.get("sessions", {}))

            message = t(
                "status.report",
                total=stats["total_memories"],
                session_count=session_count,
                last_update=last_update,
                db_size=db_size,
            )

            maintenance = stats.get("index_maintenance") or {}
            maintenance_state = str(maintenance.get("state") or "idle")
            if maintenance_state not in {"idle", "ready"}:
                message += t(
                    "status.index_maintenance",
                    state=maintenance_state,
                    current=int(maintenance.get("current", 0) or 0),
                    total=int(maintenance.get("total", 0) or 0),
                    message=str(maintenance.get("message") or ""),
                )

            # 图记忆是主库体积的主要来源，把「平均每条记忆产生多少图条目」直接摊开，
            # 便于判断是否需要收紧 graph_memory.max_edge_entries_per_memory。
            graph_entries = int(stats.get("graph_entries", 0) or 0)
            if graph_entries:
                total_memories = max(1, int(stats.get("total_memories", 0) or 0))
                message += t(
                    "status.graph_report",
                    nodes=int(stats.get("graph_nodes", 0) or 0),
                    edges=int(stats.get("graph_edges", 0) or 0),
                    entries=graph_entries,
                    per_memory=graph_entries / total_memories,
                )

            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"获取状态失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("status.action_name"),
                    e,
                    t_list("error.suggestions.status"),
                )
            )

    async def handle_search(
        self, event: AstrMessageEvent, query: str, k: int = 5
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam search 命令"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("记忆引擎", "/anam search")
            )
            return

        # 输入验证
        if not query or not query.strip():
            yield event.plain_result(t("search.query_empty"))
            return

        # 限制k的范围为1-100
        k = max(1, min(k, 100))

        try:
            session_id = event.unified_msg_origin
            results = await self.memory_engine.search_memories(
                query=query.strip(), k=k, session_id=session_id
            )

            if not results:
                yield event.plain_result(t("search.no_results", query=query))
                return

            message = t("search.header", count=len(results))
            for i, result in enumerate(results, 1):
                score = result.final_score
                content = (
                    result.content[:100] + "..."
                    if len(result.content) > 100
                    else result.content
                )
                raw_breakdown = getattr(result, "score_breakdown", {})
                breakdown = raw_breakdown if isinstance(raw_breakdown, dict) else {}
                message += t(
                    "search.item.score",
                    index=i,
                    score=score,
                    content=content,
                )
                message += t("search.item.id", id=result.doc_id)
                message += t(
                    "search.item.breakdown",
                    doc_kw=breakdown.get("document_keyword_score", 0.0),
                    doc_vec=breakdown.get("document_vector_score", 0.0),
                    graph_kw=breakdown.get("graph_keyword_score", 0.0),
                    graph_vec=breakdown.get("graph_vector_score", 0.0),
                )

            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"搜索失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("search.action_name"),
                    e,
                    t_list("error.suggestions.search"),
                )
            )

    async def handle_forget(
        self, event: AstrMessageEvent, doc_id: int
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam forget 命令"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("记忆引擎", "/anam forget")
            )
            return

        # 输入验证
        if doc_id < 0:
            yield event.plain_result(t("forget.id_invalid"))
            return

        try:
            success = await self.memory_engine.delete_memory(doc_id)
            if success:
                yield event.plain_result(t("forget.success", id=doc_id))
            else:
                yield event.plain_result(t("forget.not_found", id=doc_id))
        except Exception as e:
            logger.error(f"删除失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("forget.action_name"),
                    e,
                    t_list("error.suggestions.forget"),
                )
            )

    async def handle_rebuild_index(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam rebuild-index 命令"""
        if not self.memory_engine or not self.index_validator:
            yield event.plain_result(
                self._component_not_ready_message(
                    "记忆引擎或索引验证器", "/anam rebuild-index"
                )
            )
            return

        try:
            yield event.plain_result(t("rebuild_index.checking"))

            # 检查索引一致性
            status = await self.index_validator.check_consistency()

            if status.is_consistent and not status.needs_rebuild:
                yield event.plain_result(t("rebuild_index.ok", reason=status.reason))
                return

            # 显示当前状态
            status_msg = t(
                "rebuild_index.status_template",
                doc_count=status.documents_count,
                bm25_count=status.bm25_count,
                vec_count=status.vector_count,
                reason=status.reason,
            )
            yield event.plain_result(status_msg)

            # 执行重建
            result = await self.index_validator.rebuild_indexes(self.memory_engine)

            if result["success"]:
                partial_notice = ""
                if result.get("partial"):
                    partial_notice = t(
                        "rebuild_index.partial_notice",
                        ratio=result.get("failure_ratio", 0),
                    )
                switched_str = (
                    t("common.yes") if result.get("switched") else t("common.no")
                )
                result_msg = t(
                    "rebuild_index.result_template",
                    success=result["processed"],
                    failed=result["errors"],
                    total=result["total"],
                    vector_mode=result.get("vector_mode", "unknown"),
                    switched=switched_str,
                    partial_notice=partial_notice,
                )
                yield event.plain_result(result_msg)
            else:
                yield event.plain_result(
                    t(
                        "rebuild_index.failed",
                        message=result.get("message", t("common.unknown_error")),
                    )
                )

        except Exception as e:
            logger.error(f"重建索引失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("rebuild_index.action_name"),
                    e,
                    t_list("error.suggestions.rebuild_index"),
                )
            )

    async def handle_rebuild_graph(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam rebuild-graph 命令"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("记忆引擎", "/anam rebuild-graph")
            )
            return

        try:
            yield event.plain_result(t("rebuild_graph.starting"))
            result = await self.memory_engine.rebuild_graph_index()
            yield event.plain_result(
                t(
                    "rebuild_graph.success",
                    rebuilt=result.get("rebuilt", 0),
                    skipped=result.get("skipped", 0),
                )
            )
        except Exception as e:
            logger.error(f"重建图记忆失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("rebuild_graph.action_name"),
                    e,
                    t_list("error.suggestions.rebuild_graph"),
                )
            )

    async def handle_webui(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam webui 命令"""
        yield event.plain_result(t("webui.guide"))

    async def handle_summarize(
        self, event: AstrMessageEvent, message_count: int | None = None
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam summarize 命令 - 立即触发记忆总结"""
        if not self.conversation_manager or not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message(
                    "会话管理器或记忆引擎", "/anam summarize"
                )
            )
            return

        session_id = event.unified_msg_origin
        try:
            if not is_event_memory_allowed(self.config_manager, event):
                logger.debug("当前事件不在记忆白名单中，跳过手动总结")
                yield event.plain_result(t("summarize.access_denied"))
                return

            # 获取当前消息数和总结进度
            actual_count = await self.conversation_manager.store.get_message_count(
                session_id
            )
            last_summarized_index = (
                await self.conversation_manager.get_session_metadata(
                    session_id, "last_summarized_index", 0
                )
            )
            try:
                last_summarized_index = int(last_summarized_index)
            except (TypeError, ValueError):
                last_summarized_index = 0

            if message_count is not None:
                try:
                    requested_count = int(message_count)
                except (TypeError, ValueError):
                    requested_count = 0
                if requested_count < 2:
                    yield event.plain_result(t("summarize.invalid_count"))
                    return
                last_summarized_index = max(0, actual_count - requested_count)

            unsummarized = actual_count - last_summarized_index

            if unsummarized < 2:
                yield event.plain_result(
                    t(
                        "summarize.no_new",
                        total=actual_count,
                        index=last_summarized_index,
                    )
                )
                return

            yield event.plain_result(
                t(
                    "summarize.starting",
                    start=last_summarized_index,
                    end=actual_count,
                    count=unsummarized,
                )
            )

            history_messages = await self.conversation_manager.get_messages_range(
                session_id=session_id,
                start_index=last_summarized_index,
                end_index=actual_count,
            )

            if not history_messages:
                yield event.plain_result(t("summarize.fetch_failed"))
                return

            # 获取 persona_id
            from .utils import get_persona_id

            persona_id = await get_persona_id(self.context, event)
            memory_scope = (
                resolve_memory_scope(self.config_manager, event) or session_id
            )

            # 判断是否群聊
            is_group_chat = bool(
                history_messages[0].group_id if history_messages else False
            )
            if not is_group_chat and "GroupMessage" in session_id:
                is_group_chat = True

            if not self._memory_processor:
                yield event.plain_result(
                    self._component_not_ready_message("记忆处理器", "/anam summarize")
                )
                return

            (
                content,
                metadata,
                importance,
            ) = await self._memory_processor.process_conversation(
                messages=history_messages,
                is_group_chat=is_group_chat,
                persona_id=persona_id,
            )

            atoms = self._memory_processor.classify_atoms_from_metadata(
                metadata=metadata,
                parent_importance=importance,
                session_id=memory_scope,
                persona_id=persona_id,
            )

            metadata["source_window"] = {
                "session_id": session_id,
                "start_index": last_summarized_index,
                "end_index": actual_count,
                "message_count": actual_count - last_summarized_index,
                "triggered_by": "manual",
            }
            metadata["source_session_id"] = session_id

            await self.memory_engine.add_memory(
                content=content,
                session_id=memory_scope,
                persona_id=persona_id,
                importance=importance,
                metadata=metadata,
                atoms=atoms,
                source_messages=(
                    serialize_source_messages(history_messages)
                    if importance
                    >= float(
                        self.config_manager.get(
                            "reflection_engine.source_retention_importance_threshold",
                            0.8,
                        )
                    )
                    else None
                ),
            )

            await self.conversation_manager.update_session_metadata(
                session_id, "last_summarized_index", actual_count
            )
            await self.conversation_manager.update_session_metadata(
                session_id, "pending_summary", None
            )

            topics = ", ".join(metadata.get("topics", [])) or t("common.none")
            yield event.plain_result(
                t(
                    "summarize.success",
                    importance=importance,
                    topics=topics,
                    count=actual_count,
                )
            )

        except Exception as e:
            logger.error(f"手动触发记忆总结失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("summarize.action_name"),
                    e,
                    t_list("error.suggestions.summarize"),
                )
            )

    async def handle_reset(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam reset 命令"""
        if not self.conversation_manager:
            yield event.plain_result(
                self._component_not_ready_message("会话管理器", "/anam reset")
            )
            return

        session_id = event.unified_msg_origin
        try:
            await self.conversation_manager.clear_session(session_id)
            message = t("reset.success")
            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"手动重置记忆上下文失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("reset.action_name"),
                    e,
                    t_list("error.suggestions.reset"),
                )
            )

    async def handle_cleanup(
        self, event: AstrMessageEvent, dry_run: bool = False
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam cleanup 命令 - 清理 AstrBot 历史消息中的记忆注入片段"""
        session_id = event.unified_msg_origin
        try:
            mode_text = t("cleanup.mode_preview") if dry_run else t("cleanup.mode_exec")
            yield event.plain_result(t("cleanup.starting", mode_text=mode_text))

            # 检查 context 是否可用
            if not self.context:
                yield event.plain_result(t("cleanup.context_unavailable"))
                return

            # 获取当前对话 ID
            cid = await self.context.conversation_manager.get_curr_conversation_id(
                session_id
            )
            if not cid:
                yield event.plain_result(t("cleanup.no_history"))
                return

            # 获取对话历史
            conversation = await self.context.conversation_manager.get_conversation(
                session_id, cid
            )
            if not conversation or not conversation.history:
                yield event.plain_result(t("cleanup.empty_history"))
                return

            # 清理历史消息中的记忆注入片段
            import json
            import re

            from .base.constants import MEMORY_INJECTION_FOOTER, MEMORY_INJECTION_HEADER

            # 解析 history（字符串格式）
            try:
                history = json.loads(conversation.history)
            except json.JSONDecodeError:
                yield event.plain_result(t("cleanup.parse_failed"))
                return

            # 统计信息
            stats = {
                "scanned": len(history),
                "matched": 0,
                "cleaned": 0,
                "deleted": 0,
            }

            # 编译清理正则
            pattern = re.compile(
                re.escape(MEMORY_INJECTION_HEADER)
                + r".*?"
                + re.escape(MEMORY_INJECTION_FOOTER),
                flags=re.DOTALL,
            )

            # 清理历史消息
            cleaned_history = []
            for msg in history:
                content = msg.get("content", "")
                if not isinstance(content, str):
                    cleaned_history.append(msg)
                    continue

                # 检查是否包含注入标记
                if (
                    MEMORY_INJECTION_HEADER in content
                    and MEMORY_INJECTION_FOOTER in content
                ):
                    stats["matched"] += 1

                    # 清理内容
                    cleaned_content = pattern.sub("", content)
                    cleaned_content = re.sub(r"\n{3,}", "\n\n", cleaned_content).strip()

                    # 如果清理后为空，跳过该消息
                    if not cleaned_content:
                        stats["deleted"] += 1
                        logger.debug(
                            f"[cleanup] 删除纯记忆注入消息: role={msg.get('role')}"
                        )
                        continue

                    # 如果清理后仍有内容，保留清理后的消息
                    if cleaned_content != content:
                        msg_copy = msg.copy()
                        msg_copy["content"] = cleaned_content
                        cleaned_history.append(msg_copy)
                        stats["cleaned"] += 1
                        logger.debug(
                            f"[cleanup] 清理消息内部记忆片段: "
                            f"原长度={len(content)}, 新长度={len(cleaned_content)}"
                        )
                        continue

                cleaned_history.append(msg)

            # 如果不是预演模式，更新数据库
            if not dry_run and (stats["cleaned"] > 0 or stats["deleted"] > 0):
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=session_id,
                    conversation_id=cid,
                    history=cleaned_history,
                )
                logger.info(
                    f"[{session_id}] cleanup 已更新 AstrBot 对话历史: "
                    f"清理={stats['cleaned']}, 删除={stats['deleted']}"
                )

            # 格式化结果
            notice = (
                t("cleanup.notice_preview") if dry_run else t("cleanup.notice_exec")
            )
            message = t(
                "cleanup.result_template",
                mode_text=mode_text,
                scanned=stats["scanned"],
                matched=stats["matched"],
                cleaned=stats["cleaned"],
                deleted=stats["deleted"],
                notice=notice,
            )

            yield event.plain_result(message)

        except Exception as e:
            logger.error(f"清理历史消息失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("cleanup.action_name"),
                    e,
                    t_list("error.suggestions.cleanup"),
                )
            )

    # ------------------------------------------------------------------
    # 数据迁移 / 存储回收
    # ------------------------------------------------------------------

    def _resolve_data_dir(self) -> str | None:
        """定位插件数据目录（迁移与存储维护命令依赖）。"""
        if self.data_dir:
            return str(self.data_dir)
        db_path = getattr(self.memory_engine, "db_path", None)
        if not db_path:
            return None
        try:
            return str(Path(db_path).parent)
        except (OSError, TypeError, ValueError):
            return None

    def _build_legacy_migrator(self):
        """构造 LegacyMigrator，返回 (migrator, error_message)。"""
        data_dir = self._resolve_data_dir()
        if not data_dir:
            return None, t("migrate.data_dir_unavailable")

        from .managers.legacy_migrator import LegacyMigrator

        legacy_name = ""
        if self.config_manager:
            legacy_name = self.config_manager.get(
                "migration_settings.legacy_plugin_name", ""
            )
        return LegacyMigrator(data_dir, legacy_name or None), None

    def _engine_holds_database(self) -> bool:
        """记忆引擎是否正持有主库连接（此时覆盖库文件会损坏数据）。"""
        if self.memory_engine is None:
            return False
        return getattr(self.memory_engine, "db_connection", None) is not None

    async def handle_migrate(
        self, event: AstrMessageEvent, mode: str = "preview"
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam migrate 命令 - 把旧版 LivingMemory 的数据导入本插件"""
        action = _MIGRATE_MODES.get((mode or "preview").strip().lower())
        if action is None:
            yield event.plain_result(t("migrate.invalid_mode", mode=mode))
            return

        migrator, error = self._build_legacy_migrator()
        if migrator is None:
            yield event.plain_result(error or t("migrate.data_dir_unavailable"))
            return

        try:
            if action == "preview":
                yield event.plain_result(t("migrate.preview_starting"))
                yield event.plain_result(await migrator.preview_async())
                yield event.plain_result(t("migrate.preview_hint"))
                return

            # 覆盖一个已被引擎打开的 SQLite 文件必然损坏数据，因此运行期直接拒绝 force
            if action == "force" and self._engine_holds_database():
                yield event.plain_result(t("migrate.engine_busy"))
                return

            yield event.plain_result(t("migrate.exec_starting"))
            result = await migrator.migrate_async(force=(action == "force"))

            lines: list[str] = [str(result.get("message") or t("common.unknown_error"))]
            if result.get("ok"):
                migrated = result.get("migrated") or []
                if migrated:
                    lines.append("")
                    lines.append(
                        t(
                            "migrate.migrated_items",
                            items=", ".join(str(item) for item in migrated),
                        )
                    )
                report_path = result.get("report_path")
                if report_path:
                    lines.append(t("migrate.report_path", path=report_path))
                lines.append("")
                lines.append(t("migrate.next_steps"))
            else:
                reason = str(result.get("reason") or "unknown")
                lines.append("")
                lines.append(t("migrate.failed_hint", reason=reason))
                if reason in ("target_occupied", "already_migrated"):
                    lines.append("")
                    lines.append(t("migrate.engine_busy"))

            yield event.plain_result("\n".join(lines))

        except Exception as e:
            logger.error(f"迁移旧插件数据失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("migrate.action_name"),
                    e,
                    t_list("error.suggestions.migrate"),
                )
            )

    async def handle_migrate_verify(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam migrate-verify 命令 - 逐表对账旧库与新库"""
        migrator, error = self._build_legacy_migrator()
        if migrator is None:
            yield event.plain_result(error or t("migrate.data_dir_unavailable"))
            return

        try:
            yield event.plain_result(t("migrate_verify.starting"))
            report = await migrator.verify_async()
            yield event.plain_result(
                report.get("message") or type(migrator).render_verify(report)
            )
        except Exception as e:
            logger.error(f"迁移对账失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("migrate_verify.action_name"),
                    e,
                    t_list("error.suggestions.migrate_verify"),
                )
            )

    async def handle_vacuum(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam vacuum 命令 - 清理写操作日志并回收数据库磁盘空间"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("记忆引擎", "/anam vacuum")
            )
            return

        try:
            yield event.plain_result(t("vacuum.starting"))
            result = await self.memory_engine.maintain_storage(
                vacuum=True,
                prune_write_ops=True,
                # 手动维护应当把该做的都做掉：图子系统的残留行是主库体积的主要
                # 来源，自动维护为了省开销默认关闭该步骤，手动执行时显式开启。
                prune_graph_orphans=True,
            )

            if not result.get("success"):
                yield event.plain_result(
                    t(
                        "vacuum.failed",
                        error=result.get("error") or t("common.unknown_error"),
                    )
                )
                return

            lines: list[str] = [t("vacuum.result_header")]
            lines.append(
                t(
                    "vacuum.size_line",
                    before=_human_bytes(result.get("db_size_before")),
                    after=_human_bytes(result.get("db_size_after")),
                    wal_before=_human_bytes(result.get("wal_size_before")),
                    wal_after=_human_bytes(result.get("wal_size_after")),
                )
            )
            lines.append(
                t(
                    "vacuum.reclaimed_line",
                    reclaimed=_human_bytes(result.get("bytes_reclaimed")),
                )
            )

            pruned = result.get("pruned") or {}
            write_ops = pruned.get("write_ops") or {}
            if write_ops.get("success"):
                lines.append(
                    t(
                        "vacuum.write_ops_line",
                        deleted=write_ops.get("deleted", 0),
                        remaining=write_ops.get("remaining", 0),
                        unfinished=write_ops.get("remaining_unfinished", 0),
                    )
                )
            graph_pruned = pruned.get("graph") or {}
            if graph_pruned.get("success"):
                lines.append(
                    t(
                        "vacuum.graph_line",
                        summary=graph_pruned.get("summary")
                        or str(graph_pruned.get("deleted", 0)),
                    )
                )

            yield event.plain_result("\n".join(lines))

        except Exception as e:
            logger.error(f"存储回收失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("vacuum.action_name"),
                    e,
                    t_list("error.suggestions.vacuum"),
                )
            )

    # ----------------------------------------------------- identity diagnostics

    @staticmethod
    def _live_bot_ids(event: AstrMessageEvent) -> dict[str, tuple[str, str]]:
        """Read the authoritative Bot identity from the live adapter.

        ``IdentityRepair`` takes this as plain data so it stays testable;
        digging it out of the event is an event-layer concern and lives here.
        """
        platform = ""
        try:
            if hasattr(event, "get_platform_name"):
                platform = str(event.get_platform_name() or "")
        except Exception as exc:
            logger.debug(f"读取平台名失败: {exc}")
        key = normalise_platform(platform)
        if not key:
            return {}
        try:
            bot_id, bot_name = ConversationManager._resolve_bot_identity(
                event, platform
            )
        except Exception as exc:
            logger.debug(f"读取 Bot 身份失败: {exc}")
            return {}
        if not bot_id:
            return {}
        return {key: (str(bot_id), str(bot_name or bot_id))}

    def _build_identity_repair(self, command: str):
        """构造 IdentityRepair，返回 (repair, error_message)。"""
        if not self.conversation_manager:
            return None, self._component_not_ready_message("会话管理器", command)

        from .identity_repair import IdentityRepair

        repair = IdentityRepair(
            self.conversation_manager,
            getattr(self.memory_engine, "alias_store", None),
        )
        if repair.connection is None:
            return None, t("identity.conversation_unavailable")
        return repair, None

    def _identity_anchor_lines(self) -> list[str]:
        anchor = getattr(self._memory_processor, "identity_anchor", None)
        if anchor is None:
            return [t("identity.anchor_unavailable")]
        return [
            t(
                "identity.anchor_line",
                state=t("identity.state_on")
                if getattr(anchor, "enabled", False)
                else t("identity.state_off"),
                format=getattr(anchor, "anchor_format", "") or "-",
                tail=getattr(anchor, "tail_length", 0),
            )
        ]

    def _identity_guard_lines(self) -> list[str]:
        guard = getattr(self._memory_processor, "identity_guard", None)
        if guard is None:
            return [t("identity.guard_unavailable")]
        stats = guard.stats()
        if not stats.get("enabled"):
            return [t("identity.guard_disabled")]
        return [
            t(
                "identity.guard_line",
                tracked=stats.get("tracked_identities", 0),
                max_tracked=stats.get("max_tracked", 0),
                unstable=stats.get("unstable_identities", 0),
            ),
            t(
                "identity.guard_dropped",
                echo=stats.get("dropped_session_echo", 0),
                missing=stats.get("dropped_missing_id", 0),
                corrected=stats.get("bot_names_corrected", 0),
                flagged=stats.get("flagged_unstable", 0),
            ),
            t(
                "identity.guard_window",
                hours=f"{float(stats.get('window_hours', 0.0)):.1f}",
                names=stats.get("max_distinct_names", 0),
            ),
        ]

    async def _identity_alias_lines(self) -> list[str]:
        alias_store = getattr(self.memory_engine, "alias_store", None)
        if alias_store is None:
            return [t("identity.alias_unavailable")]
        try:
            stats = await alias_store.stats()
        except Exception as exc:
            logger.debug(f"读取别名表统计失败: {exc}")
            return [t("identity.alias_unavailable")]
        return [
            t(
                "identity.alias_line",
                aliases=stats.get("aliases", 0),
                identities=stats.get("identities", 0),
                multi=stats.get("multi_alias_identities", 0),
                shared=stats.get("shared_aliases", 0),
                cached=stats.get("cached_aliases", 0),
                cache_max=stats.get("cache_max", 0),
            )
        ]

    def _identity_report_lines(self, report: dict) -> list[str]:
        """Render the per-platform assistant-attribution audit."""
        lines: list[str] = [t("identity.conversation_header")]
        platforms = report.get("platforms") or []
        if not platforms:
            lines.append(t("identity.no_assistant_rows"))
        for item in platforms:
            label = item.get("platform") or t("identity.unknown_platform")
            if item.get("ambiguous"):
                lines.append(t("identity.platform_ambiguous", platform=label))
                continue
            lines.append(
                t(
                    "identity.platform_line",
                    platform=label,
                    bot_name=item.get("bot_name") or "-",
                    bot_id=item.get("bot_id") or "-",
                    source=t("identity.source_live")
                    if item.get("source") == "live"
                    else t("identity.source_majority"),
                )
            )
            lines.append(
                t(
                    "identity.platform_counts",
                    correct=item.get("correct", 0),
                    wrong=item.get("wrong", 0),
                    total=item.get("total", 0),
                )
            )
            for offender in item.get("offenders") or []:
                lines.append(
                    t(
                        "identity.offender_line",
                        sender_name=offender.get("sender_name") or "-",
                        sender_id=offender.get("sender_id") or "-",
                        count=offender.get("count", 0),
                    )
                )

        false_flags = report.get("false_bot_flags") or []
        if false_flags:
            lines.append("")
            lines.append(t("identity.false_flag_header", count=len(false_flags)))
            for row in false_flags[:5]:
                lines.append(
                    t(
                        "identity.false_flag_line",
                        identity=row.get("identity_key") or "-",
                        aliases=row.get("aliases") or "-",
                    )
                )

        backup_rows = int(report.get("backup_rows") or 0)
        if backup_rows > 0:
            lines.append("")
            lines.append(t("identity.backup_line", count=backup_rows))

        total_wrong = int(report.get("total_wrong") or 0)
        lines.append("")
        if total_wrong > 0 or false_flags:
            lines.append(t("identity.fix_hint", count=total_wrong))
        else:
            lines.append(t("identity.clean"))
        return lines

    async def handle_identity(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam identity 命令 - 人名锚定、身份守卫与 Bot 归属诊断"""
        lines: list[str] = [t("identity.header"), ""]
        lines.extend(self._identity_anchor_lines())
        lines.extend(self._identity_guard_lines())
        lines.extend(await self._identity_alias_lines())

        repair, error = self._build_identity_repair("/anam identity")
        if repair is None:
            lines.append("")
            lines.append(error or t("identity.conversation_unavailable"))
            yield event.plain_result("\n".join(lines))
            return

        try:
            report = await repair.analyse(live_bot_ids=self._live_bot_ids(event))
        except Exception as e:
            logger.error(f"身份诊断失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("identity.action_name"),
                    e,
                    t_list("error.suggestions.identity"),
                )
            )
            return

        lines.append("")
        if not report.get("ok"):
            lines.append(
                t("identity.analyse_failed", reason=report.get("reason") or "unknown")
            )
        else:
            lines.extend(self._identity_report_lines(report))
        yield event.plain_result("\n".join(lines))

    async def handle_fix_identity(
        self,
        event: AstrMessageEvent,
        mode: str = "preview",
        platform: str | None = None,
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam fix-identity 命令 - 修复会话库中错误的 Bot 归属"""
        action = _IDENTITY_FIX_MODES.get((mode or "preview").strip().lower())
        if action is None:
            yield event.plain_result(t("fix_identity.invalid_mode", mode=mode))
            return

        repair, error = self._build_identity_repair("/anam fix-identity")
        if repair is None:
            yield event.plain_result(error or t("identity.conversation_unavailable"))
            return

        platform_filter = (platform or "").strip() or None
        try:
            if action == "rollback":
                yield event.plain_result(t("fix_identity.rollback_starting"))
                result = await repair.rollback()
                if not result.get("ok"):
                    yield event.plain_result(
                        t(
                            "fix_identity.failed",
                            reason=result.get("reason") or t("common.unknown_error"),
                        )
                    )
                    return
                if result.get("empty"):
                    yield event.plain_result(t("fix_identity.rollback_empty"))
                    return
                yield event.plain_result(
                    t("fix_identity.rollback_done", count=result.get("restored", 0))
                )
                return

            live_bot_ids = self._live_bot_ids(event)
            if action == "preview":
                report = await repair.analyse(platform_filter, live_bot_ids)
                if not report.get("ok"):
                    yield event.plain_result(
                        t(
                            "fix_identity.failed",
                            reason=report.get("reason") or t("common.unknown_error"),
                        )
                    )
                    return
                lines = [t("fix_identity.preview_header"), ""]
                lines.extend(self._identity_report_lines(report))
                lines.append("")
                lines.append(t("fix_identity.preview_hint"))
                yield event.plain_result("\n".join(lines))
                return

            yield event.plain_result(t("fix_identity.exec_starting"))
            result = await repair.repair(platform_filter, live_bot_ids)
            if not result.get("ok"):
                yield event.plain_result(
                    t(
                        "fix_identity.failed",
                        reason=result.get("reason") or t("common.unknown_error"),
                    )
                )
                return

            repaired = int(result.get("repaired") or 0)
            cleared = int(result.get("cleared_bot_flags") or 0)
            lines = [t("fix_identity.exec_header")]
            if repaired <= 0 and cleared <= 0:
                lines.append(t("fix_identity.nothing_to_do"))
            if repaired > 0:
                lines.append(t("fix_identity.repaired_line", count=repaired))
            if cleared > 0:
                lines.append(t("fix_identity.cleared_flags_line", count=cleared))
            skipped = result.get("skipped") or []
            if skipped:
                lines.append(
                    t(
                        "fix_identity.skipped_line",
                        platforms=", ".join(str(item) for item in skipped),
                    )
                )
            if repaired > 0:
                lines.append("")
                lines.append(t("fix_identity.graph_note"))
                lines.append(t("fix_identity.rollback_hint"))
            yield event.plain_result("\n".join(lines))

        except Exception as e:
            logger.error(f"身份修复失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("fix_identity.action_name"),
                    e,
                    t_list("error.suggestions.fix_identity"),
                )
            )

    async def handle_help(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /anam help 命令"""
        message = t("help.text")
        yield event.plain_result(message)
