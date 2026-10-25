"""Public recall preserves exact no-persona selection and legacy read APIs."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core import utils
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.retrieval.temporal_retrieval import search_with_time
from astrbot_plugin_anamnesis.core.tools import memory_search_tool
from astrbot_plugin_anamnesis.core.tools.memory_search_tool import MemorySearchTool

from tests.test_memory_transfer_runtime_safety import _engine

SCOPE = "qq:GroupMessage:1"
LEGACY_NONE = "[%None]"


@pytest_asyncio.fixture
async def memories(tmp_path):
    engine = await _engine(tmp_path / "memory.db")
    engine.atom_enabled = False
    # Recent-memory fallback must not hide a failed semantic selection.
    engine.config["recent_memory_count"] = 0
    ids = {}
    try:
        for persona, content in (
            ("persona-a", "coffee persona-a secret"),
            ("persona-b", "coffee persona-b secret"),
            (None, "coffee no-persona fact"),
            (LEGACY_NONE, "coffee legacy no-persona fact"),
        ):
            ids[persona] = await engine.add_memory(
                content=content, session_id=SCOPE, persona_id=persona
            )
        yield engine, ids
    finally:
        await engine.close()


def selected_ids(ids, persona, filtered=True):
    if not filtered:
        return set(ids.values())
    if persona in (None, LEGACY_NONE):
        return {ids[None], ids[LEGACY_NONE]}
    return {ids[persona]}


async def scoped_search(
    engine, *, persona=None, filtered=True, mode="semantic", record_access=True
):
    options = {}
    if mode != "semantic":
        options = {"start_time": time.time() - 86400, "end_time": time.time() + 60}
    results, _ = await search_with_time(
        engine,
        "coffee",
        k=50,
        session_id=SCOPE,
        persona_id=persona,
        persona_filtered=filtered,
        mode=mode,
        record_access=record_access,
        **options,
    )
    return {result.doc_id for result in results}


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", [None, LEGACY_NONE, "persona-a"])
@pytest.mark.parametrize("mode", ["semantic", "time", "hybrid"])
@pytest.mark.parametrize("record_access", [True, False])
async def test_exact_persona_selection_matches_semantic_time_and_readonly_paths(
    memories, persona, mode, record_access
):
    engine, ids = memories
    assert await scoped_search(
        engine, persona=persona, mode=mode, record_access=record_access
    ) == selected_ids(ids, persona)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["semantic", "time", "hybrid"])
async def test_disabled_filter_keeps_no_persona_recall_unrestricted(memories, mode):
    engine, ids = memories
    assert await scoped_search(engine, filtered=False, mode=mode) == set(ids.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["bm25", "vector", "hybrid"])
@pytest.mark.parametrize("persona", [None, LEGACY_NONE])
async def test_direct_retriever_none_keeps_legacy_unfiltered_contract(
    memories, route, persona
):
    engine, ids = memories
    hybrid = engine.hybrid_retriever
    retriever = {
        "hybrid": hybrid,
        "bm25": hybrid.bm25_retriever,
        "vector": hybrid.vector_retriever,
    }[route]
    results = await retriever.search("coffee", 50, SCOPE, persona)
    expected = set(ids.values()) if persona is None else selected_ids(ids, persona)
    assert {result.doc_id for result in results} == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", [None, LEGACY_NONE, "persona-a"])
@pytest.mark.parametrize("filtered", [True, False])
async def test_public_recall_tool_applies_current_persona_and_filter_switch(
    memories, monkeypatch, persona, filtered
):
    engine, ids = memories
    context = SimpleNamespace()
    event = SimpleNamespace(
        unified_msg_origin=SCOPE,
        get_sender_id=lambda: "alice",
        get_group_id=lambda: "1",
    )
    run = SimpleNamespace(context=SimpleNamespace(event=event))
    config = ConfigManager(
        {
            "filtering_settings": {"use_persona_filtering": filtered},
            "recall_engine": {"max_k": 50},
        }
    )
    persona_lookup = AsyncMock(return_value=persona)
    monkeypatch.setattr(memory_search_tool, "get_persona_id", persona_lookup)
    tool = MemorySearchTool(
        context=context, config_manager=config, memory_engine=engine
    )
    result = json.loads(await tool.call(run, query="coffee", k=50))
    assert result["applied_filters"]["persona_filtered"] is filtered
    assert {item["id"] for item in result["results"]} == selected_ids(
        ids, persona, filtered
    )
    if filtered:
        persona_lookup.assert_awaited_once()
        if persona in (None, LEGACY_NONE):
            assert "secret" not in json.dumps(result)
    else:
        persona_lookup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("record_access", [True, False])
@pytest.mark.parametrize("route", ["hybrid", "bm25", "vector"])
async def test_exact_none_top_one_survives_many_higher_ranked_foreign_candidates(
    tmp_path, monkeypatch, record_access, route
):
    engine = await _engine(tmp_path / "many-foreign.db")
    engine.atom_enabled = False
    engine.config["recent_memory_count"] = 0
    # An optional channel can legitimately return no hits. Each remaining
    # route must find its own scoped candidate instead of hiding behind fusion.
    if route != "hybrid":
        absent_route = (
            engine.hybrid_retriever.vector_retriever
            if route == "bm25"
            else engine.hybrid_retriever.bm25_retriever
        )
        monkeypatch.setattr(absent_route, "search", AsyncMock(return_value=[]))
    try:
        for index in range(16):
            await engine.add_memory(
                content="coffee", session_id=SCOPE, persona_id=f"foreign-{index}"
            )
        own = await engine.add_memory(
            content="coffee no-persona fact", session_id=SCOPE, persona_id=None
        )
        results, _ = await search_with_time(
            engine,
            "coffee",
            k=1,
            session_id=SCOPE,
            persona_id=None,
            persona_filtered=True,
            mode="semantic",
            record_access=record_access,
        )
        assert [result.doc_id for result in results] == [own]
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped_first", [False, True])
async def test_cached_unrestricted_and_exact_none_reads_cannot_alias(
    memories, monkeypatch, scoped_first
):
    engine, ids = memories
    route = AsyncMock(wraps=engine.hybrid_retriever.search)
    monkeypatch.setattr(engine.hybrid_retriever, "search", route)

    async def read(scoped):
        if scoped:
            return await scoped_search(engine)
        return {
            result.doc_id
            for result in await engine.search_memories("coffee", 50, SCOPE, None)
        }

    for scoped in (scoped_first, not scoped_first, scoped_first, not scoped_first):
        assert await read(scoped) == selected_ids(ids, None, scoped)
    # Each distinct public read runs retrieval once, then serves its own cache.
    assert route.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_exact_none_and_unrestricted_reads_keep_request_contexts(
    memories, monkeypatch
):
    engine, ids = memories
    route = engine.hybrid_retriever.search
    both_entered = asyncio.Event()
    count = 0

    async def gated(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=5)
        return await route(*args, **kwargs)

    monkeypatch.setattr(engine.hybrid_retriever, "search", gated)
    tasks = [
        asyncio.create_task(scoped_search(engine)),
        asyncio.create_task(engine.search_memories("coffee", 50, SCOPE, None)),
    ]
    try:
        scoped, unrestricted = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        assert scoped == selected_ids(ids, None)
        assert {result.doc_id for result in unrestricted} == set(ids.values())
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["cancel", "timeout"])
async def test_cancelled_or_timed_out_exact_none_recall_resets_followup_read_context(
    memories, monkeypatch, end
):
    engine, ids = memories
    entered = asyncio.Event()
    route = engine.hybrid_retriever.search
    calls = 0
    after_cleanup = []

    async def gated(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.Event().wait()
        return await route(*args, **kwargs)

    monkeypatch.setattr(engine.hybrid_retriever, "search", gated)
    error = asyncio.CancelledError if end == "cancel" else TimeoutError

    async def worker():
        try:
            await search_with_time(
                engine,
                "coffee",
                k=50,
                session_id=SCOPE,
                persona_id=None,
                persona_filtered=True,
                mode="semantic",
                timeout=0.05 if end == "timeout" else None,
            )
        except error:
            # Check the cancelled caller itself; a separate task would inherit
            # the parent's clean context and could conceal a failed reset.
            results = await engine.search_memories("coffee", 50, SCOPE, None)
            after_cleanup.append({result.doc_id for result in results})
            raise

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if end == "cancel":
            task.cancel()
        with pytest.raises(error):
            await asyncio.wait_for(task, timeout=5)
        assert after_cleanup == [set(ids.values())]
        assert {
            result.doc_id
            for result in await engine.search_memories("coffee", 50, SCOPE, None)
        } == set(ids.values())
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_persona,conversation_persona,default_persona,expected",
    [
        (LEGACY_NONE, "conversation-persona", "default-persona", None),
        ("session-persona", LEGACY_NONE, "default-persona", "session-persona"),
        (None, LEGACY_NONE, "default-persona", None),
        (None, "conversation-persona", "default-persona", "conversation-persona"),
        (None, None, LEGACY_NONE, None),
        (None, None, "default-persona", "default-persona"),
        (None, None, None, None),
    ],
)
async def test_get_persona_id_normalizes_no_persona_at_each_resolution_level(
    monkeypatch, session_persona, conversation_persona, default_persona, expected
):
    monkeypatch.setattr(
        utils.sp, "get_async", AsyncMock(return_value={"persona_id": session_persona})
    )
    context = SimpleNamespace(
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="conversation"),
            get_conversation=AsyncMock(
                return_value=SimpleNamespace(persona_id=conversation_persona)
            ),
        ),
        persona_manager=SimpleNamespace(
            get_default_persona_v3=AsyncMock(return_value={"name": default_persona})
        ),
    )
    assert (
        await utils.get_persona_id(context, SimpleNamespace(unified_msg_origin=SCOPE))
        == expected
    )
