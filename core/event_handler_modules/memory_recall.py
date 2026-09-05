"""
记忆召回模块
负责长期记忆的检索和注入
"""

import asyncio
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart

from ..base.constants import (
    FAKE_TOOL_CALL_ID_PREFIX,
    MEMORY_INJECTION_FOOTER,
    MEMORY_INJECTION_HEADER,
)
from ..memory_scope import is_event_memory_allowed, resolve_memory_scope
from ..utils import (
    OperationContext,
    format_memories_for_fake_tool_call,
    format_memories_for_injection,
    get_persona_id,
)

if TYPE_CHECKING:
    from ..base.config_manager import ConfigManager
    from ..managers.conversation_manager import ConversationManager
    from ..managers.memory_engine import MemoryEngine
    from ..utils.injection_adapter import InjectionAdapter
    from .message_utils import MessageUtils


# 记忆检索默认超时（秒）。<= 0 表示不限时，保留给不希望超时中断检索的用户。
DEFAULT_SEARCH_TIMEOUT_SECONDS = 5.0

# 历史 marker 兼容列表：从上游 livingmemory 迁移过来的老会话历史里，
# 仍可能残留 <RAG-Faiss-Memory> 包裹的注入块，需要一并清理。
LEGACY_INJECTION_MARKERS: tuple[tuple[str, str], ...] = (
    ("<RAG-Faiss-Memory>", "</RAG-Faiss-Memory>"),
)

# 当前 marker + 全部历史 marker，清理时统一遍历
INJECTION_MARKER_PAIRS: tuple[tuple[str, str], ...] = (
    (MEMORY_INJECTION_HEADER, MEMORY_INJECTION_FOOTER),
    *LEGACY_INJECTION_MARKERS,
)

# 预编译清理正则（热路径：每条用户消息都会走一遍）
_INJECTION_BLOCK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(re.escape(header) + r".*?" + re.escape(footer), re.DOTALL)
    for header, footer in INJECTION_MARKER_PAIRS
)

_BLANK_LINES_PATTERN = re.compile(r"\n{3,}")


