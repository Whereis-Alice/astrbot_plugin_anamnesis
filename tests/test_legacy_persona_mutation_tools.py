"""Real mutation tools keep legacy null personas within their original scope."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core import passive_group_capture, utils
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.memory_scope import GLOBAL_MEMORY_SCOPE
from astrbot_plugin_anamnesis.core.tools.memory_correction_plan_tool import (
    MemoryCorrectionPlanTool,
)
from astrbot_plugin_anamnesis.core.tools.memory_forget_tool import MemoryForgetTool

from tests.test_memory_transfer_runtime_safety import _engine

ORIGIN = "qq:GroupMessage:1"


@pytest_asyncio.fixture
async def tool_context(tmp_path, monkeypatch):
    engine = await _engine(tmp_path / "tool-no-persona.db")
    engine.atom_enabled = False
    monkeypatch.setattr(
        passive_group_capture, "is_session_enabled", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        passive_group_capture,
        "is_plugin_enabled_for_session",
        AsyncMock(return_value=True),
    )
    persona_config = {"persona_id": "[%None]"}
    monkeypatch.setattr(utils.sp, "get_async", AsyncMock(return_value=persona_config))
    event = SimpleNamespace(
        unified_msg_origin=ORIGIN,
        get_sender_id=lambda: "alice",
        get_sender_name=lambda: "Alice",
        get_platform_name=lambda: "qq",
        get_group_id=lambda: "1",
        get_message_type=lambda: None,
        is_admin=lambda: True,
    )
    run = SimpleNamespace(context=SimpleNamespace(event=event))
    context = SimpleNamespace()
    # Exercise AstrBot's actual no-persona normalization, not a tool stub.
    assert await utils.get_persona_id(context, event) is None
    try:
        yield engine, context, run, persona_config
    finally:
        await engine.close()


def config(mode="delete", *, scope_mode="session"):
    return ConfigManager(
        {
            "filtering_settings": {
                "use_persona_filtering": True,
                "memory_scope_mode": scope_mode,
                "use_session_filtering": scope_mode != "legacy",
            },
            "agent_tools": {
                "enable_forget_tool": True,
                "enable_correction_plan_tool": True,
                "forget_mode": mode,
                "forget_cooldown_seconds": 0,
                "forget_max_per_request": 10,
            },
        }
    )


async def memory(engine, persona, *, session=ORIGIN, source=ORIGIN):
    return await engine.add_memory(
        "喜欢咖啡",
        session_id=session,
        persona_id=persona,
        metadata={
            "status": "active",
            "source_session_id": source,
            "key_facts": ["喜欢咖啡"],
        },
        source_messages=[
            {"role": "user", "content": "我喜欢咖啡", "sender_id": "alice"}
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", ["[%None]", None])
@pytest.mark.parametrize("mode", ["delete", "archive", "recoverable"])
async def test_null_persona_forget_modes_use_real_scoped_memory(
    tool_context, persona, mode
):
    engine, context, run, _ = tool_context
    memory_id = await memory(engine, persona)
    tool = MemoryForgetTool(
        context=context, config_manager=config(mode), memory_engine=engine
    )
    result = json.loads(await tool.call(run, memory_id, reason="用户要求遗忘"))
    current = await engine.get_memory(memory_id)
    if mode == "delete":
        assert result == {"deleted": True, "id": memory_id}
        assert current is None
    else:
        assert result["archived"] and current["metadata"]["status"] == "archived"
        if mode == "recoverable":
            operation = await engine.memory_administration.store.get(
                result["operation_id"]
            )
            assert operation["actor"] == "tool:" + ORIGIN + ":alice"


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", ["[%None]", None])
@pytest.mark.parametrize("mode", ["archive", "recoverable"])
async def test_null_persona_restore_keeps_original_memory_id(
    tool_context, persona, mode
):
    engine, context, run, _ = tool_context
    memory_id = await memory(engine, persona)
    source = await engine.get_memory_source(memory_id)
    tool = MemoryForgetTool(
        context=context, config_manager=config(mode), memory_engine=engine
    )
    assert json.loads(await tool.call(run, memory_id, reason="用户要求遗忘"))[
        "archived"
    ]
    result = json.loads(
        await tool.call(run, memory_id, reason="用户要求恢复", action="restore")
    )
    assert result == {"restored": True, "id": memory_id}
    current = await engine.get_memory(memory_id)
    assert current["text"] == "喜欢咖啡" and current["metadata"]["status"] == "active"
    assert await engine.get_memory_source(memory_id) == source


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", ["[%None]", None])
async def test_null_persona_correction_creates_review_only(tool_context, persona):
    engine, context, run, _ = tool_context
    memory_id = await memory(engine, persona)
    processor = SimpleNamespace(
        _call_llm_with_retry=AsyncMock(
            return_value='{"content":"喜欢茶","key_facts":["喜欢茶"]}'
        )
    )
    tool = MemoryCorrectionPlanTool(
        context=context,
        config_manager=config(),
        memory_engine=engine,
        memory_processor=processor,
    )
    result = json.loads(await tool.call(run, memory_id, "纠正为茶"))
    assert result["requires_webui_confirmation"] and result["new_content"] == "喜欢茶"
    assert "confirmation_token" not in result
    assert (await engine.get_memory(memory_id))["text"] == "喜欢咖啡"
    operation = await engine.memory_administration.store.get(result["operation_id"])
    assert operation["status"] == "preview"
    processor._call_llm_with_retry.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["forget", "correction"])
@pytest.mark.parametrize(
    "current_persona,stored_persona",
    [("[%None]", "other"), ("p", "[%None]"), ("p", None), ("p", "other")],
)
async def test_null_persona_compatibility_does_not_allow_other_personas(
    tool_context, kind, current_persona, stored_persona
):
    engine, context, run, persona_config = tool_context
    persona_config["persona_id"] = current_persona
    memory_id = await memory(engine, stored_persona)
    processor = SimpleNamespace(_call_llm_with_retry=AsyncMock())
    cls = MemoryForgetTool if kind == "forget" else MemoryCorrectionPlanTool
    options = {} if kind == "forget" else {"memory_processor": processor}
    tool = cls(
        context=context, config_manager=config(), memory_engine=engine, **options
    )
    result = json.loads(await tool.call(run, memory_id, "用户要求纠正"))
    expected = (
        "memory is not available in current scope"
        if kind == "forget"
        else "memory_not_in_current_persona"
    )
    assert result["error"] == expected
    assert (await engine.get_memory(memory_id))["text"] == "喜欢咖啡"
    processor._call_llm_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["forget", "correction"])
async def test_legacy_null_persona_cannot_mutate_another_session(tool_context, kind):
    engine, context, run, _ = tool_context
    memory_id = await memory(engine, "[%None]", session="qq:GroupMessage:2")
    processor = SimpleNamespace(_call_llm_with_retry=AsyncMock())
    cls = MemoryForgetTool if kind == "forget" else MemoryCorrectionPlanTool
    options = {} if kind == "forget" else {"memory_processor": processor}
    tool = cls(
        context=context, config_manager=config(), memory_engine=engine, **options
    )
    result = json.loads(await tool.call(run, memory_id, "用户要求纠正"))
    assert "error" in result
    assert (await engine.get_memory(memory_id))["text"] == "喜欢咖啡"
    processor._call_llm_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_mode", ["global", "legacy"])
@pytest.mark.parametrize("source", [ORIGIN, "qq:GroupMessage:2"])
async def test_forget_global_and_unfiltered_scopes_still_require_own_source(
    tool_context, scope_mode, source
):
    engine, context, run, _ = tool_context
    session = GLOBAL_MEMORY_SCOPE if scope_mode == "global" else ORIGIN
    memory_id = await memory(engine, "[%None]", session=session, source=source)
    tool = MemoryForgetTool(
        context=context,
        config_manager=config(scope_mode=scope_mode),
        memory_engine=engine,
    )
    result = json.loads(await tool.call(run, memory_id, "用户要求遗忘"))
    if source == ORIGIN:
        assert result["deleted"] and await engine.get_memory(memory_id) is None
    else:
        assert result["error"] == "memory is not available in current scope"
        assert (await engine.get_memory(memory_id))["text"] == "喜欢咖啡"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_mode", ["session", "global"])
async def test_correction_plan_still_requires_current_source(tool_context, scope_mode):
    engine, context, run, _ = tool_context
    session = GLOBAL_MEMORY_SCOPE if scope_mode == "global" else ORIGIN
    memory_id = await memory(
        engine, "[%None]", session=session, source="qq:GroupMessage:2"
    )
    processor = SimpleNamespace(_call_llm_with_retry=AsyncMock())
    tool = MemoryCorrectionPlanTool(
        context=context,
        config_manager=config(scope_mode=scope_mode),
        memory_engine=engine,
        memory_processor=processor,
    )
    result = json.loads(await tool.call(run, memory_id, "纠正为茶"))
    assert result == {"error": "memory_not_in_current_scope"}
    assert (await engine.get_memory(memory_id))["text"] == "喜欢咖啡"
    processor._call_llm_with_retry.assert_not_awaited()
