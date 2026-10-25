"""Tool execution rechecks mutable authorization and document ownership."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core import passive_group_capture
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.managers.memory_administration import (
    MemoryAdministration,
    fingerprint,
)
from astrbot_plugin_anamnesis.core.managers.memory_mutation import memory_mutation_guard
from astrbot_plugin_anamnesis.core.tools import agentic_recall
from astrbot_plugin_anamnesis.core.tools.memory_correction_plan_tool import (
    MemoryCorrectionPlanTool,
)
from astrbot_plugin_anamnesis.core.tools.memory_evidence_tool import MemoryEvidenceTool
from astrbot_plugin_anamnesis.core.tools.memory_forget_tool import MemoryForgetTool
from astrbot_plugin_anamnesis.core.tools.memory_memorize_tool import MemoryMemorizeTool
from astrbot_plugin_anamnesis.core.tools.memory_search_tool import MemorySearchTool

ORIGIN = "test:GroupMessage:1"
KINDS = ("search", "evidence", "forget", "correction", "memorize")


@pytest.fixture(autouse=True)
def allowed_sessions(monkeypatch):
    monkeypatch.setattr(
        passive_group_capture, "is_session_enabled", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        passive_group_capture,
        "is_plugin_enabled_for_session",
        AsyncMock(return_value=True),
    )
    for module in (
        agentic_recall,
        __import__(MemoryForgetTool.__module__, fromlist=["get_persona_id"]),
        __import__(MemoryCorrectionPlanTool.__module__, fromlist=["get_persona_id"]),
        __import__(MemorySearchTool.__module__, fromlist=["get_persona_id"]),
        __import__(MemoryMemorizeTool.__module__, fromlist=["get_persona_id"]),
    ):
        monkeypatch.setattr(module, "get_persona_id", AsyncMock(return_value="p"))


class PreviewService(MemoryAdministration):
    """Honor the administration snapshot contract without invoking a provider."""

    def __init__(self, engine):
        self.engine = engine
        self.previewed = []

    async def preview_correction(
        self, memory_id, instruction, *, actor, expected_memory=None
    ):
        async with memory_mutation_guard(self.engine):
            snapshot = await self.engine._replacement_snapshot(memory_id)
            if expected_memory is not None and fingerprint(
                expected_memory
            ) != fingerprint(snapshot):
                raise ValueError("memory_changed_before_preview")
        self.previewed.append(snapshot)
        return {"old_content": snapshot["text"], "requires_webui_confirmation": True}


def setup_tool(kind, *, config_values=None):
    event = SimpleNamespace(
        unified_msg_origin=ORIGIN,
        get_sender_id=lambda: "alice",
        get_sender_name=lambda: "Alice",
        get_platform_name=lambda: "test",
        get_message_type=lambda: None,
        is_admin=lambda: True,
    )
    context = SimpleNamespace(context=SimpleNamespace(event=event))
    config = ConfigManager(
        {
            "agent_tools": {
                "enable_recall_tool": True,
                "agentic_recall_enabled": True,
                "enable_forget_tool": True,
                "enable_correction_plan_tool": True,
                "enable_memorize_tool": True,
            },
            "filtering_settings": {
                "memory_scope_mode": "session",
                "use_persona_filtering": True,
            },
            **(config_values or {}),
        }
    )
    record = {
        "id": 7,
        "text": "current scoped fact",
        "source_messages": [{"content": "private source"}],
        "metadata": {
            "session_id": ORIGIN,
            "source_session_id": ORIGIN,
            "persona_id": "p",
            "status": "active",
            "has_source": True,
        },
    }
    loaded = asyncio.Event()

    async def load(_memory_id):
        loaded.set()
        return copy.deepcopy(record)

    engine = SimpleNamespace(
        get_memory=AsyncMock(side_effect=load),
        _replacement_snapshot=AsyncMock(side_effect=load),
        delete_memory=AsyncMock(return_value=True),
        add_memory=AsyncMock(return_value=8),
        search_memories=AsyncMock(return_value=[]),
        resolve_live_facts=AsyncMock(return_value=["current scoped fact"]),
        get_memory_source=AsyncMock(return_value=record["source_messages"]),
        get_memory_atom_lifecycle=AsyncMock(return_value=[]),
    )
    engine.memory_administration = PreviewService(engine)
    cls = {
        "search": MemorySearchTool,
        "evidence": MemoryEvidenceTool,
        "forget": MemoryForgetTool,
        "correction": MemoryCorrectionPlanTool,
        "memorize": MemoryMemorizeTool,
    }[kind]
    kwargs = {}
    if kind == "memorize":
        kwargs["memory_processor"] = SimpleNamespace(
            build_memory_from_structured_data=lambda **values: ("saved fact", {}, 0.5)
        )
    tool = cls(context=object(), config_manager=config, memory_engine=engine, **kwargs)
    return tool, context, engine, record, loaded


async def call_tool(kind, tool, context):
    if kind == "search":
        return json.loads(await tool.call(context, query="fact"))
    if kind == "evidence":
        return json.loads(await tool.call(context, memory_id=7))
    if kind == "forget":
        return json.loads(
            await tool.call(context, memory_id=7, reason="explicit request")
        )
    if kind == "memorize":
        return json.loads(await tool.call(context, memory="fact"))
    return json.loads(await tool.call(context, memory_id=7, instruction="correct fact"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_retained_tool_reference_honors_disabled_feature(kind):
    tool, context, engine, _, _ = setup_tool(kind)
    key = {
        "search": "enable_recall_tool",
        "evidence": "agentic_recall_enabled",
        "forget": "enable_forget_tool",
        "correction": "enable_correction_plan_tool",
        "memorize": "enable_memorize_tool",
    }[kind]
    tool.config_manager._config["agent_tools"][key] = False
    result = await call_tool(kind, tool, context)
    assert "error" in result
    engine.get_memory.assert_not_awaited()
    engine._replacement_snapshot.assert_not_awaited()
    engine.search_memories.assert_not_awaited()
    engine.delete_memory.assert_not_awaited()
    engine.add_memory.assert_not_awaited()
    assert engine.memory_administration.previewed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "gate", ["is_session_enabled", "is_plugin_enabled_for_session"]
)
async def test_retained_tool_honors_dynamic_session_disablement(
    kind, gate, monkeypatch
):
    tool, context, engine, _, _ = setup_tool(kind)
    monkeypatch.setattr(passive_group_capture, gate, AsyncMock(return_value=False))
    result = await call_tool(kind, tool, context)
    assert "error" in result
    engine.get_memory.assert_not_awaited()
    engine._replacement_snapshot.assert_not_awaited()
    engine.search_memories.assert_not_awaited()
    engine.delete_memory.assert_not_awaited()
    engine.add_memory.assert_not_awaited()
    assert engine.memory_administration.previewed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("session_id", "other-session"), ("persona_id", "other-persona")]
)
async def test_forget_rechecks_ownership_after_waiting_for_operation_lock(field, value):
    tool, context, engine, record, loaded = setup_tool("forget")
    await tool._forget_lock.acquire()
    pending = asyncio.create_task(call_tool("forget", tool, context))
    try:
        await asyncio.wait_for(loaded.wait(), 2)
        async with memory_mutation_guard(engine):
            record["metadata"][field] = value
            record["text"] = "another account's private fact"
    finally:
        tool._forget_lock.release()
    result = await asyncio.wait_for(pending, 2)
    assert result.get("error") == "memory is not available in current scope"
    engine.delete_memory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("session_id", "other-session"), ("persona_id", "other-persona")]
)
async def test_correction_plan_rejects_changed_scope_before_provider(field, value):
    tool, context, engine, record, loaded = setup_tool("correction")
    state = agentic_recall.request_state(context.context.event)
    await state["lock"].acquire()
    pending = asyncio.create_task(call_tool("correction", tool, context))
    try:
        await asyncio.wait_for(loaded.wait(), 2)
        async with memory_mutation_guard(engine):
            record["metadata"][field] = value
            record["text"] = "another account's private fact"
    finally:
        state["lock"].release()
    result = await asyncio.wait_for(pending, 2)
    assert "error" in result and "old_content" not in result
    assert engine.memory_administration.previewed == []


@pytest.mark.asyncio
async def test_evidence_cannot_pair_authorized_metadata_with_foreign_source():
    tool, context, engine, record, _ = setup_tool("evidence")
    reading, release = asyncio.Event(), asyncio.Event()

    async def read_facts(*args):
        reading.set()
        await release.wait()
        return ["current scoped fact"]

    engine.resolve_live_facts.side_effect = read_facts
    pending = asyncio.create_task(call_tool("evidence", tool, context))
    mutation = None
    try:
        await asyncio.wait_for(reading.wait(), 2)

        async def move_source():
            async with memory_mutation_guard(engine):
                record["metadata"]["session_id"] = "other-session"
                engine.get_memory_source.return_value = [
                    {"content": "foreign private source"}
                ]

        mutation = asyncio.create_task(move_source())
        await asyncio.sleep(0.02)
        release.set()
        result = await asyncio.wait_for(pending, 2)
        assert "foreign private source" not in json.dumps(result)
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (pending, mutation) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_search_omits_source_that_moved_after_candidate_retrieval():
    from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridResult

    tool, context, engine, record, _ = setup_tool("search")
    tool.config_manager._config["agent_tools"]["agentic_recall_enabled"] = False
    metadata = copy.deepcopy(record["metadata"])

    async def search(*args, **kwargs):
        record["metadata"]["session_id"] = "other-session"
        engine.get_memory_source.return_value = [{"content": "foreign private source"}]
        return [
            HybridResult(
                doc_id=7, content="old scoped fact", metadata=metadata, final_score=0.9
            )
        ]

    engine.search_memories.side_effect = search
    result = json.loads(await tool.call(context, query="fact", include_source=True))
    assert "foreign private source" not in json.dumps(result)
    engine.get_memory_source.assert_not_awaited()
