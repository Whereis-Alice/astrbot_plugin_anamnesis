"""供 Agent 删除当前记忆作用域中指定长期记忆的工具。"""

import asyncio
import inspect
import json
import time
from dataclasses import field
from typing import Any

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic.dataclasses import dataclass

from ..managers.memory_mutation import memory_mutation_guard
from ..memory_scope import (
    GLOBAL_MEMORY_SCOPE,
    resolve_memory_scope,
)
from ..recall_persona import persona_matches
from ..utils import get_persona_id
from .agentic_recall import request_state
from .tool_access import ensure_tool_access


def _json_result(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


async def _owned_metadata(tool, event, memory_id: int, scope, origin: str):
    memory = await tool.memory_engine.get_memory(memory_id)
    metadata = memory.get("metadata") if isinstance(memory, dict) else None
    if not isinstance(metadata, dict):
        return None
    if scope is None:
        matches = bool(origin and metadata.get("source_session_id") == origin)
    elif scope == GLOBAL_MEMORY_SCOPE:
        matches = bool(origin and metadata.get("session_id") == scope and metadata.get("source_session_id") == origin)
    else:
        matches = metadata.get("session_id") == scope
    if tool.config_manager.filtering_settings.get("use_persona_filtering", True):
        matches = matches and persona_matches(
            metadata.get("persona_id"), await get_persona_id(tool.context, event)
        )
    return metadata if matches else None


@dataclass
class MemoryForgetTool(FunctionTool[AstrAgentContext]):
    """仅按已核实的记忆 ID 删除当前作用域中的记忆。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None
    user_profile_manager: Any = None
    _forget_times: dict = field(default_factory=dict)
    _forget_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    name: str = "anamnesis_forget_memory"
    description: str = (
        "Delete one long-term memory only when the user explicitly asks to forget "
        "or correct that specific fact. First use anamnesis_recall_memory to obtain "
        "its exact ID; never guess an ID or delete merely because a memory seems "
        "irrelevant. The configured mode permanently deletes or reversibly archives the memory. "
        "Use action=restore for a known archived ID when the user asks to restore it."
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
                "action": {"type": "string", "enum": ["forget", "restore"], "default": "forget"},
            },
            "required": ["memory_id", "reason"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        memory_id: int,
        reason: str = "",
        action: str = "forget",
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
            await ensure_tool_access(self, event, "enable_forget_tool")
            scope = resolve_memory_scope(self.config_manager, event)
            origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
            metadata = await _owned_metadata(self, event, memory_id, scope, origin)
            if metadata is None:
                return _json_result(
                    {
                        "deleted": False,
                        "error": "memory is not available in current scope",
                    }
                )

            if action not in ("forget", "restore"):
                return _json_result({"deleted": False, "error": "invalid_action"})
            operation_id = None
            actor = "tool:"+origin+":"+str(event.get_sender_id())
            state = request_state(event)
            # The event lock protects the per-request quota; the tool lock
            # makes cooldown checking and accounting atomic across events.
            async with state["lock"], self._forget_lock, memory_mutation_guard(self.memory_engine):
                # A waiting tool must not mutate a document whose owner or
                # persona changed since its initial eligibility check.
                await ensure_tool_access(self, event, "enable_forget_tool")
                scope = resolve_memory_scope(self.config_manager, event)
                metadata = await _owned_metadata(self, event, memory_id, scope, origin)
                if metadata is None:
                    return _json_result({"deleted": False, "error": "memory is not available in current scope"})
                mode = self.config_manager.get("agent_tools.forget_mode", "delete")
                if state.get("forget_count", 0) >= int(self.config_manager.get("agent_tools.forget_max_per_request", 3)):
                    return _json_result({"deleted": False, "error": "forget_limit_reached"})
                key = scope or origin
                now = time.monotonic()
                if now - self._forget_times.get(key, -1e9) < float(self.config_manager.get("agent_tools.forget_cooldown_seconds", 2)):
                    return _json_result({"deleted": False, "error": "forget_cooldown"})
                if action == "restore":
                    if metadata.get("status") != "archived" and not metadata.get("restored_at"):
                        return _json_result({"restored": False, "error": "memory_is_not_archived"})
                    if metadata.get("recovery_operation_id"):
                        from ..managers.memory_administration import (
                            MemoryAdministration,
                        )
                        administration = getattr(self.memory_engine, "memory_administration", None)
                        if not isinstance(administration, MemoryAdministration):
                            administration = MemoryAdministration(self.memory_engine, self.config_manager)
                            self.memory_engine.memory_administration = administration
                        recovered = await administration.restore(metadata["recovery_operation_id"], actor=actor)
                        success = memory_id in recovered["ids"]
                    else:
                        success = await self.memory_engine.restore_memory(memory_id)
                elif mode == "recoverable":
                    from ..managers.memory_administration import MemoryAdministration
                    administration = getattr(self.memory_engine, "memory_administration", None)
                    if not isinstance(administration, MemoryAdministration):
                        administration = MemoryAdministration(self.memory_engine, self.config_manager)
                        self.memory_engine.memory_administration = administration
                    deleted = await administration.delete([memory_id], actor=actor, reason=reason)
                    success = memory_id in deleted["deleted_ids"]
                    operation_id = deleted["operation_id"]
                elif mode == "archive":
                    success = bool(await self.memory_engine.archive_memories([memory_id]))
                else:
                    success = await self.memory_engine.delete_memory(memory_id)
                audit = getattr(self.memory_engine, "record_forget_audit", None)
                if inspect.iscoroutinefunction(audit):
                    try:
                        await audit(memory_id=memory_id, scope=key, mode="restore" if action == "restore" else mode,
                                    reason=reason.strip(), success=success)
                    except Exception:
                        logger.warning("遗忘操作审计记录失败 (memory_id=%s)", memory_id, exc_info=True)
                if not success:
                    return _json_result({"deleted": False, "id": memory_id,
                                         "error": "delete_failed" if mode == "delete" and action == "forget" else "operation_failed"})
                state["forget_count"] = state.get("forget_count", 0) + 1
                if len(self._forget_times) >= 1024:
                    self._forget_times.clear()
                self._forget_times[key] = now

            if action == "restore":
                return _json_result({"restored": True, "id": memory_id})
            if mode in ("archive", "recoverable"):
                return _json_result({"archived": True, "deleted": False, "recoverable": True, "id": memory_id,
                                     "operation_id": operation_id})

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
        except ValueError as error:
            return _json_result({"deleted": False, "error": str(error)})
        except Exception:
            logger.error("Agent 删除记忆失败 (memory_id=%s)", memory_id, exc_info=True)
            return _json_result({"deleted": False, "error": "internal_error"})
