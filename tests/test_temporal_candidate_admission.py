"""Known out-of-window events cannot consume the parent-time fallback budget."""

import time
from types import SimpleNamespace

import aiosqlite
import pytest

from astrbot_plugin_anamnesis.core.retrieval import temporal_retrieval as temporal
from tests.test_memory_transfer_runtime_safety import _engine
from tests.test_temporal_evidence_consistency import add_events


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["time", "hybrid"])
@pytest.mark.parametrize("record_access", [False, True])
async def test_future_event_parents_cannot_hide_undated_in_range_memory(
    tmp_path, mode, record_access
):
    engine = await _engine(tmp_path / "budget.db")
    engine.config.update(recent_memory_count=0)
    now = time.time()
    try:
        fallback = await engine.add_memory(
            "undated record",
            session_id="scope",
            persona_id="p",
            metadata={"create_time": now - 30},
        )
        future = await add_events(engine, now, [86400])
        results, details = await temporal.search_with_time(
            engine,
            "tea",
            k=1,
            candidate_limit=1,
            mode=mode,
            session_id="scope",
            persona_id="p",
            start_time=now - 60,
            end_time=now + 60,
            record_access=record_access,
        )
        assert [item.doc_id for item in results] == [fallback]
        assert results[0].content == "undated record" and future != fallback
        assert details["mode"] == mode
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_dated_events_keep_the_independent_event_route_and_scope_filters(
    tmp_path,
):
    engine = await _engine(tmp_path / "event-route.db")
    now = time.time()
    try:
        target = await add_events(engine, now, [-30])
        other = await add_events(engine, now, [-10], persona="other")
        candidates = await temporal.direct_time_candidates(
            engine,
            temporal.TimeRange(now - 60, now + 60),
            k=1,
            session_id="scope",
            persona_id="p",
            speaker="Alice",
        )
        assert [item.doc_id for item in candidates] == [target]
        assert candidates[0].metadata["temporal_time_basis"] == "atom_event_time"
        assert other != target
        assert (
            await temporal.direct_time_candidates(
                engine,
                temporal.TimeRange(now - 60, now + 60),
                k=1,
                session_id="scope",
                persona_id="p",
                exclude_ids=[target],
            )
            == []
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_legacy_atom_table_without_event_time_keeps_parent_fallback(tmp_path):
    path = tmp_path / "legacy-event-column.db"
    now = time.time()
    async with aiosqlite.connect(path) as connection:
        await connection.execute(
            "CREATE TABLE documents(id INTEGER,text TEXT,metadata TEXT)"
        )
        await connection.execute(
            "CREATE TABLE memory_atoms(parent_memory_id INTEGER,status TEXT,"
            "expires_at REAL,session_id TEXT,persona_id TEXT)"
        )
        await connection.execute(
            "INSERT INTO documents VALUES(1,'legacy undated fact',json_object("
            "'session_id','scope','persona_id','p','create_time',?))",
            [now],
        )
        await connection.execute(
            "INSERT INTO memory_atoms VALUES(1,'active',?,'scope','p')",
            [now + 100],
        )
        await connection.commit()
    engine = SimpleNamespace(db_path=path, config={}, atom_enabled=True)
    candidates = await temporal.direct_time_candidates(
        engine,
        temporal.TimeRange(now - 60, now + 60),
        k=1,
        session_id="scope",
        persona_id="p",
    )
    assert [item.doc_id for item in candidates] == [1]
    assert candidates[0].content == "legacy undated fact"
