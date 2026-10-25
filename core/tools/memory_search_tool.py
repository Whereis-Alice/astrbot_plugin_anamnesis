"""供 Agent 主动调用的长期记忆回忆工具。"""

import asyncio
import json
from dataclasses import field
from typing import Any

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic.dataclasses import dataclass

from ..base.config_manager import ConfigManager
from ..memory_scope import is_event_memory_allowed, resolve_memory_scope
from ..retrieval.temporal_query import resolve_context_timezone
from ..retrieval.temporal_retrieval import retrieval_time_bounds, search_with_time
from ..utils import get_persona_id
from .agentic_recall import agentic_search, evidence_allowed, scoped_source
from .tool_access import ensure_tool_access


def _json_result(data: dict[str, Any]) -> str:
    """将工具结果稳定序列化为 JSON 文本。"""
    return json.dumps(data, ensure_ascii=False, default=str)


@dataclass
class MemorySearchTool(FunctionTool[AstrAgentContext]):
    """长期记忆主动回忆工具。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: ConfigManager | None = None
    memory_engine: Any = None

    name: str = "anamnesis_recall_memory"
    description: str = (
        "Recall long-term memory when the current context is insufficient. "
        "Use concise, focused recall keywords instead of copying the full user message. "
        "Call this when the user asks you to recall prior facts, preferences, agreements, or older context, "
        "or when resolving ambiguous references requires checking memory. "
        "Prefer short topic phrases, named entities, preferences, commitments, or past events as recall keywords. "
        "If the first recall is not enough, refine the keywords and recall again. "
        "For dates such as yesterday or last week, use auto/time/hybrid mode. "
        "An explicit time range uses inclusive start_time and exclusive end_time; "
        "query may be empty in time mode with a provided range."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Concise recall keywords for long-term memory. Prefer key entities, topics, preferences, commitments, or past events instead of copying the full user message.",
                },
                "k": {
                    "type": "integer",
                    "description": "Maximum number of memory items to return for one recall. Keep this small unless more evidence is needed.",
                    "default": 5,
                },
                "include_source": {
                    "type": "boolean",
                    "description": "Include retained original messages when exact details are needed. Use only after normal recall is insufficient.",
                    "default": False,
                },
                "mode": {"type": "string", "enum": ["auto", "semantic", "time", "hybrid"],
                         "default": "auto", "description": "Time mode directly queries a resolved time range; hybrid combines time and semantic candidates."},
                "start_time": {"type": "string", "description": "Inclusive time boundary: epoch seconds or ISO-8601."},
                "end_time": {"type": "string", "description": "Exclusive time boundary: epoch seconds or ISO-8601. Use the next midnight to include a full day."},
                "speaker": {"type": "string", "description": "Exact speaker account ID or recorded participant name."},
                "exclude_ids": {"type": "array", "items": {"type": "integer"}, "description": "Memory IDs already seen, at most 500."},
            },
            "required": [],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        query: str = "",
        k: int = 5,
        include_source: bool = False,
        speaker: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        exclude_ids: list[int] | None = None,
        cursor: str | None = None,
        mode: str = "auto",
    ) -> ToolExecResult:
        """执行长期记忆回忆。"""
        cleaned_query = (query or "").strip()
        if not cleaned_query and start_time is None and end_time is None:
            return _json_result(
                {
                    "query": "",
                    "count": 0,
                    "results": [],
                    "error": "query is empty",
                }
            )

        if (
            self.config_manager is None
            or self.memory_engine is None
            or self.context is None
        ):
            return _json_result(
                {
                    "query": cleaned_query,
                    "count": 0,
                    "results": [],
                    "error": "memory search tool is not initialized",
                }
            )

        try:
            event = context.context.event
            await ensure_tool_access(self, event, "enable_recall_tool", default=True)
            if self.config_manager.get("agent_tools.agentic_recall_enabled", False):
                return await agentic_search(self, event, cleaned_query, k, include_source,
                                            speaker=speaker, start_time=start_time, end_time=end_time,
                                            exclude_ids=exclude_ids, cursor=cursor, mode=mode)
            if not is_event_memory_allowed(self.config_manager, event):
                return _json_result(
                    {
                        "query": cleaned_query,
                        "count": 0,
                        "results": [],
                        "error": "memory access is not allowed",
                    }
                )
            filtering_config = self.config_manager.filtering_settings
            use_persona_filtering = filtering_config.get("use_persona_filtering", True)

            persona_id = (
                await get_persona_id(self.context, event)
                if use_persona_filtering
                else None
            )

            recall_session_id = resolve_memory_scope(self.config_manager, event)
            recall_persona_id = persona_id if use_persona_filtering else None

            default_k = int(self.config_manager.get("recall_engine.top_k", 5))
            max_k = int(self.config_manager.get("recall_engine.max_k", 10))
            requested_k = default_k if k is None else k
            try:
                requested_k_int = int(requested_k)
            except (TypeError, ValueError):
                requested_k_int = default_k

            limited_k = max(1, min(requested_k_int, max_k))

            memories, retrieval_details = await search_with_time(
                self.memory_engine, query=cleaned_query,
                k=limited_k,
                session_id=recall_session_id,
                persona_id=recall_persona_id,
                config_manager=self.config_manager, mode=mode,
                start_time=start_time, end_time=end_time, speaker=speaker,
                exclude_ids=exclude_ids,
                timezone_name=resolve_context_timezone(self.context, self.config_manager,
                                                      getattr(event, "unified_msg_origin", None)),
                timeout=self.config_manager.get("recall_engine.search_timeout_seconds", 5),
            )

            serialized_results = []
            for memory in memories:
                metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
                item = {
                    "id": memory.doc_id,
                    "content": memory.content,
                    "score": memory.final_score,
                    "importance": metadata.get("importance"),
                    "session_id": metadata.get("session_id"),
                    "persona_id": metadata.get("persona_id"),
                    "create_time": metadata.get("create_time"),
                    "last_access_time": metadata.get("last_access_time"),
                }
                breakdown = getattr(memory, "score_breakdown", None)
                if isinstance(breakdown, dict):
                    item["score_breakdown"] = breakdown
                if retrieval_details.get("time_range"):
                    start, end, basis = retrieval_time_bounds(metadata)
                    item.update(time_basis=basis, time_start=start, time_end=end)
                if (include_source and metadata.get("has_source") and
                    evidence_allowed(metadata, recall_session_id, recall_persona_id, event, self.config_manager)):
                    get_source = getattr(
                        self.memory_engine, "get_memory_source", None
                    )
                    if callable(get_source):
                        source = await scoped_source(self, event, memory.doc_id, recall_session_id, recall_persona_id)
                        if source is not None:
                            item["source_messages"] = source
                serialized_results.append(item)

            return _json_result(
                {
                    "query": cleaned_query,
                    "applied_filters": {
                        "session_filtered": recall_session_id is not None,
                        "persona_filtered": use_persona_filtering,
                    },
                    "count": len(serialized_results),
                    "results": serialized_results,
                    "retrieval": retrieval_details,
                }
            )
        except asyncio.CancelledError:
            raise
        except ValueError as error:
            return _json_result({"query": cleaned_query, "count": 0, "results": [], "error": str(error)})
        except TimeoutError:
            return _json_result({"query": cleaned_query, "count": 0, "results": [], "error": "recall_time_budget_exhausted"})
        except Exception as e:
            logger.error(f"记忆工具检索失败: {e}", exc_info=True)
            return _json_result(
                {
                    "query": cleaned_query,
                    "count": 0,
                    "results": [],
                    "error": "internal_error",
                }
            )
