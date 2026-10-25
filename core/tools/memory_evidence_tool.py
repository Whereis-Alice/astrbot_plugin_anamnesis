"""Read retained source evidence within the same scoped agentic budget."""

from dataclasses import field
from typing import Any

from astrbot.core.agent.tool import FunctionTool
from pydantic.dataclasses import dataclass

from .agentic_recall import agentic_evidence


@dataclass
class MemoryEvidenceTool(FunctionTool):
    __pydantic_config__ = {"arbitrary_types_allowed": True}
    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None
    name: str = "anamnesis_read_memory_evidence"
    description: str = "Read historical source messages for an exact recalled memory ID. Sources may include superseded statements; interpret them using the returned live facts. Shares recall call/time/output budgets."
    parameters: dict = field(default_factory=lambda: {
        "type": "object", "properties": {
            "memory_id": {"type": "integer"}, "offset": {"type": "integer", "default": 0},
            "limit": {"type": "integer", "default": 20}}, "required": ["memory_id"]})

    async def call(self, context, memory_id: int, offset: int = 0, limit: int = 20):
        return await agentic_evidence(self, context.context.event, memory_id, offset, limit)
