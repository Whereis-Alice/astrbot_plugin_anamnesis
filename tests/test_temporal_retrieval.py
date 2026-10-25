"""Calendar boundaries and scoped direct retrieval regressions."""

import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.managers.memory_engine_evidence import (
    MemoryEngineEvidenceMixin,
)
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_anamnesis.core.retrieval.temporal_query import (
    parse_time_range,
    parse_timestamp,
    range_overlaps,
    resolve_context_timezone,
)
from astrbot_plugin_anamnesis.core.retrieval.temporal_retrieval import search_with_time
from astrbot_plugin_anamnesis.storage.atom_store import AtomStore

REFERENCE = datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "query,start,end",
    [
        ("昨天聊了什么", "2026-10-24T00:00:00+08:00", "2026-10-25T00:00:00+08:00"),
        ("上周有哪些事情", "2026-10-12T00:00:00+08:00", "2026-10-19T00:00:00+08:00"),
        ("上上周", "2026-10-05T00:00:00+08:00", "2026-10-12T00:00:00+08:00"),
        ("上个月", "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00"),
        (
            "2026年9月7日到2026年9月14日",
            "2026-09-07T00:00:00+08:00",
            "2026-09-15T00:00:00+08:00",
        ),
        (
            "2026-09-07至2026-09-14",
            "2026-09-07T00:00:00+08:00",
            "2026-09-15T00:00:00+08:00",
        ),
        ("9月7日", "2026-09-07T00:00:00+08:00", "2026-09-08T00:00:00+08:00"),
        ("2026年9月", "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00"),
        ("12月30日至1月2日", "2026-12-30T00:00:00+08:00", "2027-01-03T00:00:00+08:00"),
    ],
)
def test_calendar_ranges_use_explicit_zone_and_next_midnight(query, start, end):
    actual = parse_time_range(query, now=REFERENCE, timezone_name="Asia/Shanghai")
    assert actual.start == datetime.fromisoformat(start).timestamp()
    assert actual.end == datetime.fromisoformat(end).timestamp()
    assert actual.as_dict()["interval"] == "[start,end)"


def test_dst_calendar_day_is_not_fixed_86400_seconds():
    now = datetime(2026, 3, 9, 12, tzinfo=timezone.utc)
    value = parse_time_range("昨天", now=now, timezone_name="America/New_York")
    assert value.end - value.start == 23 * 3600


@pytest.mark.parametrize(
    "query", ["以前喜欢什么", "上次说了什么", "最近怎么样", "咖啡"]
)
def test_vague_time_does_not_invent_date_filter(query):
    assert parse_time_range(query, now=REFERENCE) is None


@pytest.mark.parametrize("value", [True, float("inf"), float("nan"), -1, "not-a-date"])
def test_invalid_timestamp_rejected(value):
    with pytest.raises(ValueError):
        parse_timestamp(value)


def test_exclusive_end_and_source_window_overlap():
    assert range_overlaps(100, 100, 100, 200)
    assert not range_overlaps(200, 200, 100, 200)
    assert range_overlaps(90, 110, 100, 200)
    assert parse_timestamp(
        "2026-10-25T00:00:00", timezone_name="Asia/Shanghai"
    ) == parse_timestamp("2026-10-24T16:00:00Z")


