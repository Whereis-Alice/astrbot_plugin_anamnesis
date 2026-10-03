"""Security and behavior tests for the Agent memory deletion tool."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.tools.memory_forget_tool import MemoryForgetTool


def _run_context(*, sender_id: str = "user-1", origin: str = "test:private:session-1"):
    event = Mock()
    event.unified_msg_origin = origin
    event.get_platform_name.return_value = "test"
    event.get_sender_id.return_value = sender_id
    event.get_sender_name.return_value = sender_id
    context = Mock()
    context.context.event = event
    return context


def _tool(
    metadata: dict | None = None,
    *,
    config: dict | None = None,
    profile_manager=None,
):
    engine = Mock()
    engine.get_memory = AsyncMock(
        return_value={
            "id": 7,
            "text": "记住这件事",
            "metadata": metadata
            or {
                "session_id": "test:private:session-1",
                "source_session_id": "test:private:session-1",
                "persona_id": "persona-a",
            },
        }
    )
    engine.delete_memory = AsyncMock(return_value=True)
    tool = MemoryForgetTool(
        context=Mock(),
        config_manager=ConfigManager(
            config or {"filtering_settings": {"use_persona_filtering": False}}
        ),
        memory_engine=engine,
        user_profile_manager=profile_manager,
    )
    return tool, engine


@pytest.mark.asyncio
async def test_forget_tool_deletes_memory_in_current_scope_and_cleans_profile():
    profile_manager = Mock()
    profile_manager.delete_by_source_memory = AsyncMock(return_value=1)
    tool, engine = _tool(profile_manager=profile_manager)

    result = json.loads(
        await tool.call(_run_context(), memory_id=7, reason="用户要求遗忘")
    )

    assert result == {"deleted": True, "id": 7}
    engine.get_memory.assert_awaited_once_with(7)
    engine.delete_memory.assert_awaited_once_with(7)
    profile_manager.delete_by_source_memory.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_forget_tool_rejects_other_session_even_when_id_exists():
    tool, engine = _tool(
        metadata={
            "session_id": "test:private:other-session",
            "source_session_id": "test:private:other-session",
            "persona_id": "persona-a",
        }
    )

    result = json.loads(await tool.call(_run_context(), memory_id=7, reason="删除"))

    assert result["deleted"] is False
    assert result["error"] == "memory is not available in current scope"
    engine.delete_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_forget_tool_user_scope_allows_same_user_across_sessions():
    tool, engine = _tool(
        metadata={
            "session_id": "livingmemory:user:test:user-1",
            "source_session_id": "test:private:older-session",
            "persona_id": "persona-a",
        },
        config={
            "filtering_settings": {
                "memory_scope_mode": "user",
                "use_persona_filtering": False,
            }
        },
    )

    result = json.loads(await tool.call(_run_context(), memory_id=7, reason="删除"))

    assert result == {"deleted": True, "id": 7}
    engine.delete_memory.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_forget_tool_rejects_other_persona_when_filtering_enabled():
    tool, engine = _tool(config={"filtering_settings": {"use_persona_filtering": True}})
    with patch(
        "astrbot_plugin_anamnesis.core.tools.memory_forget_tool.get_persona_id",
        new_callable=AsyncMock,
        return_value="persona-b",
    ):
        result = json.loads(await tool.call(_run_context(), memory_id=7, reason="删除"))

    assert result["error"] == "memory is not available in current scope"
    engine.delete_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_forget_tool_rejects_unlisted_sender_before_loading_memory():
    tool, engine = _tool(
        config={
            "access_control": {
                "whitelist_enabled": True,
                "allowed_ids": "another-user",
            },
            "filtering_settings": {"use_persona_filtering": False},
        }
    )

    result = json.loads(await tool.call(_run_context(), memory_id=7, reason="删除"))

    assert result["error"] == "memory access is not allowed"
    engine.get_memory.assert_not_awaited()
    engine.delete_memory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_id", [0, -1, True, "7", None])
async def test_forget_tool_rejects_invalid_id(memory_id):
    tool, engine = _tool()

    result = json.loads(
        await tool.call(_run_context(), memory_id=memory_id, reason="删除")
    )

    assert result["error"] == "invalid_memory_id"
    engine.get_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_forget_tool_requires_reason():
    tool, engine = _tool()

    result = json.loads(await tool.call(_run_context(), memory_id=7))

    assert result["error"] == "reason is required"
    engine.get_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_forget_tool_missing_memory_does_not_delete():
    tool, engine = _tool()
    engine.get_memory.return_value = None

    result = json.loads(await tool.call(_run_context(), memory_id=7, reason="删除"))

    assert result["error"] == "memory is not available in current scope"
    engine.delete_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_forget_tool_failed_engine_delete_does_not_clean_profile():
    profile_manager = Mock()
    profile_manager.delete_by_source_memory = AsyncMock()
    tool, engine = _tool(profile_manager=profile_manager)
    engine.delete_memory.return_value = False

    result = json.loads(await tool.call(_run_context(), memory_id=7, reason="删除"))

    assert result == {"deleted": False, "id": 7, "error": "delete_failed"}
    profile_manager.delete_by_source_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_forget_tool_global_scope_requires_source_session_match():
    metadata = {
        "session_id": "livingmemory:global",
        "source_session_id": "test:private:another-session",
        "persona_id": "persona-a",
    }
    tool, engine = _tool(
        metadata=metadata,
        config={
            "filtering_settings": {
                "memory_scope_mode": "global",
                "use_persona_filtering": False,
            }
        },
    )

    denied = json.loads(await tool.call(_run_context(), memory_id=7, reason="删除"))
    assert denied["error"] == "memory is not available in current scope"
    engine.delete_memory.assert_not_awaited()

    metadata["source_session_id"] = "test:private:session-1"
    allowed = json.loads(await tool.call(_run_context(), memory_id=7, reason="删除"))
    assert allowed["deleted"] is True


@pytest.mark.asyncio
async def test_forget_tool_unfiltered_legacy_scope_requires_source_session_match():
    tool, engine = _tool(
        metadata={
            "session_id": "test:private:another-session",
            "source_session_id": "test:private:another-session",
        },
        config={
            "filtering_settings": {
                "memory_scope_mode": "legacy",
                "use_session_filtering": False,
                "use_persona_filtering": False,
            }
        },
    )

    result = json.loads(await tool.call(_run_context(), memory_id=7, reason="删除"))

    assert result["error"] == "memory is not available in current scope"
    engine.delete_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_forget_tool_does_not_leak_internal_exception():
    tool, engine = _tool()
    engine.delete_memory.side_effect = RuntimeError("secret path /var/private/db")

    result = json.loads(await tool.call(_run_context(), memory_id=7, reason="删除"))

    assert result == {"deleted": False, "error": "internal_error"}
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_forget_tool_reports_profile_cleanup_failure_after_deletion():
    profile_manager = Mock()
    profile_manager.delete_by_source_memory = AsyncMock(
        side_effect=RuntimeError("profile db unavailable")
    )
    tool, engine = _tool(profile_manager=profile_manager)

    result = json.loads(await tool.call(_run_context(), memory_id=7, reason="删除"))

    assert result == {"deleted": True, "id": 7, "profile_cleanup": "failed"}
    engine.delete_memory.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_forget_tool_propagates_cancellation():
    tool, engine = _tool()
    engine.get_memory.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await tool.call(_run_context(), memory_id=7, reason="删除")