class MemoryRecall:
    """记忆召回类"""

    def __init__(
        self,
        context,
        config_manager: "ConfigManager",
        memory_engine: "MemoryEngine",
        conversation_manager: "ConversationManager",
        message_utils: "MessageUtils",
        injection_adapter: "InjectionAdapter",
    ):
        """
        初始化记忆召回模块

        Args:
            context: AstrBot上下文
            config_manager: 配置管理器
            memory_engine: 记忆引擎
            conversation_manager: 会话管理器
            message_utils: 消息处理工具
            injection_adapter: 注入适配器
        """
        self.context = context
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.conversation_manager = conversation_manager
        self.message_utils = message_utils
        self.injection_adapter = injection_adapter

    @staticmethod
    def _message_timestamp_seconds(value) -> float | None:
        if isinstance(value, (int, float)):
            timestamp = float(value)
        elif isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                timestamp = float(stripped)
            except ValueError:
                try:
                    timestamp = datetime.fromisoformat(
                        stripped.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    return None
        else:
            return None

        if timestamp > 100_000_000_000:
            timestamp /= 1000.0
        return timestamp if timestamp > 0 else None

    def _resolve_search_timeout(self) -> float | None:
        """解析记忆检索超时秒数；返回 None 表示不限时（配置值 <= 0）"""
        raw = self.config_manager.get(
            "recall_engine.search_timeout_seconds", DEFAULT_SEARCH_TIMEOUT_SECONDS
        )
        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            logger.debug(
                f"recall_engine.search_timeout_seconds 配置值非法（{raw!r}），"
                f"回退默认 {DEFAULT_SEARCH_TIMEOUT_SECONDS}s",
                exc_info=True,
            )
            timeout = DEFAULT_SEARCH_TIMEOUT_SECONDS
        return timeout if timeout > 0 else None

    async def handle_memory_recall(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """Query and inject long-term memory before LLM request"""
        try:
            if not is_event_memory_allowed(self.config_manager, event):
                logger.debug("当前事件不在记忆白名单中，跳过记忆召回")
                return
            session_id = event.unified_msg_origin
            logger.debug(f"[DEBUG-Recall] 获取到 unified_msg_origin: {session_id}")

            # 检测异常session_id
            if session_id and (
                "Error:" in session_id or "error:" in session_id.lower()
            ):
                logger.warning(
                    f"[{session_id}] 检测到异常的session_id，这可能导致记忆功能异常。"
                )

            async with OperationContext("记忆召回", session_id):
                prompt_text = getattr(req, "prompt", "")
                extra_parts = getattr(req, "extra_user_content_parts", [])
                has_prompt_text = isinstance(prompt_text, str) and bool(
                    prompt_text.strip()
                )
                has_extra_parts = bool(extra_parts)

                if not has_prompt_text and not has_extra_parts:
                    logger.debug(f"[{session_id}] 请求中无可用用户内容，跳过记忆召回")
                    return

                normalized = self._normalize_text_only_context_parts(req, session_id)
                if normalized > 0:
                    logger.debug(f"[{session_id}] 已归一化 {normalized} 条纯文本历史消息")

                # 自动删除旧的注入记忆
                if self.config_manager.get("recall_engine.auto_remove_injected", True):
                    removed = self._remove_injected_memories_from_context(
                        req, session_id
                    )
                    removed += self._remove_fake_tool_call_from_context(req, session_id)
                    if removed > 0:
                        logger.debug(
                            f"[{session_id}] 已清理 {removed} 处历史记忆注入片段"
                        )

                # 先提取用户消息（消息存储和召回都需要）
                actual_query = await self.message_utils.get_event_message_str(event)

                request_query = (
                    prompt_text.strip() if isinstance(prompt_text, str) else ""
                )

                # 存储用户消息（仅私聊），无论是否启用召回都需要
                is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
                if not is_group and actual_query:
                    message_to_store = request_query
                    if not message_to_store:
                        message_to_store = (
                            await self.message_utils.extract_message_content(event, req)
                        )
                    if not message_to_store:
                        message_to_store = actual_query.strip()
                    await self.conversation_manager.add_message_from_event(
                        event=event,
                        role="user",
                        content=message_to_store,
                    )
                    await self.message_utils.enforce_message_limit(session_id)

                # 若 top_k <= 0，跳过记忆检索和注入，但上述清理和消息存储已执行
                top_k = self.config_manager.get("recall_engine.top_k", 5)
                if top_k <= 0:
                    logger.debug(
                        f"[{session_id}] top_k={top_k} <= 0，跳过记忆检索和注入"
                    )
                    return

                if not actual_query:
                    logger.warning(f"[{session_id}] 原始用户消息为空，跳过记忆召回")
                    return

                # 获取过滤配置
                filtering_config = self.config_manager.filtering_settings
                use_persona_filtering = filtering_config.get(
                    "use_persona_filtering", True
                )

                # 获取 persona_id，与 AstrBot 主流程保持一致的三级优先级：
                # 1. session_service_config（最高）
                # 2. req.conversation.persona_id（会话级）
                # 3. 全局默认人格（最低）
                # 注意：on_llm_request 钩子在 _ensure_persona_and_skills 之前触发，
                # 因此不能直接依赖 req.system_prompt 已注入人格，需自行走完整优先级。
                persona_id = await get_persona_id(self.context, event)

                recall_session_id = resolve_memory_scope(self.config_manager, event)
                recall_persona_id = persona_id if use_persona_filtering else None

                # 使用原始用户输入作为召回关键字
                query_for_search = actual_query

                # 上下文扩展：拼接最近2轮对话作为查询，提升检索精准度
                if self.config_manager.get(
                    "recall_engine.inject_with_recent_context", False
                ):
                    try:
                        recent_messages = (
                            await self.conversation_manager.get_context(
                                session_id,
                                max_messages=5,
                                format_for_llm=False,
                            )
                        )
                        if recent_messages and len(recent_messages) > 1:
                            # recent_messages 按 timestamp DESC 排列（最新在前）
                            # 跳过索引0（当前消息），取后续消息作为扩展上下文
                            context_parts = []
                            max_age_seconds = self.config_manager.get(
                                "recall_engine.recent_context_max_age_seconds", 7200
                            )
                            now = time.time()
                            skipped_by_age = 0
                            for msg in reversed(recent_messages[1:]):
                                if max_age_seconds > 0:
                                    timestamp = self._message_timestamp_seconds(
                                        msg.get("timestamp")
                                    )
                                    if (
                                        timestamp is None
                                        or now - timestamp > max_age_seconds
                                    ):
                                        skipped_by_age += 1
                                        continue
                                content = msg.get("content", "")
                                if content and content.strip():
                                    context_parts.append(content.strip())
                            if context_parts:
                                expanded = " | ".join(context_parts)
                                query_for_search = expanded + " " + actual_query
                                logger.debug(
                                    f"[{session_id}] 上下文扩展查询: "
                                    f"{len(context_parts)}条历史消息 + 当前消息，"
                                    f"按时间跳过={skipped_by_age}条"
                                )
                    except Exception as e:
                        logger.warning(f"[{session_id}] 获取上下文扩展失败: {e}")

                # 执行记忆召回
                logger.debug(
                    f"[{session_id}] 开始记忆召回，查询='{query_for_search[:80]}...'"
                )

                # 记忆检索跑在 on_llm_request 钩子里，慢检索会直接阻塞用户回复，
                # 因此必须有超时兜底：超时后静默跳过注入，不影响用户拿到回复。
                search_timeout = self._resolve_search_timeout()
                search_kwargs = {
                    "query": query_for_search,
                    "k": self.config_manager.get("recall_engine.top_k", 5),
                    "session_id": recall_session_id,
                    "persona_id": recall_persona_id,
                }
                try:
                    if search_timeout is None:
                        # 配置 <= 0：用户显式选择不限时，保持旧行为
                        recalled_memories = await self.memory_engine.search_memories(
                            **search_kwargs
                        )
                    else:
                        # wait_for 会在超时时取消内层协程并 await 其收尾，
                        # 不会留下半完成状态或 "Task exception was never retrieved" 告警。
                        recalled_memories = await asyncio.wait_for(
                            self.memory_engine.search_memories(**search_kwargs),
                            timeout=search_timeout,
                        )
                except (asyncio.TimeoutError, TimeoutError):
                    logger.warning(
                        f"[{session_id}] 记忆检索超过 {search_timeout}s 超时，"
                        f"本轮跳过记忆注入"
                    )
                    return

                if recalled_memories:
                    logger.debug(
                        f"[{session_id}] 检索到 {len(recalled_memories)} 条记忆"
                    )

                    # 格式化并注入记忆
                    memory_list = [
                        {
                            "id": getattr(mem, "doc_id", None),
                            "content": mem.content,
                            "score": mem.final_score,
                            "metadata": mem.metadata,
                            "timestamp": mem.metadata.get("create_time"),
                        }
                        for mem in recalled_memories
                    ]

                    # 输出详细记忆信息
                    for i, mem in enumerate(recalled_memories, 1):
                        logger.debug(
                            f"[{session_id}] 记忆 #{i}: 得分={mem.final_score:.3f}, "
                            f"重要性={mem.metadata.get('importance', 0.5):.2f}, "
                            f"内容={mem.content[:100]}..."
                        )

                    # 根据配置选择注入方式（含 Provider 兼容降级）
                    configured_method = self.config_manager.get(
                        "recall_engine.injection_method", "extra_user_content"
                    )
                    provider = None
                    if configured_method in (
                        "fake_tool_call",
                        "fake_tool_call_deepseek_v4",
                    ):
                        try:
                            provider = self.context.get_using_provider(session_id)
                        except Exception as e:
                            logger.warning(
                                f"[{session_id}] 获取当前 Provider 失败，"
                                f"将按无 Provider 继续解析注入模式: {e}"
                            )
                    injection_method, fallback_reason = (
                        self.injection_adapter.resolve(provider, configured_method)
                    )
                    if fallback_reason:
                        logger.warning(
                            f"[{session_id}] 注入模式从 {configured_method} 降级为 "
                            f"{injection_method}: {fallback_reason}"
                        )

                    memory_str = format_memories_for_injection(memory_list)

                    if injection_method == "user_message_before":
                        req.prompt = memory_str + "\n\n" + (req.prompt or "")
                        logger.debug(
                            f"[{session_id}] 成功向用户消息前注入 {len(recalled_memories)} 条记忆"
                        )
                    elif injection_method == "user_message_after":
                        req.prompt = (req.prompt or "") + "\n\n" + memory_str
                        logger.debug(
                            f"[{session_id}] 成功向用户消息后注入 {len(recalled_memories)} 条记忆"
                        )
                    elif injection_method == "fake_tool_call":
                        fake_messages = format_memories_for_fake_tool_call(
                            memory_list,
                            query=actual_query,
                            k=self.config_manager.get("recall_engine.top_k", 5),
                            session_filtered=recall_session_id is not None,
                            persona_filtered=use_persona_filtering,
                        )
                        if fake_messages:
                            req.contexts.extend(fake_messages)
                            logger.debug(
                                f"[{session_id}] 成功以伪造工具调用方式注入 "
                                f"{len(recalled_memories)} 条记忆"
                            )
                    else:
                        # extra_user_content（推荐）：追加到用户消息末尾，
                        # 不影响前缀缓存且 mark_as_temp 后不污染对话历史
                        req.extra_user_content_parts.append(
                            TextPart(text=memory_str).mark_as_temp()
                        )
                        logger.debug(
                            f"[{session_id}] 成功向用户消息末尾注入 "
                            f"{len(recalled_memories)} 条记忆"
                        )
                else:
                    logger.debug(f"[{session_id}] 未找到相关记忆")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理 on_llm_request 钩子时发生错误: {e}", exc_info=True)

    # ==================== 注入清理 ====================

    @staticmethod
    def _part_type(part) -> str | None:
        """读取 content part 的类型，兼容 dict 与 ContentPart 对象"""
        if isinstance(part, dict):
            part_type = part.get("type")
        else:
            part_type = getattr(part, "type", None)
        return part_type if isinstance(part_type, str) else None

    @staticmethod
    def _part_text(part) -> str:
        """读取 content part 的文本，兼容 dict 与 ContentPart 对象"""
        if isinstance(part, dict):
            text = part.get("text")
        else:
            text = getattr(part, "text", None)
        if isinstance(text, str):
            return text
        return "" if text is None else str(text)

    @staticmethod
    def _part_no_save(part) -> bool:
        """判断 content part 是否被宿主标记为不持久化（临时内容）"""
        if isinstance(part, dict):
            return bool(part.get("_no_save"))
        return bool(getattr(part, "_no_save", False))

    @staticmethod
    def _with_part_text(part, text: str):
        """返回替换过文本的 part

        dict 走浅拷贝，对象就地赋值以保留 _no_save 等私有属性与对象 identity。
        """
        if isinstance(part, dict):
            return {**part, "text": text}
        try:
            part.text = text
        except Exception:
            logger.debug("写入 content part 文本失败，保持原样", exc_info=True)
        return part

    @staticmethod
    def _strip_injected_markers(text: str) -> tuple[str, bool]:
        """清理文本中的记忆注入块（含历史 marker 兼容）

        Returns:
            tuple[str, bool]: (清理后的文本, 是否发生了变化)
        """
        if not isinstance(text, str) or not text:
            return (text if isinstance(text, str) else ""), False

        cleaned = text
        for pattern in _INJECTION_BLOCK_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        if cleaned == text:
            return text, False

        cleaned = _BLANK_LINES_PATTERN.sub("\n\n", cleaned).strip()
        return cleaned, True

    @staticmethod
    def _msg_get(msg, key: str, default=None):
        """读取消息字段，兼容 dict 与 Message 对象"""
        if isinstance(msg, dict):
            return msg.get(key, default)
        return getattr(msg, key, default)

    @staticmethod
    def _msg_set(msg, key: str, value) -> None:
        """写入消息字段，兼容 dict 与 Message 对象"""
        if isinstance(msg, dict):
            msg[key] = value
        else:
            setattr(msg, key, value)

    @staticmethod
    def _msg_drop(msg, key: str) -> None:
        """移除消息字段：dict 直接删键，对象置 None"""
        if isinstance(msg, dict):
            msg.pop(key, None)
        else:
            setattr(msg, key, None)

    @classmethod
    def _has_message_content(cls, msg) -> bool:
        """判断消息除 tool_calls 外是否还有实际内容"""
        content = cls._msg_get(msg, "content")
        if content is None:
            return False
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return bool(content)
        return True

    def _remove_injected_memories_from_context(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """从请求上下文中移除临时注入的记忆片段

        覆盖三处：
        1. req.system_prompt（旧版本注入残留）
        2. req.contexts（user_message_before/after 注入方式改写 req.prompt 后，
           会被 AstrBot 持久化进会话历史，不清理会随对话轮数不断累积）
        3. req.extra_user_content_parts（靠 mark_as_temp/_no_save 标记）
        """
        removed = 0

        # 清理 system_prompt（兼容旧版本注入残留）
        system_prompt = getattr(req, "system_prompt", None)
        if isinstance(system_prompt, str) and system_prompt:
            cleaned_prompt, changed = self._strip_injected_markers(system_prompt)
            if changed:
                req.system_prompt = cleaned_prompt
                removed += 1

        # 清理会话历史（Bug C：注入进 req.prompt 的记忆会被持久化进 contexts）
        removed += self._remove_injected_memories_from_contexts(req, session_id)

        # 清理 extra_user_content_parts（通过 mark_as_temp/_no_save 标记）
        parts_before = len(getattr(req, "extra_user_content_parts", []))
        if parts_before > 0:
            req.extra_user_content_parts = [
                part
                for part in req.extra_user_content_parts
                if not self._is_anamnesis_temp_part(part)
            ]
            parts_after = len(req.extra_user_content_parts)
            removed += parts_before - parts_after

        return removed

    def _remove_injected_memories_from_contexts(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """清理 req.contexts 中残留的记忆注入块

        注意：即使某条消息的 content 被清空，也**保留该条消息**（退化为空串），
        而不是把它从 contexts 里删掉——删除会打断 user/assistant 的角色交替，
        Anthropic / Gemini 等 Provider 会直接拒绝这种请求；而空串是各家都能接受的
        形态，代价只是一条空消息，远小于请求直接失败。

        Returns:
            int: 被改动过的消息条数
        """
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list) or not contexts:
            return 0

        removed = 0
        for msg in contexts:
            try:
                content = self._msg_get(msg, "content")

                if isinstance(content, str):
                    cleaned, changed = self._strip_injected_markers(content)
                    if changed:
                        self._msg_set(msg, "content", cleaned)
                        removed += 1
                    continue

                if not isinstance(content, list) or not content:
                    continue

                new_parts: list = []
                changed = False
                for part in content:
                    # 非文本 part（图片 / 音频等）原样保留，保持对象 identity（Bug K）
                    if self._part_type(part) != "text":
                        new_parts.append(part)
                        continue

                    cleaned_text, part_changed = self._strip_injected_markers(
                        self._part_text(part)
                    )
                    if not part_changed:
                        new_parts.append(part)
                        continue

                    changed = True
                    # 整个 part 都是注入块时直接丢弃该 part
                    if cleaned_text:
                        new_parts.append(self._with_part_text(part, cleaned_text))

                if not changed:
                    continue

                # 所有 part 都被清空时退化为空串占位，理由见方法 docstring
                self._msg_set(msg, "content", new_parts if new_parts else "")
                removed += 1
            except Exception:
                logger.debug(
                    f"[{session_id}] 清理历史消息中的记忆注入块失败，已跳过该条",
                    exc_info=True,
                )

        return removed

    def _is_anamnesis_temp_part(self, part) -> bool:
        """判断是否为 Anamnesis 本轮临时注入的 extra_user_content part"""
        text = self._part_text(part)
        return (
            self._part_no_save(part)
            and MEMORY_INJECTION_HEADER in text
            and MEMORY_INJECTION_FOOTER in text
        )

    def _normalize_text_only_context_parts(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """把历史中的纯文本 content parts 折叠回字符串，避免污染长期上下文格式

        只在**所有** part 都是普通文本时才折叠（Bug K）：
        - 含 image_url / audio_url 等非文本 part 时保持原结构，否则会丢图
        - 含被宿主标记 _no_save 的临时 part 时也不折叠，否则临时内容会被永久化
        """
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list):
            return 0

        normalized = 0
        for msg in contexts:
            if self._msg_get(msg, "role") != "user":
                continue
            content = self._msg_get(msg, "content")
            if not isinstance(content, list) or not content:
                continue

            text_parts: list[str] = []
            text_only = True
            for part in content:
                if self._part_type(part) != "text" or self._part_no_save(part):
                    text_only = False
                    break
                text_parts.append(self._part_text(part))

            if not text_only:
                continue

            self._msg_set(msg, "content", "".join(text_parts))
            normalized += 1

        if normalized:
            logger.debug(
                f"[{session_id}] 已归一化 {normalized} 条纯文本历史 content parts"
            )
        return normalized

    # ==================== 伪造工具调用回收 ====================

    @staticmethod
    def _is_fake_tool_call(tool_call) -> bool:
        """判断是否为本插件伪造的工具调用

        只按 ID 前缀匹配：伪造调用复用了真实工具名 FAKE_TOOL_CALL_NAME，
        按 name 匹配会把用户真实触发的同名调用一起删掉。
        """
        if isinstance(tool_call, dict):
            call_id = tool_call.get("id", "")
        else:
            call_id = getattr(tool_call, "id", "")
        return isinstance(call_id, str) and call_id.startswith(
            FAKE_TOOL_CALL_ID_PREFIX
        )

    def _purge_fake_tool_calls(self, messages: list, session_id: str) -> int:
        """就地清理消息列表中的伪造工具调用（dict 与 Message 对象通用）

        倒序单轮扫描，逐条处理：
        - role=="tool" 且 tool_call_id 带伪造前缀 → 删除（可回收上一轮遗留的孤儿消息）
        - role=="assistant" → 只摘掉伪造的 tool_call，保留同一条里的真实调用；
          伪造调用全部摘掉且消息没有正文时才删除整条

        Returns:
            int: 删除或改动过的消息条数
        """
        removed = 0
        try:
            for i in range(len(messages) - 1, -1, -1):
                msg = messages[i]
                role = self._msg_get(msg, "role")

                if role == "tool":
                    call_id = self._msg_get(msg, "tool_call_id", "") or ""
                    if isinstance(call_id, str) and call_id.startswith(
                        FAKE_TOOL_CALL_ID_PREFIX
                    ):
                        messages.pop(i)
                        removed += 1
                    continue

                if role != "assistant":
                    continue

                tool_calls = self._msg_get(msg, "tool_calls")
                if not isinstance(tool_calls, list) or not tool_calls:
                    continue

                kept = [tc for tc in tool_calls if not self._is_fake_tool_call(tc)]
                if len(kept) == len(tool_calls):
                    continue

                if kept:
                    # 同一条里还有真实调用，只摘掉伪造的那些
                    self._msg_set(msg, "tool_calls", kept)
                elif self._has_message_content(msg):
                    # 消息本身还有正文，只摘掉 tool_calls，避免丢失助手回复
                    self._msg_drop(msg, "tool_calls")
                else:
                    messages.pop(i)
                removed += 1
        except Exception:
            logger.debug(
                f"[{session_id}] 清理伪造工具调用消息时出错，已跳过",
                exc_info=True,
            )

        return removed

    def _remove_fake_tool_call_from_context(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """从请求上下文中移除伪造的工具调用记忆（fake_tool_call 注入方式）

        识别并移除以 FAKE_TOOL_CALL_ID_PREFIX 为 ID 前缀的
        assistant(tool_calls) + tool(result) 消息对。
        """
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list) or not contexts:
            return 0
        return self._purge_fake_tool_calls(contexts, session_id)

    def remove_fake_tool_call_from_agent_messages(
        self, target, session_id: str = ""
    ) -> int:
        """在**同一次请求内**回收伪造工具调用消息（Bug D）

        背景：AstrBot 4.25.0 的 `_no_save` 过滤只认 assistant/user 两种角色
        （agent_sub_stages/internal.py 的
        `if message.role in ["assistant", "user"] and message._no_save: continue`），
        而伪造注入是 assistant(tool_calls) + tool(result) 消息对，给它们打 `_no_save`
        只会保住 assistant 半边、留下孤儿 tool 消息，Gemini Provider 又不过滤孤儿
        tool 消息，会直接报错。因此不硬造标记。

        改为在 OnAgentDoneEvent 时机就地把伪造消息摘掉——该钩子早于
        internal.py 的 `_save_to_history`，且 `run_context.messages` 正是被持久化的
        那个 list 对象，就地摘掉即不会落库，无需依赖下一轮回收。

        Args:
            target: Agent 的 run_context（取其 .messages）或消息列表本身
            session_id: 仅用于日志

        Returns:
            int: 删除或改动过的消息条数
        """
        messages = getattr(target, "messages", target)
        if not isinstance(messages, list) or not messages:
            return 0
        return self._purge_fake_tool_calls(messages, session_id)
