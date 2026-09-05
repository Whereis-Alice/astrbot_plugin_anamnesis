"""
记忆处理器 - 使用LLM将对话历史处理为结构化记忆
"""

import ast
import asyncio
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..models.conversation_models import Message
from .memory_processor_parse import MemoryProcessorParseMixin
from .memory_processor_build import MemoryProcessorBuildMixin

# 后备提示词文件的进程内缓存：{绝对路径: (mtime, 文件内容)}
# PromptManager 不可用时会退化到直接读盘，而 _load_prompts_fallback 位于
# 每轮对话处理的热路径上，按 mtime 失效既避免重复磁盘 IO，又保证用户改动
# 提示词文件后仍能生效。
_PROMPT_FILE_CACHE: dict[str, tuple[float, str]] = {}


def _read_prompt_file_cached(path: Path) -> str:
    """按 mtime 缓存地读取提示词文件，避免热路径上重复磁盘 IO。"""
    key = str(path)
    mtime = path.stat().st_mtime
    cached = _PROMPT_FILE_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    with open(path, encoding="utf-8") as f:
        content = f.read()
    _PROMPT_FILE_CACHE[key] = (mtime, content)
    return content


class MemoryProcessor(MemoryProcessorParseMixin, MemoryProcessorBuildMixin):
    """
    记忆处理器

    使用LLM将对话历史转换为结构化记忆。
    支持私聊和群聊两种场景的不同处理策略。
    """

    def __init__(
        self,
        context=None,
        llm_provider: Any = None,
        config: dict[str, Any] | None = None,
    ):
        """
        初始化记忆处理器

        Args:
            context: AstrBot上下文,用于获取人格管理器
            llm_provider: LLM Provider 实例或 Provider ID 字符串。
                          传入实例时直接使用（测试用）；传入字符串时动态解析。
                          留空则使用AstrBot默认Provider。
            config: 记忆处理器配置。
        """
        self.context = context
        self._llm_provider = llm_provider
        self.config = config or {}

        # 加载提示词模板
        self._load_prompts()

    def _get_current_llm_provider(self):
        """动态解析LLM Provider以避免持有过期引用

        AstrBot可能在运行期间重新创建Provider实例（例如配置变更后），
        旧的Provider实例内部的httpx client会被关闭，导致
        RuntimeError: Cannot send a request, as the client has been closed.
        因此每次调用前都从AstrBot上下文重新获取当前有效的Provider。
        """
        if not self.context:
            # 无 context 时直接返回传入的 provider 实例（测试路径）
            if self._llm_provider is not None and not isinstance(
                self._llm_provider, str
            ):
                return self._llm_provider
            return None

        # 如果传入的是 provider 实例（非字符串），直接使用（测试路径）
        if self._llm_provider is not None and not isinstance(self._llm_provider, str):
            return self._llm_provider

        # 优先使用配置中指定的Provider ID（字符串）
        if isinstance(self._llm_provider, str) and self._llm_provider:
            try:
                provider = self.context.get_provider_by_id(self._llm_provider)
                if provider:
                    return provider
            except Exception:
                logger.debug(
                    f"[MemoryProcessor] 按 ID 获取 Provider 失败: {self._llm_provider}",
                    exc_info=True,
                )

        # 回退到AstrBot当前默认Provider
        try:
            provider = self.context.get_using_provider()
            if provider:
                return provider
        except Exception:
            logger.debug("[MemoryProcessor] 获取默认 Provider 失败", exc_info=True)

        return None

    def _load_prompts(self) -> None:
        """从 PromptManager 加载提示词模板（支持用户自定义覆盖）"""
        try:
            from ..prompts.prompt_manager import get_prompt_manager

            mgr = get_prompt_manager()
            if mgr is not None:
                self.private_chat_prompt = mgr.get_prompt("private_chat_prompt")
                self.group_chat_prompt = mgr.get_prompt("group_chat_prompt")
                logger.debug(
                    "[MemoryProcessor] 通过 PromptManager 加载提示词模板成功"
                )
            else:
                self._load_prompts_fallback()
        except Exception as e:
            logger.error(f"[MemoryProcessor] 通过 PromptManager 加载提示词失败: {e}")
            self._load_prompts_fallback()

    def _get_chat_prompt(self, is_group_chat: bool) -> str:
        """每次处理时从 PromptManager 实时读取，确保 WebUI 保存后立即生效。"""
        try:
            from ..prompts.prompt_manager import get_prompt_manager

            mgr = get_prompt_manager()
            if mgr is not None:
                prompt_id = "group_chat_prompt" if is_group_chat else "private_chat_prompt"
                return mgr.get_prompt(prompt_id)
        except Exception:
            logger.debug(
                "[MemoryProcessor] 从 PromptManager 实时读取提示词失败，回退到已加载模板",
                exc_info=True,
            )
        return self.group_chat_prompt if is_group_chat else self.private_chat_prompt

    def _load_prompts_fallback(self) -> None:
        """后备加载：直接从文件读取提示词"""
        prompt_dir = Path(__file__).parent.parent / "prompts"

        try:
            # 走进程内 mtime 缓存，避免每次实例化都重复读盘
            self.private_chat_prompt = _read_prompt_file_cached(
                prompt_dir / "private_chat_prompt.txt"
            )
            self.group_chat_prompt = _read_prompt_file_cached(
                prompt_dir / "group_chat_prompt.txt"
            )

            logger.debug("[MemoryProcessor] 提示词模板加载成功（后备模式）")

        except Exception as e:
            logger.error(f"[MemoryProcessor] 加载提示词模板失败: {e}")
            self.private_chat_prompt = """分析以下对话并生成JSON格式的记忆:
{conversation}

输出格式:
{"summary": "摘要", "topics": ["主题"], "key_facts": ["事实"], "sentiment": "neutral", "importance": 0.5}
"""
            self.group_chat_prompt = """分析以下群聊对话并生成JSON格式的记忆:
{conversation}

输出格式:
{"summary": "摘要", "topics": ["主题"], "key_facts": ["事实"], "participants": ["参与者"], "sentiment": "neutral", "importance": 0.5}
"""

    async def _build_system_prompt_with_persona(self, persona_id: str | None) -> str:
        """
        构建包含人格提示的 system_prompt

        Args:
            persona_id: 人格ID

        Returns:
            str: 包含人格提示的 system_prompt
        """
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 尝试从 PromptManager 获取基础 system prompt
        try:
            from ..prompts.prompt_manager import get_prompt_manager

            mgr = get_prompt_manager()
            if mgr is not None:
                base_prompt = mgr.get_prompt("memory_system_prompt_base").replace(
                    "{current_date}", current_date
                )
            else:
                base_prompt = self._build_base_prompt_fallback(current_date)
        except Exception:
            base_prompt = self._build_base_prompt_fallback(current_date)

        if not persona_id:
            logger.debug("[MemoryProcessor] 未指定人格ID，使用基础提示词")
            return base_prompt

        if not self.context:
            logger.debug("[MemoryProcessor] Context 未设置，使用基础提示词")
            return base_prompt

        try:
            persona_manager = getattr(self.context, "persona_manager", None)
            if not persona_manager:
                logger.warning(
                    "[MemoryProcessor] persona_manager 不可用，使用基础提示词"
                )
                return base_prompt

            persona = await persona_manager.get_persona(persona_id)
            if not persona:
                logger.warning(
                    f"[MemoryProcessor] 人格 '{persona_id}' 不存在，使用基础提示词"
                )
                return base_prompt

            if not persona.system_prompt:
                logger.debug(
                    f"[MemoryProcessor] 人格 '{persona_id}' 无 system_prompt，使用基础提示词"
                )
                return base_prompt

            persona_prompt = persona.system_prompt.strip()
            if not persona_prompt:
                logger.debug(
                    f"[MemoryProcessor] 人格 '{persona_id}' 的 system_prompt 为空，使用基础提示词"
                )
                return base_prompt

            logger.debug(
                f"[MemoryProcessor] 成功加载人格 '{persona_id}' 的提示词 "
                f"(长度={len(persona_prompt)}字符)"
            )
            logger.debug(f"[MemoryProcessor] 人格提示词预览: {persona_prompt[:100]}...")

            # 使用 PromptManager 模板构建增强提示词
            try:
                if mgr is not None:
                    enhanced_template = mgr.get_prompt(
                        "memory_system_prompt_with_persona"
                    )
                    enhanced_prompt = (
                        enhanced_template.replace("{base_prompt}", base_prompt)
                        .replace("{persona_prompt}", persona_prompt)
                        .replace("{current_date}", current_date)
                    )
                else:
                    enhanced_prompt = self._build_enhanced_prompt_fallback(
                        base_prompt, persona_prompt, current_date
                    )
            except Exception:
                enhanced_prompt = self._build_enhanced_prompt_fallback(
                    base_prompt, persona_prompt, current_date
                )

            return enhanced_prompt

        except ValueError as e:
            logger.warning(f"[MemoryProcessor] 人格 '{persona_id}' 不存在: {e}")
            return base_prompt
        except Exception as e:
            logger.error(
                f"[MemoryProcessor] 获取人格提示词时发生错误: {e}", exc_info=True
            )
            return base_prompt

    @staticmethod
    def _build_base_prompt_fallback(current_date: str) -> str:
        """后备基础 system prompt（当 PromptManager 不可用时）"""
        return (
            "你正在总结对话记忆。请严格按照JSON格式输出。\n"
            f"当前日期时间: {current_date}\n"
            "重要: 请将对话中出现的相对时间表达（如\u201c今天\u201d、"
            "\u201c明天\u201d、\u201c昨天\u201d、"
            "\u201c下周\u201d、\u201c上个月\u201d等）"
            "转换为具体日期后再写入记忆，以便未来查阅时仍能准确理解时间信息。"
        )

    @staticmethod
    def _build_enhanced_prompt_fallback(
        base_prompt: str, persona_prompt: str, current_date: str
    ) -> str:
        """后备增强 system prompt（当 PromptManager 不可用时）"""
        return (
            f"{base_prompt}\n\n"
            f"## 你的人格设定\n"
            f"{persona_prompt}\n\n"
            f"## 记忆总结要求\n"
            f"在总结对话记忆时,你需要:\n"
            f"1. **保持你的人格特色**: 使用符合上述人格设定的语气、用词习惯和表达方式\n"
            f'2. **第一人称视角**: 以"我"的视角回顾对话,不要说"bot"、"助手"等第三人称\n'
            f"3. **体现你的关注点**: 根据你的人格特点,侧重记录你会关注的信息\n"
            f"4. **自然真实**: 让记忆读起来像是你本人在回忆这段对话,而不是机械的客观描述\n"
            f"5. **时间转换**: 将对话中的相对时间（今天、明天、下周等）转换为具体日期（当前日期: {current_date}）\n\n"
            f"例如:\n"
            f'- 如果你是活泼可爱的性格,记忆中可以使用"呀"、"呢"、"~"等语气词\n'
            f"- 如果你是专业严谨的性格,记忆应该用词准确、逻辑清晰、格式规范\n"
            f"- 如果你是幽默风趣的性格,记忆中可以包含轻松的表达和有趣的观察"
        )

    async def _call_llm_with_retry(
        self, prompt: str, system_prompt: str, max_retries: int = 3
    ) -> str:
        """
        带指数退避的 LLM 调用

        Args:
            prompt: 提示词
            system_prompt: 系统提示词
            max_retries: 最大重试次数

        Returns:
            LLM 响应文本
        """
        last_error = None
        for attempt in range(max_retries):
            try:
                provider = self._get_current_llm_provider()
                if not provider:
                    raise RuntimeError("LLM Provider 不可用")
                response = await provider.text_chat(
                    prompt=prompt, system_prompt=system_prompt
                )
                return response.completion_text
            except Exception as e:
                last_error = e
                if attempt == max_retries - 1:
                    raise
                wait_time = (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    f"[MemoryProcessor] LLM 调用失败，{wait_time:.1f}s 后重试 "
                    f"({attempt + 1}/{max_retries}): {e}"
                )
                await asyncio.sleep(wait_time)
        if last_error:
            raise last_error
        raise RuntimeError("LLM 调用失败，未捕获到具体异常")

    # ---------- JSON 修复辅助 ----------

    @staticmethod
    def _is_valid_json(text: str) -> bool:
        """判断字符串当前是否已可被 json.loads 解析。"""
        if not text:
            return False
        try:
            json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
            return False
        return True

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """移除 Markdown 代码块围栏（```json ... ``` 或 ``` ... ```）。"""
        fixed = text
        if fixed.startswith("```json"):
            fixed = fixed[7:]
        elif fixed.startswith("```"):
            fixed = fixed[3:]
        if fixed.endswith("```"):
            fixed = fixed[:-3]
        return fixed.strip()

    @staticmethod
    def _escape_control_chars_in_strings(text: str) -> str:
        r"""只转义 JSON 字符串字面量**内部**的裸控制字符。

        旧实现对整个响应做 `replace("\n", "\\n")`，把对象/数组之间的结构性
        换行也转义成字面 `\n`，导致原本合法的多行 JSON 反而被改坏
        （merge_memories 因此必然抛「合并结果 JSON 解析失败」）。
        这里改用状态机扫描：跟踪是否处于字符串字面量内、当前字符是否被反斜杠
        转义，仅在字符串内部把裸控制字符转成合法转义序列，字符串外的排版空白
        原样保留。
        """
        out: list[str] = []
        simple = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}
        in_string = False
        escaped = False
        for ch in text:
            if not in_string:
                if ch == '"':
                    in_string = True
                out.append(ch)
                continue
            if escaped:
                out.append(ch)
                escaped = False
            elif ch == "\\":
                out.append(ch)
                escaped = True
            elif ch == '"':
                out.append(ch)
                in_string = False
            elif ch in simple:
                out.append(simple[ch])
            elif ch < " ":
                # 其余 C0 控制字符用 \uXXXX 表示，JSON 规范不允许裸出现
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        return "".join(out)

    @staticmethod
    def _strip_trailing_commas(text: str) -> str:
        """移除 `,` 与 `}`/`]` 之间的尾随逗号（跳过字符串字面量内部）。"""
        out: list[str] = []
        in_string = False
        escaped = False
        length = len(text)
        i = 0
        while i < length:
            ch = text[i]
            if in_string:
                out.append(ch)
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                i += 1
                continue
            if ch == '"':
                in_string = True
                out.append(ch)
                i += 1
                continue
            if ch == ",":
                j = i + 1
                while j < length and text[j].isspace():
                    j += 1
                if j < length and text[j] in "}]":
                    # 丢弃逗号，保留原有空白排版
                    out.append(text[i + 1 : j])
                    i = j
                    continue
            out.append(ch)
            i += 1
        return "".join(out)

    def _close_unbalanced(self, text: str) -> str:
        """补齐被截断的引号/数组/对象，并移除尾随逗号。"""
        fixed = text

        # 修复未闭合的字符串（截断的 JSON）
        open_quotes = fixed.count('"') - fixed.count('\\"')
        if open_quotes % 2 != 0:
            fixed += '"'

        # 修复未闭合的数组
        open_brackets = fixed.count("[") - fixed.count("]")
        if open_brackets > 0:
            fixed += "]" * open_brackets

        # 修复未闭合的对象
        open_braces = fixed.count("{") - fixed.count("}")
        if open_braces > 0:
            fixed += "}" * open_braces

        # 移除尾部逗号（JSON 不允许）
        return self._strip_trailing_commas(fixed)

    @staticmethod
    def _iter_balanced_spans(text: str, max_starts: int = 20):
        """产出括号配对完整的子串，用于剥离 JSON 前后的自然语言。

        从每个 `{` / `[` 起做「跳过字符串字面量」的深度匹配。起点数量设上限，
        避免超长文本上退化成 O(n^2)。
        """
        starts = [i for i, ch in enumerate(text) if ch in "{["][:max_starts]
        for start in starts:
            depth = 0
            in_string = False
            escaped = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
                    if depth == 0:
                        yield text[start : i + 1]
                        break
                    if depth < 0:
                        break

    @staticmethod
    def _try_python_literal(text: str) -> str | None:
        """把 Python 风格字面量（如单引号 dict）转成标准 JSON；失败返回 None。"""
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return None
        if not isinstance(value, (dict, list)):
            return None
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return None

    def _try_fix_json(self, text: str) -> str:
        """
        尝试修复损坏的 JSON 字符串

        修复顺序：已合法则原样返回 -> 去 Markdown 围栏 -> 补齐截断并去尾随逗号、
        转义字符串内裸控制字符 -> 剥离前后自然语言 -> Python 字面量兜底。
        每一步得到合法 JSON 就立即返回，保证「本来能解析的输入不会被改坏」。

        Args:
            text: 可能损坏的 JSON 字符串

        Returns:
            修复后的 JSON 字符串（完全无法修复时返回尽力修复的结果）
        """
        fixed = text.strip() if isinstance(text, str) else str(text or "").strip()
        if self._is_valid_json(fixed):
            return fixed

        # 移除 markdown 代码块标记
        fixed = self._strip_code_fence(fixed)
        if self._is_valid_json(fixed):
            return fixed

        repaired = self._escape_control_chars_in_strings(self._close_unbalanced(fixed))
        if self._is_valid_json(repaired):
            return repaired

        # 剥离 LLM 在 JSON 前后附带的自然语言。必须放在「补齐截断」之后，否则
        # '{"outer": {"inner": 1}' 会只捞出内层对象而丢掉外层结构。
        for span in self._iter_balanced_spans(fixed):
            if self._is_valid_json(span):
                return span
            span_fixed = self._escape_control_chars_in_strings(
                self._close_unbalanced(span)
            )
            if self._is_valid_json(span_fixed):
                return span_fixed

        # 单引号等 Python 风格字面量兜底
        literal = self._try_python_literal(repaired) or self._try_python_literal(fixed)
        return literal if literal is not None else repaired

    async def process_conversation(
        self,
        messages: list[Message],
        is_group_chat: bool = False,
        persona_id: str | None = None,
    ) -> tuple[str, dict[str, Any], float]:
        """
        处理对话历史,生成结构化记忆

        Args:
            messages: 消息列表(Message对象)
            is_group_chat: 是否为群聊
            persona_id: 人格ID,用于获取人格提示词

        Returns:
            tuple: (content, metadata, importance)
                - content: 格式化的记忆内容字符串
                - metadata: 包含结构化信息的字典
                - importance: 重要性评分(0-1)

        Raises:
            Exception: 处理失败时抛出异常
        """
        if not messages:
            raise ValueError("消息列表不能为空")

        # 1. 格式化对话历史
        conversation_text = self._format_conversation(messages)

        # 2. 选择合适的提示词模板（每次从 PromptManager 读取，确保 WebUI 保存后立即生效）
        # 使用 replace 而非 format，避免对话内容中的大括号导致解析错误
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt = self._get_chat_prompt(is_group_chat).replace(
            "{conversation}", conversation_text
        ).replace("{current_date}", current_date)

        # 3. 调用LLM生成结构化记忆
        conversation_type = "群聊" if is_group_chat else "私聊"
        try:
            logger.debug(
                f"[MemoryProcessor] 准备调用 LLM，对话类型={conversation_type}, 消息数={len(messages)}"
            )
            logger.debug(f"[MemoryProcessor] Prompt 模板长度={len(prompt)}")
            logger.debug(
                f"[MemoryProcessor] 发送给LLM的对话内容（前500字符）:\n{conversation_text[:500]}"
            )

            # 构建 system_prompt，嵌入人格提示
            system_prompt = await self._build_system_prompt_with_persona(persona_id)
            logger.debug(f"[MemoryProcessor] System Prompt: {system_prompt[:200]}...")

            llm_response_text = await self._call_llm_with_retry(
                prompt=prompt,
                system_prompt=system_prompt,
            )

            logger.debug(
                f"[MemoryProcessor]  LLM 响应成功，响应长度={len(llm_response_text)}"
            )
            logger.debug(f"[MemoryProcessor] LLM 原始响应内容:\n{llm_response_text}")

            # 4. 解析LLM响应
            structured_data = self._parse_llm_response(llm_response_text, is_group_chat)

            # 4.5 质量校验
            quality = self._validate_summary_quality(structured_data)
            if quality == "low":
                logger.warning(
                    "[MemoryProcessor] 总结质量不达标（low），将标记但仍写入"
                )
            structured_data["_quality"] = quality

            # 5. 构建存储格式
            fallback_excerpt = (
                conversation_text[:200] + "..."
                if len(conversation_text) > 200
                else conversation_text
            )
            content, metadata = self._build_storage_format(
                fallback_excerpt, structured_data, is_group_chat
            )
            metadata["participant_identities"] = self._extract_participant_identities(
                messages
            )
            content = self._apply_source_time_tags(content, metadata, messages)
            # 将质量标记写入 metadata
            metadata["summary_quality"] = structured_data.get("_quality", "normal")

            importance = float(structured_data.get("importance", 0.5))

            logger.debug(
                f"[MemoryProcessor]  成功生成结构化记忆: 摘要={structured_data.get('summary', '')[:50]}..., "
                f"主题={structured_data.get('topics', [])}, "
                f"重要性={importance}, 类型={conversation_type}"
            )
            logger.debug(
                f"[MemoryProcessor] 生成的记忆内容（前200字符）:\n{content[:200]}"
            )

            return content, metadata, importance

        except Exception as e:
            logger.error(f"[MemoryProcessor] 处理对话历史失败: {e}", exc_info=True)
            # 不再降级处理，直接向上抛出异常，由调用方处理重试逻辑
            raise

    def _apply_source_time_tags(
        self,
        content: str,
        metadata: dict[str, Any],
        messages: list[Message],
    ) -> str:
        """Attach source dates without asking the LLM to infer them."""
        if not self.config.get("include_source_time_tags", True) or not messages:
            return content

        timestamps = sorted(float(message.timestamp) for message in messages)
        start = datetime.fromtimestamp(timestamps[0]).astimezone()
        end = datetime.fromtimestamp(timestamps[-1]).astimezone()
        dates = sorted(
            {
                datetime.fromtimestamp(value).strftime("%Y-%m-%d")
                for value in timestamps
            }
        )
        label = dates[0] if len(dates) == 1 else f"{dates[0]} - {dates[-1]}"

        metadata["time_tags"] = dates
        metadata["source_time_start"] = start.isoformat()
        metadata["source_time_end"] = end.isoformat()
        metadata["source_time_label"] = label
        return content

    def _format_conversation(self, messages: list[Message]) -> str:
        """
        格式化对话历史为文本

        Args:
            messages: 消息列表(Message对象)

        Returns:
            格式化后的对话文本
        """

        formatted_lines = []
        for i, msg in enumerate(messages):
            logger.debug(
                f"[_format_conversation] 消息#{i}: "
                f"sender_id={msg.sender_id}, sender_name={msg.sender_name}, "
                f"role={msg.role}, group_id={msg.group_id}"
            )

            content_text = self._message_content_to_text(msg.content)
            sender_info = self._format_sender_info(msg)
            formatted_line = f"{sender_info} {content_text}".rstrip()
            formatted_lines.append(formatted_line)
            if msg.group_id:
                logger.debug(
                    f"[_format_conversation] 消息#{i} 格式化结果(群聊): {formatted_line[:100]}..."
                )
            else:
                logger.debug(
                    f"[_format_conversation] 消息#{i} 格式化结果(私聊): {sender_info[:50]}..."
                )
        return "\n".join(formatted_lines)

    @staticmethod
    def _extract_participant_identities(
        messages: list[Message],
    ) -> list[dict[str, Any]]:
        """Build stable graph identities from message sender IDs, not LLM names."""
        identities: dict[str, dict[str, Any]] = {}
        for message in messages:
            if message.role == "system":
                continue
            sender_id = str(message.sender_id or "").strip()
            if not sender_id:
                continue
            platform = str(message.platform or "unknown").strip().lower() or "unknown"
            identity_key = f"{platform}:{sender_id}"
            display_name = str(message.sender_name or sender_id).strip() or sender_id
            is_bot = bool(
                message.metadata.get("is_bot_message", False)
                or message.role == "assistant"
            )

            identity = identities.setdefault(
                identity_key,
                {
                    "identity_key": identity_key,
                    "sender_id": sender_id,
                    "platform": platform,
                    "display_name": display_name,
                    "aliases": [],
                    "is_bot": is_bot,
                },
            )
            identity["display_name"] = display_name
            identity["is_bot"] = bool(identity["is_bot"] or is_bot)
            if display_name not in identity["aliases"]:
                identity["aliases"].append(display_name)

        return list(identities.values())

    @staticmethod
    def _format_sender_info(msg: Message) -> str:
        time_str = datetime.fromtimestamp(msg.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        display_name = msg.sender_name if msg.sender_name else msg.sender_id or "未知"
        is_bot = msg.metadata.get("is_bot_message", False) or msg.role == "assistant"
        if is_bot:
            return f"[Bot: {display_name} | ID: {msg.sender_id} | {time_str}]"
        return f"[{display_name} | ID: {msg.sender_id} | {time_str}]"

    @classmethod
    def _message_content_to_text(cls, content: Any) -> str:
        return Message.content_to_text(content)

    @classmethod
    def _message_part_to_text(cls, part: Any) -> tuple[str, bool]:
        return Message._content_part_to_text(part)
