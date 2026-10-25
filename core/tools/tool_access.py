"""Authorization belongs to each execution, including retained tool references."""

from .. import passive_group_capture
from ..memory_scope import is_event_memory_allowed


async def ensure_tool_access(
    tool, event, feature: str, *, default: bool = False
) -> None:
    if getattr(tool.memory_engine, "_writes_closing", False) is True:
        raise ValueError("memory engine is closing")
    if tool.config_manager is None or not tool.config_manager.get(
        f"agent_tools.{feature}", default
    ):
        raise ValueError("memory tool is disabled")
    if not is_event_memory_allowed(tool.config_manager, event):
        raise ValueError("memory access is not allowed")
    origin = str(getattr(event, "unified_msg_origin", "") or "")
    if not await passive_group_capture.is_session_enabled(
        origin
    ) or not await passive_group_capture.is_plugin_enabled_for_session(origin):
        raise ValueError("memory access is disabled for current session")
