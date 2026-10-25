"""Optional agentic recall and its cursors share legacy no-persona policy."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core import passive_group_capture, utils
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.recall_persona import NO_PERSONA
from astrbot_plugin_anamnesis.core.tools.memory_evidence_tool import MemoryEvidenceTool
from astrbot_plugin_anamnesis.core.tools.memory_search_tool import MemorySearchTool

from tests.test_memory_transfer_runtime_safety import _engine

SCOPE = "qq:GroupMessage:1"


@pytest_asyncio.fixture
async def recall(tmp_path, monkeypatch):
    engine = await _engine(tmp_path / "agentic-persona.db")
    engine.atom_enabled = False
    engine.config["recent_memory_count"] = 0
    selection = {"persona": NO_PERSONA}
    monkeypatch.setattr(
        utils.sp,
        "get_async",
        AsyncMock(side_effect=lambda **_: {"persona_id": selection["persona"]}),
    )
    monkeypatch.setattr(
        passive_group_capture, "is_session_enabled", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        passive_group_capture,
        "is_plugin_enabled_for_session",
        AsyncMock(return_value=True),
    )
    config = ConfigManager(
        {
            "filtering_settings": {
                "use_persona_filtering": True,
                "memory_scope_mode": "session",
            },
            "agent_tools": {"agentic_recall_enabled": True},
        }
    )
    context = SimpleNamespace()
    event = SimpleNamespace(
        unified_msg_origin=SCOPE,
        get_sender_id=lambda: "alice",
        get_group_id=lambda: "1",
    )
    run = SimpleNamespace(context=SimpleNamespace(event=event))
    ids = {}
    try:
        for persona in (NO_PERSONA, None, "selected", "foreign"):
            ids[persona] = await engine.add_memory(
                "coffee remembered fact"
                if persona != "foreign"
                else "coffee foreign secret",
                session_id=SCOPE,
                persona_id=persona,
                metadata={"source_session_id": SCOPE},
                source_messages=[
                    {"role": "user", "content": "original coffee message"}
                ],
            )
        await engine.add_memory(
            "coffee other chat secret", session_id="qq:GroupMessage:2", persona_id=None
        )
        tool = MemorySearchTool(
            context=context, config_manager=config, memory_engine=engine
        )
        yield tool, run, ids, selection
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", [NO_PERSONA, "selected"])
@pytest.mark.parametrize("mode", ["semantic", "time", "hybrid"])
async def test_public_agentic_tool_accepts_legacy_none_and_excludes_other_personas(
    recall, persona, mode
):
    tool, run, ids, selection = recall
    selection["persona"] = persona
    options = (
        {"start_time": time.time() - 86400, "end_time": time.time() + 60}
        if mode != "semantic"
        else {}
    )
    result = json.loads(
        await tool.call(
            run, query="coffee", k=10, mode=mode, include_source=True, **options
        )
    )
    expected = {ids[None], ids[NO_PERSONA]} if persona == NO_PERSONA else {ids[persona]}
    assert {item["id"] for item in result["results"]} == expected
    assert all(item["source_messages"] for item in result["results"])
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_public_agentic_cursor_keeps_legacy_and_null_records_on_later_pages(
    recall,
):
    tool, run, ids, _ = recall
    first = json.loads(
        await tool.call(run, query="coffee", k=1, mode="semantic", include_source=True)
    )
    assert len(first["results"]) == 1 and first["next_cursor"]
    second = json.loads(
        await tool.call(
            run,
            query="coffee",
            k=1,
            mode="semantic",
            include_source=True,
            cursor=first["next_cursor"],
        )
    )
    assert len(second["results"]) == 1 and second["next_cursor"] is None
    assert {item["id"] for item in first["results"] + second["results"]} == {
        ids[None],
        ids[NO_PERSONA],
    }
    assert all(item["source_messages"] for item in first["results"] + second["results"])


@pytest.mark.asyncio
async def test_cursor_rechecks_persona_after_a_legacy_record_moves(recall, monkeypatch):
    tool, run, ids, _ = recall
    route = AsyncMock(wraps=tool.memory_engine.hybrid_retriever.search)
    monkeypatch.setattr(tool.memory_engine.hybrid_retriever, "search", route)
    first = json.loads(await tool.call(run, query="coffee", k=1, mode="semantic"))
    remaining = ({ids[None], ids[NO_PERSONA]} - {first["results"][0]["id"]}).pop()
    assert await tool.memory_engine.update_memory(
        remaining, {"metadata": {"persona_id": "foreign"}}
    )
    second = json.loads(
        await tool.call(
            run, query="coffee", k=1, mode="semantic", cursor=first["next_cursor"]
        )
    )
    assert second["results"] == []
    assert route.await_count == 1


@pytest.mark.asyncio
async def test_legacy_recall_and_public_evidence_reader_use_the_same_boundary(recall):
    tool, run, ids, _ = recall
    found = json.loads(await tool.call(run, query="coffee", k=10, mode="semantic"))
    reader = MemoryEvidenceTool(
        context=tool.context,
        config_manager=tool.config_manager,
        memory_engine=tool.memory_engine,
    )
    for item in found["results"]:
        evidence = json.loads(await reader.call(run, item["id"]))
        assert evidence["id"] in {ids[None], ids[NO_PERSONA]}
        assert evidence["source_messages"][0]["content"] == "original coffee message"
    denied = json.loads(await reader.call(run, ids["foreign"]))
    assert "current scope" in denied["error"]
