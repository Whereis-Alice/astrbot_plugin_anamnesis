"""供 Agent 删除当前记忆作用域中指定长期记忆的工具。"""

import asyncio
import json
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..memory_scope import (
    GLOBAL_MEMORY_SCOPE,
    is_event_memory_allowed,
    resolve_memory_scope,
)
from ..utils import get_persona_id


def _json_result(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


@dataclass
class MemoryForgetTool(FunctionTool[AstrAgentContext]):
    """仅按已核实的记忆 ID 删除当前作用域中的记忆。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None
    user_profile_manager: Any = None

    name: str = "anamnesis_forget_memory"
    description: str = (
        "Delete one long-term memory only when the user explicitly asks to forget "
        "or correct that specific fact. First use anamnesis_recall_memory to obtain "
        "its exact ID; never guess an ID or delete merely because a memory seems "
        "irrelevant. This operation is irreversible."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "description": (
                        "Exact positive memory ID returned by anamnesis_recall_memory. "
                        "Do not infer or guess an ID."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Short reason based on the user's explicit request to forget or correct this memory.",
                },
            },
            "required": ["memory_id", "reason"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        memory_id: int,
        reason: str = "",
    ) -> ToolExecResult:
        """校验记忆归属后删除指定 ID。"""
        if (
            isinstance(memory_id, bool)
            or not isinstance(memory_id, int)
            or memory_id <= 0
        ):
            return _json_result({"deleted": False, "error": "invalid_memory_id"})
        if not isinstance(reason, str) or not reason.strip():
            return _json_result({"deleted": False, "error": "reason is required"})
        if (
            self.context is None
            or self.config_manager is None
            or self.memory_engine is None
        ):
            return _json_result({"deleted": False, "error": "tool is not initialized"})

        try:
            event = context.context.event
            if not is_event_memory_allowed(self.config_manager, event):
                return _json_result(
                    {"deleted": False, "error": "memory access is not allowed"}
                )

            memory = await self.memory_engine.get_memory(memory_id)
            metadata = memory.get("metadata") if isinstance(memory, dict) else None
            if not isinstance(metadata, dict):
                return _json_result(
                    {
                        "deleted": False,
                        "error": "memory is not available in current scope",
                    }
                )

            scope = resolve_memory_scope(self.config_manager, event)
            origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
            stored_scope = metadata.get("session_id")
            source_session = metadata.get("source_session_id")
            if scope is None:
                # Unfiltered legacy retrieval does not authorize cross-session deletion.
                scope_matches = bool(origin and source_session == origin)
            elif scope == GLOBAL_MEMORY_SCOPE:
                # Global recall can see everyone else's memories. Require the
                # original session as an additional ownership check.
                scope_matches = bool(
                    origin and stored_scope == scope and source_session == origin
                )
            else:
                scope_matches = stored_scope == scope
            if not scope_matches:
                return _json_result(
                    {
                        "deleted": False,
                        "error": "memory is not available in current scope",
                    }
                )

            use_persona_filtering = self.config_manager.filtering_settings.get(
                "use_persona_filtering", True
            )
            if use_persona_filtering:
                persona_id = await get_persona_id(self.context, event)
                if metadata.get("persona_id") != persona_id:
                    return _json_result(
                        {
                            "deleted": False,
                            "error": "memory is not available in current scope",
                        }
                    )

            if not await self.memory_engine.delete_memory(memory_id):
                return _json_result(
                    {"deleted": False, "id": memory_id, "error": "delete_failed"}
                )

            result: dict[str, Any] = {"deleted": True, "id": memory_id}
            cleanup = getattr(
                self.user_profile_manager, "delete_by_source_memory", None
            )
            if callable(cleanup):
                try:
                    await cleanup(memory_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "已删除记忆，但清理对应用户档案项失败 (memory_id=%s)",
                        memory_id,
                        exc_info=True,
                    )
                    result["profile_cleanup"] = "failed"
            return _json_result(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Agent 删除记忆失败 (memory_id=%s)", memory_id, exc_info=True)
            return _json_result({"deleted": False, "error": "internal_error"})
