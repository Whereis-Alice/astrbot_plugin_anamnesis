"""Unfiltered legacy correction plans still require current source and live permissions."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.managers.memory_mutation import memory_mutation_guard
from astrbot_plugin_anamnesis.core.tools.memory_correction_plan_tool import (
    MemoryCorrectionPlanTool,
)

from tests.test_memory_forget_tool import _run_context

pytest_plugins = ["tests.test_memory_administration"]
ORIGIN = "qq:GroupMessage:1"


async def setup_plan(
    administration, monkeypatch, *, source=ORIGIN, persona="p", admin=True
):
    memory_id = await administration.engine.add_memory(
        "喜欢咖啡",
        session_id=ORIGIN,
        persona_id=persona,
        metadata={"source_session_id": source},
        source_messages=[{"role": "user", "content": "咖啡原文"}],
    )
    config = ConfigManager(
        {
            "agent_tools": {"enable_correction_plan_tool": True},
            "filtering_settings": {
                "memory_scope_mode": "legacy",
                "use_session_filtering": False,
                "use_persona_filtering": True,
            },
        }
    )
    tool = MemoryCorrectionPlanTool(
        context=Mock(),
        config_manager=config,
        memory_engine=administration.engine,
        memory_processor=administration.processor,
    )
    context = _run_context(sender_id="alice", origin=ORIGIN)
    context.context.event.is_admin = Mock(return_value=admin)
    module = __import__(
        MemoryCorrectionPlanTool.__module__, fromlist=["get_persona_id"]
    )
    monkeypatch.setattr(module, "get_persona_id", AsyncMock(return_value="p"))
    return memory_id, tool, context, module


async def propose(tool, context, memory_id):
    return json.loads(
        await tool.call(context, memory_id=memory_id, instruction="改为茶")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["current", "foreign_source", "missing_source", "foreign_persona"]
)
async def test_legacy_none_scope_uses_current_source_and_persona_boundary(
    administration,
    monkeypatch,
    case,
):
    source = (
        "qq:GroupMessage:2"
        if case == "foreign_source"
        else None
        if case == "missing_source"
        else ORIGIN
    )
    memory_id, tool, context, _ = await setup_plan(
        administration,
        monkeypatch,
        source=source,
        persona="other" if case == "foreign_persona" else "p",
    )
    before = await administration.snapshot(memory_id)
    result = await propose(tool, context, memory_id)
    if case == "current":
        assert (
            result["old_content"] == "喜欢咖啡"
            and result["requires_webui_confirmation"]
        )
        operation = await administration.store.get(result["operation_id"])
        assert (
            operation["status"] == "preview"
            and operation["actor"] == f"tool:{ORIGIN}:alice"
        )
        assert operation["payload"]["before"] == before
    else:
        assert result["error"] == (
            "memory_not_in_current_persona"
            if case == "foreign_persona"
            else "memory_not_in_current_scope"
        )
        assert await administration.store.list(kind="correction") == []
        administration.processor._call_llm_with_retry.assert_not_awaited()
    assert await administration.snapshot(memory_id) == before


@pytest.mark.asyncio
async def test_nonadministrator_cannot_use_legacy_scope_to_get_a_preview(
    administration,
    monkeypatch,
):
    memory_id, tool, context, _ = await setup_plan(
        administration, monkeypatch, admin=False
    )
    result = await propose(tool, context, memory_id)
    assert result == {"error": "administrator_required"}
    assert await administration.store.list(kind="correction") == []
    administration.processor._call_llm_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", ["administrator", "feature"])
async def test_waiting_plan_rechecks_permissions_after_acquiring_writer(
    administration,
    monkeypatch,
    lost,
):
    memory_id, tool, context, module = await setup_plan(administration, monkeypatch)
    checked, locked, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    access = module.ensure_tool_access

    async def observed_access(*args, **kwargs):
        await access(*args, **kwargs)
        checked.set()

    monkeypatch.setattr(module, "ensure_tool_access", observed_access)

    async def writer():
        async with memory_mutation_guard(administration.engine):
            locked.set()
            await release.wait()

    writing = asyncio.create_task(writer())
    tasks = [writing]
    try:
        await asyncio.wait_for(locked.wait(), 5)
        planning = asyncio.create_task(propose(tool, context, memory_id))
        tasks.append(planning)
        await asyncio.wait_for(checked.wait(), 5)
        assert not planning.done()
        if lost == "administrator":
            context.context.event.is_admin.return_value = False
        else:
            tool.config_manager._config["agent_tools"][
                "enable_correction_plan_tool"
            ] = False
        release.set()
        result = await asyncio.wait_for(planning, 5)
        assert result == {
            "error": "administrator_required"
            if lost == "administrator"
            else "memory tool is disabled"
        }
        assert await administration.store.list(kind="correction") == []
        administration.processor._call_llm_with_retry.assert_not_awaited()
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
