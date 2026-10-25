"""
记忆反思模块
负责检查是否需要记忆总结和存储
"""

import asyncio
import inspect
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse

from ..memory_scope import is_event_memory_allowed, resolve_memory_scope
from ..memory_source import serialize_source_messages
from ..utils import get_persona_id

_DEFAULT_MEMORY_SCOPE = object()

if TYPE_CHECKING:
    from ..base.config_manager import ConfigManager
    from ..managers.conversation_manager import ConversationManager
    from ..managers.memory_engine import MemoryEngine
    from ..processors.memory_processor import MemoryProcessor
    from .message_utils import MessageUtils


class MemoryReflection:
    """记忆反思类"""

    def __init__(
        self,
        context: Any,
        config_manager: "ConfigManager",
        memory_engine: "MemoryEngine",
        memory_processor: "MemoryProcessor",
        conversation_manager: "ConversationManager",
        message_utils: "MessageUtils",
        storage_tasks: set[asyncio.Task],
        storage_sessions_inflight: set[str],
        storage_state_lock: asyncio.Lock,
        consolidation_manager=None,
        user_profile_manager=None,
    ):
        """
        初始化记忆反思模块

        Args:
            context: AstrBot上下文
            config_manager: 配置管理器
            memory_engine: 记忆引擎
            memory_processor: 记忆处理器
            conversation_manager: 会话管理器
            message_utils: 消息处理工具
            storage_tasks: 后台存储任务集合（共享状态）
            storage_sessions_inflight: 正在处理的会话集合（共享状态）
            storage_state_lock: 存储状态锁（共享状态）
            consolidation_manager: 记忆整合管理器（用于反思触发）
        """
        self.context = context
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.memory_processor = memory_processor
        self.conversation_manager = conversation_manager
        self.message_utils = message_utils
        self._storage_tasks = storage_tasks
        self._storage_sessions_inflight = storage_sessions_inflight
        self._storage_state_lock = storage_state_lock
        self.consolidation_manager = consolidation_manager
        self.user_profile_manager = user_profile_manager
        self._shutting_down = False

    def _schedule_consolidation(self) -> None:
        """反思触发时在后台顺带检查记忆整合（带冷却，不会频繁执行）。"""
        if self.consolidation_manager is None or self._shutting_down:
            return
        task = asyncio.create_task(self.consolidation_manager.maybe_run("reflection"))
        self._storage_tasks.add(task)
        task.add_done_callback(self._storage_tasks.discard)

    async def handle_memory_reflection(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        """Check if reflection and memory storage is needed after LLM response"""
        logger.debug(
            f"[DEBUG-Reflection] 进入 handle_memory_reflection，resp.role={resp.role}"
        )

        if not is_event_memory_allowed(self.config_manager, event):
            logger.debug("当前事件不在记忆白名单中，跳过记忆反思")
            return

        if resp.role != "assistant":
            return

        # 过滤 tool 循环中间轮次（有工具调用时跳过，等待最终总结轮）
        if resp.tools_call_name:
            logger.debug(
                f"[DEBUG-Reflection] 检测到工具调用响应（tools={resp.tools_call_name}），跳过记录"
            )
            return

        # 过滤 tool 循环最终总结：若本次响应是 tool 调用完成后的总结，
        # 其 tools_call_extra_content 会携带工具调用上下文，说明这是 tool loop 产生的内容
        if resp.tools_call_extra_content:
            logger.debug(
                "[DEBUG-Reflection] 检测到 tool loop 总结响应（tools_call_extra_content 非空），跳过记录"
            )
            return

        try:
            session_id = event.unified_msg_origin
            logger.debug(f"[DEBUG-Reflection] 获取到 unified_msg_origin: {session_id}")

            if not session_id:
                logger.warning("[DEBUG-Reflection] session_id 为空，跳过反思")
                return

            # 检测异常session_id
            if "Error:" in session_id or "error:" in session_id.lower():
                logger.warning(
                    f"[{session_id}] 检测到异常的session_id，这可能导致记忆总结异常。"
                )

            # 检查响应内容是否有效（过滤空回复和错误）
            response_text = resp.completion_text
            if not response_text or not response_text.strip():
                logger.debug(f"[{session_id}] 模型返回空回复，跳过记录")
                return

            # 检查是否为错误响应
            error_indicators = [
                "api error",
                "request failed",
                "rate limit",
                "timeout",
                "connection error",
                "服务暂时不可用",
                "请求失败",
                "接口错误",
            ]
            response_lower = response_text.lower()
            if any(indicator in response_lower for indicator in error_indicators):
                logger.debug(
                    f"[{session_id}] 检测到错误响应，跳过记录: {response_text[:50]}..."
                )
                return

            valid_response = getattr(self.conversation_manager, "response_checkpoint_valid", None)
            if str(event.get_platform_name()).casefold() == "webchat" and callable(valid_response) and not await valid_response(event):
                logger.info("[%s] 跳过已被重试取代或重复的回复", session_id)
                return
            # 添加助手响应
            added_response = await self.conversation_manager.add_message_from_event(
                event=event,
                role="assistant",
                content=response_text,
            )
            if added_response is None:
                return
            logger.debug(f"[DEBUG-Reflection] [{session_id}] 已添加助手响应消息")

            # 私聊：助手消息写入后也执行消息数量上限控制
            is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
            if not is_group:
                await self.message_utils.enforce_message_limit(session_id)

            # 获取会话信息
            session_info = await self.conversation_manager.get_session_info(session_id)
            if not session_info:
                logger.warning(
                    f"[DEBUG-Reflection] [{session_id}] session_info 为 None，跳过反思"
                )
                return

            # 获取实际消息数量（用于数据一致性检查）
            actual_message_count = (
                await self.conversation_manager.store.get_message_count(session_id)
            )

            # 数据一致性检查
            if session_info.message_count != actual_message_count:
                logger.warning(
                    f"[DEBUG-Reflection] [{session_id}] 数据不一致! "
                    f"sessions表记录={session_info.message_count}, "
                    f"实际消息数={actual_message_count}"
                )

            # 使用实际消息数量
            total_messages = actual_message_count
            expected_revision = await self._read_revision(session_id)

            # 检查是否满足总结条件
            trigger_rounds = self.config_manager.get(
                "reflection_engine.summary_trigger_rounds", 10
            )

            # 获取上次总结的位置
            last_summarized_index = (
                await self.conversation_manager.get_session_metadata(
                    session_id, "last_summarized_index", 0
                )
            )

            # 检查 last_summarized_index 是否超出实际消息数量
            # 这种情况通常发生在消息被删除后
            if last_summarized_index > total_messages:
                logger.warning(
                    f"[DEBUG-Reflection] [{session_id}] last_summarized_index({last_summarized_index}) "
                    f"> 实际消息数({total_messages})，调整为当前消息总数"
                )
                # 调整为当前消息总数，而非归零（避免重复处理已总结的内容）
                last_summarized_index = total_messages
                if not await self._write_summary_state(session_id,
                    {"last_summarized_index": total_messages}, expected_revision):
                    return

            # 计算未总结的消息数量
            unsummarized_messages = total_messages - last_summarized_index
            unsummarized_rounds = unsummarized_messages // 2

            # 检查是否有待处理的失败总结
            pending_summary = await self.conversation_manager.get_session_metadata(
                session_id, "pending_summary", None
            )

            # A completed previous day / idle stretch can be sealed before the
            # normal round threshold. Keep the new, short active stretch open.
            from ..utils import get_now_datetime_from_context
            from ..utils.history_alignment import split_summary_windows

            planned_end_index = total_messages
            sealed_end_index = None
            plan_start = last_summarized_index
            if pending_summary and pending_summary.get("retry_count", 0) < 3:
                plan_start = min(plan_start, pending_summary.get("start_index", plan_start))
            planned_history = await self.conversation_manager.get_messages_range(
                session_id=session_id, start_index=plan_start, end_index=total_messages
            )
            if not await self._revision_valid(session_id, expected_revision):
                return
            windows = split_summary_windows(planned_history,
                split_by_day=self.config_manager.get("reflection_engine.split_by_day", True),
                idle_gap_minutes=self.config_manager.get("reflection_engine.idle_gap_minutes", 180),
                tz=get_now_datetime_from_context(self.context).tzinfo)
            if len(windows) > 1:
                sealed_end_index = plan_start + windows[-1][0]
                if (total_messages - sealed_end_index) // 2 < trigger_rounds:
                    planned_end_index = sealed_end_index

            logger.info(
                f"[DEBUG-Reflection] [{session_id}] 总消息数: {total_messages}, "
                f"上次总结位置: {last_summarized_index}, "
                f"未总结轮数: {unsummarized_rounds}, "
                f"触发阈值: {trigger_rounds}轮, "
                f"待处理失败总结: {pending_summary is not None}"
            )

            # 当未总结的轮数达到触发阈值时进行总结
            if unsummarized_rounds >= trigger_rounds or sealed_end_index is not None:
                logger.info(
                        f"[{session_id}] 达到总结阈值或已有封口窗口，启动记忆反思任务"
                )

                self._schedule_consolidation()

                # 计算总结范围（考虑待处理的失败总结）
                start_index = last_summarized_index
                end_index = planned_end_index
                retry_count = 0

                # 如果有待处理的失败总结，合并范围
                if pending_summary:
                    pending_start = pending_summary.get("start_index", start_index)
                    retry_count = pending_summary.get("retry_count", 0)

                    # 检查是否已达到最大重试次数
                    if retry_count >= 3:
                        logger.warning(
                            f"[{session_id}] 待处理总结已连续失败 {retry_count} 次，放弃该范围 "
                            f"[{pending_start}:{pending_summary.get('end_index', end_index)}]"
                        )
                        # Only abandon the failed window. Newly arrived messages
                        # must not disappear merely because an older window failed.
                        abandoned_end = min(total_messages, max(start_index,
                            int(pending_summary.get("end_index", start_index))))
                        if not await self._write_summary_state(session_id,
                            {"pending_summary": None, "last_summarized_index": abandoned_end}, expected_revision):
                            return
                        start_index = abandoned_end
                        retry_count = 0
                        if (end_index - start_index) // 2 < trigger_rounds:
                            return
                    else:
                        # 合并范围：使用待处理的起始位置
                        start_index = pending_start
                        logger.info(
                            f"[{session_id}] 合并待处理失败总结，新范围 [{start_index}:{end_index}], "
                            f"重试次数: {retry_count + 1}/3"
                        )

                if end_index - start_index < 2:
                    logger.debug(f"[{session_id}] 消息数不足一轮对话，跳过总结")
                    return

                messages_to_summarize = end_index - start_index
                rounds_to_summarize = messages_to_summarize // 2

                logger.info(
                    f"[{session_id}] 滑动窗口总结: "
                    f"消息范围 [{start_index}:{end_index}]/{total_messages}, "
                    f"本次总结 {rounds_to_summarize} 轮"
                )

                # 获取需要总结的消息
                if start_index == plan_start and end_index == total_messages:
                    history_messages = planned_history
                elif plan_start <= start_index and end_index <= plan_start + len(planned_history):
                    history_messages = planned_history[start_index-plan_start:end_index-plan_start]
                else:
                    history_messages = await self.conversation_manager.get_messages_range(
                        session_id=session_id, start_index=start_index, end_index=end_index
                    )
                if not await self._revision_valid(session_id, expected_revision):
                    return
                if expected_revision is not None and len(history_messages) != end_index - start_index:
                    return  # History shifted while the window was being read.

                logger.info(
                    f"[{session_id}] 获取到 {len(history_messages)} 条消息用于总结"
                )

                persona_id = await get_persona_id(self.context, event)

                # 创建后台任务进行存储（跟踪任务）
                if not self._shutting_down:
                    async with self._storage_state_lock:
                        if session_id in self._storage_sessions_inflight:
                            logger.info(
                                f"[{session_id}] 已有记忆反思任务在执行，跳过本次触发"
                            )
                            return
                        self._storage_sessions_inflight.add(session_id)

                    try:
                        task = asyncio.create_task(
                            self._storage_windows_task(
                                session_id,
                                history_messages,
                                persona_id,
                                start_index,
                                end_index,
                                retry_count,
                                memory_scope=(
                                    resolve_memory_scope(self.config_manager, event)
                                    or session_id
                                ),
                                event=event,
                                expected_revision=expected_revision,
                            )
                        )
                    except Exception:
                        self._storage_sessions_inflight.discard(session_id)
                        raise

                    self._storage_tasks.add(task)
                    task.add_done_callback(
                        lambda t, sid=session_id: self._on_storage_task_done(t, sid)
                    )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理记忆反思时发生错误: {e}", exc_info=True)

    async def _read_revision(self, session_id):
        reader = getattr(self.conversation_manager, "get_session_revision", None)
        if not inspect.iscoroutinefunction(reader):
            return None
        value = await reader(session_id)
        return value if isinstance(value, int) else None

    async def _revision_valid(self, session_id, expected_revision):
        return expected_revision is None or await self._read_revision(session_id) == expected_revision

    async def _write_summary_state(self, session_id, updates, expected_revision):
        batch = getattr(self.conversation_manager, "update_session_metadata_batch_if_revision", None)
        if expected_revision is not None and inspect.iscoroutinefunction(batch):
            return await batch(session_id, updates, expected_revision)
        # Compatibility with older hosts and minimal test doubles. Production
        # ConversationManager commits these fields with one revision CAS.
        for key, value in updates.items():
            if not await self._revision_valid(session_id, expected_revision):
                return False
            await self.conversation_manager.update_session_metadata(session_id, key, value)
        return True

    async def _storage_windows_task(self, session_id, history_messages, persona_id,
                                    start_index, end_index, retry_count, **kwargs):
        from ..utils import get_now_datetime_from_context
        from ..utils.history_alignment import split_summary_windows

        windows = split_summary_windows(
            history_messages,
            split_by_day=self.config_manager.get("reflection_engine.split_by_day", True),
            idle_gap_minutes=self.config_manager.get("reflection_engine.idle_gap_minutes", 180),
            tz=get_now_datetime_from_context(self.context).tzinfo,
        )
        for start, end in windows:
            if not await self._revision_valid(session_id, kwargs.get("expected_revision")):
                break
            await self._storage_task(session_id, history_messages[start:end], persona_id,
                                     start_index + start, start_index + end, retry_count, **kwargs)
            if not await self._revision_valid(session_id, kwargs.get("expected_revision")):
                break
            progress = await self.conversation_manager.get_session_metadata(
                session_id, "last_summarized_index", 0)
            if not isinstance(progress, int) or progress < start_index + end:
                break  # Retry a failed window before advancing into later windows.
            retry_count = 0  # A failure in a later window starts its own retry budget.

    async def _storage_task(
        self,
        session_id: str,
        history_messages: list[dict],
        persona_id: str,
        start_index: int,
        end_index: int,
        retry_count: int,
        memory_scope: str | None | object = _DEFAULT_MEMORY_SCOPE,
        event: AstrMessageEvent | None = None,
        expected_revision: int | None = None,
    ):
        """后台存储任务"""
        from ..utils import OperationContext

        if memory_scope is _DEFAULT_MEMORY_SCOPE:
            memory_scope = session_id
        if expected_revision is None:
            expected_revision = await self._read_revision(session_id)
        if not await self._revision_valid(session_id, expected_revision):
            return

        async with OperationContext("记忆存储", session_id):
            try:
                # 如果其他任务已经推进了总结进度，本任务可能已过期，直接跳过
                current_summarized = (
                    await self.conversation_manager.get_session_metadata(
                        session_id, "last_summarized_index", 0
                    )
                )
                try:
                    summarized_index = int(current_summarized)
                except (TypeError, ValueError):
                    summarized_index = 0

                if summarized_index >= end_index:
                    logger.info(
                        f"[{session_id}] 检测到过期总结任务，跳过: "
                        f"current={summarized_index}, target_end={end_index}"
                    )
                    return

                # 判断是否为群聊
                is_group_chat = bool(
                    history_messages[0].group_id if history_messages else False
                )
                # 备用判断：从 session_id 解析（防御性编程）
                if not is_group_chat and "GroupMessage" in session_id:
                    is_group_chat = True

                logger.info(
                    f"[{session_id}] 开始处理记忆，类型={'群聊' if is_group_chat else '私聊'}, "
                    f"范围=[{start_index}:{end_index}], 重试次数={retry_count}, "
                    f"当前人格={persona_id or '未设置'}"
                )

                # 使用 MemoryProcessor 处理对话历史
                if not self.memory_engine:
                    logger.error(f"[{session_id}] MemoryEngine 未初始化，记录待重试")
                    await self._record_pending_summary(
                        session_id, start_index, end_index, retry_count, expected_revision
                    )
                    return
                if not self.memory_processor:
                    logger.error(f"[{session_id}] MemoryProcessor 未初始化，记录待重试")
                    await self._record_pending_summary(
                        session_id, start_index, end_index, retry_count, expected_revision
                    )
                    return

                try:
                    logger.info(
                        f"[{session_id}] 调用 MemoryProcessor 处理 {len(history_messages)} 条消息"
                    )
                    (
                        content,
                        metadata,
                        importance,
                    ) = await self.memory_processor.process_conversation(
                        messages=history_messages,
                        is_group_chat=is_group_chat,
                        persona_id=persona_id,
                    )

                    atoms = self.memory_processor.classify_atoms_from_metadata(
                        metadata=metadata,
                        parent_importance=importance,
                        session_id=memory_scope,
                        persona_id=persona_id,
                    )

                    # 补充 source_window 元数据，记录本次总结的消息范围
                    metadata["source_window"] = {
                        "session_id": session_id,
                        "start_index": start_index,
                        "end_index": end_index,
                        "message_count": end_index - start_index,
                    }
                    metadata["source_session_id"] = session_id
                    if expected_revision is not None:
                        metadata["source_revision"] = expected_revision

                    logger.info(
                        f"[{session_id}] 已使用LLM生成结构化记忆, "
                        f"主题={metadata.get('topics', [])}, "
                        f"重要性={importance:.2f}"
                    )

                except Exception as e:
                    # LLM处理失败，记录待重试信息
                    logger.error(
                        f"[{session_id}] LLM处理失败 (重试 {retry_count + 1}/3): {e}",
                        exc_info=True,
                    )
                    if not await self._revision_valid(session_id, expected_revision):
                        return
                    await self._record_pending_summary(
                        session_id, start_index, end_index, retry_count, expected_revision
                    )
                    return

                if not await self._revision_valid(session_id, expected_revision):
                    return
                # 正常流程：添加到记忆引擎
                if self.memory_engine:
                    source_threshold = float(
                        self.config_manager.get(
                            "reflection_engine.source_retention_importance_threshold",
                            0.8,
                        )
                    )
                    source_messages = (
                        serialize_source_messages(history_messages)
                        if importance >= source_threshold
                        else None
                    )
                    write_kwargs = dict(
                        content=content,
                        session_id=memory_scope,
                        persona_id=persona_id,
                        importance=importance,
                        metadata=metadata,
                        atoms=atoms,
                        source_messages=source_messages,
                    )
                    previous_write = None
                    if self.config_manager.get("reflection_engine.dedup_enabled", False):
                        memory_id, previous_write = await self.memory_engine.add_memory_conservatively(
                            threshold=self.config_manager.get("reflection_engine.dedup_similarity_threshold", .97),
                            candidate_count=self.config_manager.get("reflection_engine.dedup_candidate_count", 10),
                            **write_kwargs)
                    else:
                        memory_id = await self.memory_engine.add_memory(**write_kwargs)
                    if memory_id is None:
                        raise RuntimeError("记忆写入未返回 ID，不能推进总结进度")
                    if not await self._revision_valid(session_id, expected_revision):
                        await self.memory_engine.undo_summary_write(memory_id, previous_write)
                        return

                    logger.info(
                        f"[{session_id}] 成功存储对话记忆（{len(history_messages)}条消息，重要性={importance:.2f}）"
                    )

                    if previous_write is None and self.user_profile_manager is not None and event is not None and memory_id is not None:
                        try:
                            await self.user_profile_manager.update_from_messages(
                                event,
                                history_messages,
                                self.memory_processor,
                                source_memory_id=memory_id,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # 普通记忆已落盘，档案提取失败不可重试整个总结窗口。
                            logger.warning(
                                "[%s] 用户档案提取失败，已保留普通记忆",
                                session_id,
                                exc_info=True,
                            )

                    if not await self._revision_valid(session_id, expected_revision):
                        await self.memory_engine.undo_summary_write(memory_id, previous_write)
                        return

                # 成功：更新已总结的位置，清除待处理记录
                if self.conversation_manager:
                    try:
                        committed = await self._write_summary_state(session_id,
                            {"last_summarized_index": end_index, "pending_summary": None}, expected_revision)
                        if not committed:
                            if self.memory_engine:
                                await self.memory_engine.undo_summary_write(memory_id, previous_write)
                            return
                        logger.info(
                            f"[{session_id}] 更新滑动窗口位置: last_summarized_index = {end_index}"
                        )
                    except Exception as meta_err:
                        logger.error(
                            f"[{session_id}] 记忆已存储但元数据更新失败: {meta_err}。"
                            "正在按原历史版本重试提交。",
                            exc_info=True,
                        )
                        # Retry the same atomic CAS; never resurrect an old
                        # index after the chat has been reverted or cleared.
                        try:
                            committed = await self._write_summary_state(session_id,
                                {"last_summarized_index": end_index, "pending_summary": None}, expected_revision)
                            if not committed and self.memory_engine:
                                await self.memory_engine.undo_summary_write(memory_id, previous_write)
                        except Exception:
                            logger.error(
                                f"[{session_id}] 重试元数据更新仍然失败，"
                                "回滚本次写入以便重新总结。",
                                exc_info=True,
                            )
                            if self.memory_engine:
                                await self.memory_engine.undo_summary_write(memory_id, previous_write)
                            await self._record_pending_summary(session_id, start_index,
                                end_index, retry_count, expected_revision)

            except Exception as e:
                logger.error(f"[{session_id}] 存储记忆失败: {e}", exc_info=True)
                if not await self._revision_valid(session_id, expected_revision):
                    return
                await self._record_pending_summary(
                    session_id, start_index, end_index, retry_count, expected_revision
                )

    async def _record_pending_summary(
        self,
        session_id: str,
        start_index: int,
        end_index: int,
        current_retry_count: int,
        expected_revision: int | None = None,
    ):
        """记录待处理的失败总结信息"""
        if not self.conversation_manager:
            return

        new_retry_count = current_retry_count + 1
        pending_summary = {
            "start_index": start_index,
            "end_index": end_index,
            "retry_count": new_retry_count,
        }

        if not await self._write_summary_state(session_id, {"pending_summary": pending_summary}, expected_revision):
            return

        logger.warning(
            f"[{session_id}] 记录待重试总结: 范围=[{start_index}:{end_index}], "
            f"重试次数={new_retry_count}/3"
        )

    def _on_storage_task_done(self, task: asyncio.Task, session_id: str):
        """存储任务完成回调"""
        self._storage_tasks.discard(task)
        self._storage_sessions_inflight.discard(session_id)

        if task.cancelled():
            logger.info(f"[{session_id}] 存储任务已取消")
            return

        exc = task.exception()
        if exc:
            logger.error(f"[{session_id}] 存储任务异常: {exc}", exc_info=exc)

    def set_shutting_down(self, value: bool):
        """设置关闭状态"""
        self._shutting_down = value