class _Engine(MemoryEngineEvidenceMixin):
    def __init__(self, path, connection):
        self.db_path = str(path)
        self.db_connection = connection
        self.config = {}
        self.atom_enabled = True
        self.search_memories = AsyncMock(return_value=[])
        self._schedule_access_updates = lambda ids: None

    async def get_memory(self, memory_id):
        async with self.db_connection.execute(
            "SELECT text,metadata FROM documents WHERE id=?", (memory_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return {"text": row[0], "metadata": json.loads(row[1])} if row else None


@pytest_asyncio.fixture
async def engine(tmp_path):
    path = tmp_path / "time.db"
    connection = await aiosqlite.connect(path)
    await connection.execute(
        "CREATE TABLE documents(id INTEGER PRIMARY KEY,text TEXT,metadata TEXT)"
    )
    await connection.commit()
    atom_store = AtomStore(str(path))
    await atom_store.initialize()
    value = _Engine(path, connection)
    try:
        yield value
    finally:
        await connection.close()


async def _put(engine, memory_id, instant=150, **metadata):
    data = {
        "create_time": instant,
        "session_id": "scope",
        "persona_id": "p",
        "status": "active",
        "participants": ["Alice"],
        **metadata,
    }
    await engine.db_connection.execute(
        "INSERT INTO documents VALUES(?,?,?)",
        (memory_id, f"body {memory_id}", json.dumps(data)),
    )
    await engine.db_connection.commit()
    return data


@pytest.mark.asyncio
async def test_pure_time_finds_low_semantic_memory_and_excludes_scope_end_archives(
    engine,
):
    await _put(engine, 1)
    await _put(engine, 2, session_id="other")
    await _put(engine, 3, persona_id="other")
    await _put(engine, 4, status="archived")
    await _put(engine, 5, instant=200)
    await _put(engine, 6, instant=100)
    results, details = await search_with_time(
        engine,
        "",
        k=10,
        session_id="scope",
        persona_id="p",
        mode="time",
        start_time=100,
        end_time=200,
    )
    assert {item.doc_id for item in results} == {1, 6}
    engine.search_memories.assert_not_awaited()
    assert details["direct_time_lookup"] is True


@pytest.mark.asyncio
async def test_sql_time_uses_event_then_window_then_creation_and_handles_numeric_strings(
    engine,
):
    await _put(engine, 1, instant=900, event_time=150)
    await _put(engine, 2, instant=900, source_time_start=90, source_time_end=110)
    await _put(engine, 3, instant="150")
    await _put(engine, 4, instant=150, event_time=500)
    results, _ = await search_with_time(
        engine, "", k=10, mode="time", start_time=100, end_time=200
    )
    assert {item.doc_id for item in results} == {1, 2, 3}


@pytest.mark.asyncio
async def test_speaker_predicate_precedes_candidate_limit_and_supports_legacy_scalar(
    engine,
):
    await _put(engine, 1, instant=110, participants="Alice")
    for memory_id in range(2, 10):
        await _put(engine, memory_id, instant=150 + memory_id, participants=["Bob"])
    results, _ = await search_with_time(
        engine,
        "",
        k=1,
        mode="time",
        start_time=100,
        end_time=200,
        speaker="alice",
        candidate_limit=1,
    )
    assert [item.doc_id for item in results] == [1]


@pytest.mark.asyncio
async def test_expired_atoms_and_missing_atom_markers_do_not_consume_candidate_limit(
    engine,
):
    await _put(engine, 1, instant=110)
    await _put(engine, 2, instant=190, atom_types=["factual"])
    await _put(engine, 3, instant=195, atom_types=["factual"])
    now = time.time()
    await engine.db_connection.execute(
        """INSERT INTO memory_atoms
        (parent_memory_id,content,created_at,last_accessed_at,expires_at,status)
        VALUES(2,'expired',?,?,?,'active')""",
        (now, now, now - 1),
    )
    await engine.db_connection.commit()
    results, _ = await search_with_time(
        engine, "", k=1, mode="time", start_time=100, end_time=200, candidate_limit=1
    )
    assert [item.doc_id for item in results] == [1]


@pytest.mark.asyncio
async def test_natural_time_only_does_not_require_embedding(engine):
    instant = parse_timestamp("2026-10-24T12:00:00+08:00")
    await _put(engine, 1, instant=instant)
    engine.search_memories.side_effect = RuntimeError("provider is unavailable")
    results, details = await search_with_time(
        engine, "昨天这个群讨论了什么", now=REFERENCE
    )
    assert [item.doc_id for item in results] == [1]
    assert details["mode"] == "time"
    engine.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_hybrid_keeps_direct_candidates_when_semantic_channel_fails(engine):
    instant = parse_timestamp("2026-10-24T12:00:00+08:00")
    await _put(engine, 1, instant=instant)
    engine.search_memories.side_effect = RuntimeError("provider unavailable")
    results, details = await search_with_time(engine, "昨天关于咖啡", now=REFERENCE)
    assert [item.doc_id for item in results] == [1]
    assert details["degraded_channels"] == ["semantic"]


@pytest.mark.asyncio
async def test_one_total_timeout_cancels_semantic_child(engine):
    settled = asyncio.Event()

    async def slow(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            settled.set()

    engine.search_memories.side_effect = slow
    with pytest.raises(TimeoutError):
        await search_with_time(engine, "coffee", timeout=0.01)
    assert settled.is_set()


@pytest.mark.asyncio
async def test_semantic_without_date_preserves_small_existing_candidate_pool(engine):
    metadata = await _put(engine, 1)
    engine.search_memories.return_value = [
        HybridResult(1, 0.9, 0.1, 0.4, 0.9, "body 1", metadata)
    ]
    results, details = await search_with_time(engine, "coffee", k=2)
    assert [item.doc_id for item in results] == [1]
    assert details["candidate_limit"] == 2
    assert engine.search_memories.await_args.kwargs["k"] == 2


@pytest.mark.asyncio
async def test_reloaded_scope_change_cannot_escape_original_authorization(engine):
    await _put(engine, 1)
    engine.get_memory = AsyncMock(
        return_value={
            "text": "moved",
            "metadata": {
                "create_time": 150,
                "session_id": "other",
                "persona_id": "p",
                "status": "active",
            },
        }
    )
    results, _ = await search_with_time(
        engine, "", mode="time", session_id="scope", start_time=100, end_time=200
    )
    assert results == []


@pytest.mark.asyncio
async def test_atom_event_time_retrieves_historic_fact_without_unrelated_current_facts(
    engine,
):
    await _put(engine, 1, instant=900, atom_types=["episodic"])
    now = time.time()
    for content, event_time in (("historic meeting", 150), ("later meeting", 250)):
        await engine.db_connection.execute(
            """INSERT INTO memory_atoms
          (parent_memory_id,content,created_at,last_accessed_at,expires_at,event_time,session_id,persona_id)
          VALUES(1,?,?,?,?,?,'scope','p')""",
            (content, now, now, now + 3600, event_time),
        )
    await engine.db_connection.commit()
    results, _ = await search_with_time(
        engine,
        "",
        mode="time",
        start_time=100,
        end_time=200,
        session_id="scope",
        persona_id="p",
    )
    assert [item.doc_id for item in results] == [1]
    assert results[0].content == "historic meeting"
    assert results[0].metadata["temporal_time_basis"] == "atom_event_time"
    assert results[0].metadata["key_facts"] == ["historic meeting"]


@pytest.mark.asyncio
async def test_actual_time_query_ignores_date_words_in_semantic_expansion(engine):
    await _put(engine, 1)
    engine.search_memories.return_value = [
        HybridResult(
            1, 0.9, 0.1, 0.5, 0.9, "body 1", (await engine.get_memory(1))["metadata"]
        )
    ]
    results, details = await search_with_time(
        engine, "coffee 昨天 历史别名", time_query="喜欢什么饮料"
    )
    assert [item.doc_id for item in results] == [1]
    assert details["time_range"] is None


def test_host_timezone_uses_event_profile_and_plugin_override():
    origins = []
    context = SimpleNamespace(
        get_config=lambda origin: (
            origins.append(origin) or {"timezone": "America/New_York"}
        )
    )
    config = SimpleNamespace(get=lambda key, default: "")
    assert (
        resolve_context_timezone(context, config, "qq:GroupMessage:1")
        == "America/New_York"
    )
    assert origins == ["qq:GroupMessage:1"]
    override = SimpleNamespace(get=lambda key, default: "Asia/Tokyo")
    assert resolve_context_timezone(context, override, "scope") == "Asia/Tokyo"


def test_invalid_temporal_timezone_falls_back_to_valid_host_zone():
    config = SimpleNamespace(get=lambda key, default: "invalid-timezone")
    context = SimpleNamespace(get_config=lambda origin: {"timezone": "UTC"})
    assert resolve_context_timezone(context, config, "scope") == "UTC"
    assert resolve_context_timezone(object(), None) == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_today_meal_question_keeps_general_preference_retrieval(engine):
    metadata = await _put(engine, 1, instant=150)
    engine.search_memories.return_value = [
        HybridResult(1, 0.9, 0.1, 0.5, 0.9, "body 1", metadata)
    ]
    results, details = await search_with_time(engine, "今天吃什么", now=REFERENCE)
    assert [item.doc_id for item in results] == [1]
    assert details["mode"] == "semantic" and details["time_range"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("agentic_enabled", [False, True])
async def test_tool_time_mode_works_with_and_without_agentic_recall_flag(
    engine, monkeypatch, agentic_enabled
):
    from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
    from astrbot_plugin_anamnesis.core.tools import agentic_recall, memory_search_tool

    await _put(engine, 1)
    event = SimpleNamespace(
        unified_msg_origin="scope",
        get_sender_id=lambda: "1",
        get_platform_name=lambda: "qq",
    )
    context = SimpleNamespace(get_config=lambda origin: {"timezone": "UTC"})
    config = ConfigManager(
        {
            "agent_tools": {"agentic_recall_enabled": agentic_enabled},
            "filtering_settings": {"memory_scope_mode": "session"},
        }
    )
    tool = memory_search_tool.MemorySearchTool(
        context=context, config_manager=config, memory_engine=engine
    )
    monkeypatch.setattr(
        memory_search_tool, "get_persona_id", AsyncMock(return_value="p")
    )
    monkeypatch.setattr(agentic_recall, "get_persona_id", AsyncMock(return_value="p"))
    run = SimpleNamespace(context=SimpleNamespace(event=event))
    result = json.loads(
        await tool.call(run, mode="time", start_time="100", end_time="200")
    )
    assert [item["id"] for item in result["results"]] == [1]
    assert result["retrieval"]["time_range"]["timezone"] == "UTC"
    engine.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_all_personas_requires_explicit_override(engine):
    await _put(engine, 1, persona_id="p")
    config = SimpleNamespace(
        get=lambda key, default: default,
        filtering_settings={"use_persona_filtering": True},
    )
    scoped, _ = await search_with_time(
        engine,
        "",
        mode="time",
        start_time=100,
        end_time=200,
        config_manager=config,
        persona_id=None,
    )
    all_personas, _ = await search_with_time(
        engine,
        "",
        mode="time",
        start_time=100,
        end_time=200,
        config_manager=config,
        persona_filtered=False,
    )
    assert scoped == []
    assert [item.doc_id for item in all_personas] == [1]
