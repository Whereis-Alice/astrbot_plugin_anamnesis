"""Administrator-only correction proposal; execution belongs to the WebUI."""

from __future__ import annotations

import inspect
import json
from dataclasses import field
from typing import Any

from astrbot.core.agent.tool import FunctionTool
from pydantic.dataclasses import dataclass

from ..managers.memory_administration import MemoryAdministration
from ..managers.memory_mutation import memory_mutation_guard
from ..memory_scope import is_event_memory_allowed, resolve_memory_scope
from ..recall_persona import persona_matches
from ..utils import get_persona_id
from .agentic_recall import request_state
from .tool_access import ensure_tool_access


@dataclass
class MemoryCorrectionPlanTool(FunctionTool):
    __pydantic_config__ = {"arbitrary_types_allowed": True}  # noqa: RUF012 -- Pydantic dataclass configuration
    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None
    memory_processor: Any = None
    name: str = "anamnesis_propose_correction"
    description: str = (
        "For an administrator's explicit request, propose correcting one exact recalled memory ID "
        "in the current chat/persona. This only creates a review plan; it never executes changes. "
        "Ask the administrator to review and confirm it in Anamnesis Administration WebUI."
    )
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {
        "memory_id": {"type": "integer", "description": "Exact ID from recall, never guess"},
        "instruction": {"type": "string", "description": "Administrator's requested correction"}},
        "required": ["memory_id", "instruction"]})

    async def call(self, context, memory_id: int, instruction: str) -> str:
        event = context.context.event
        check = getattr(event, "is_admin", None)
        admin = check() if callable(check) else getattr(event, "role", None) == "admin"
        if inspect.isawaitable(admin):
            admin = await admin
        if admin is not True or not is_event_memory_allowed(self.config_manager, event):
            return json.dumps({"error": "administrator_required"})
        try:
            await ensure_tool_access(self, event, "enable_correction_plan_tool")
            service = getattr(self.memory_engine, "memory_administration", None)
            if not isinstance(service, MemoryAdministration):
                service = MemoryAdministration(self.memory_engine, self.config_manager, self.memory_processor)
                self.memory_engine.memory_administration = service
            async with memory_mutation_guard(self.memory_engine):
                memory = await self.memory_engine._replacement_snapshot(memory_id)
                metadata = memory.get("metadata", {}) if memory else {}
                origin = str(event.unified_msg_origin)
                scope = resolve_memory_scope(self.config_manager, event)
                if metadata.get("session_id") != scope or metadata.get("source_session_id") != origin:
                    return json.dumps({"error": "memory_not_in_current_scope"})
                if self.config_manager.filtering_settings.get("use_persona_filtering", True) and not persona_matches(
                    metadata.get("persona_id"), await get_persona_id(self.context, event)
                ):
                    return json.dumps({"error": "memory_not_in_current_persona"})
            state = request_state(event)
            async with state["lock"]:
                if state.get("correction_plan_count", 0) >= 1:
                    return json.dumps({"error": "one_plan_per_request"})
                await ensure_tool_access(self, event, "enable_correction_plan_tool")
                state["correction_plan_count"] = state.get("correction_plan_count", 0)+1
            result = await service.preview_correction(memory_id, instruction,
                actor="tool:"+origin+":"+str(event.get_sender_id()), expected_memory=memory)
            return json.dumps(result, ensure_ascii=False)
        except (ValueError, TypeError, RuntimeError) as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
