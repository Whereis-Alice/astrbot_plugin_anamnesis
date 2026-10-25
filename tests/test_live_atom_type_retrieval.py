"""Retrieval policies use current live atom types across all recall paths."""

import time
from datetime import datetime, timezone
from types import MethodType
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.managers.memory_engine_write_ops import (
    MemoryEngineWriteOpsMixin,
)
from astrbot_plugin_anamnesis.core.models.memory_atom import AtomType, MemoryAtom
from astrbot_plugin_anamnesis.core.retrieval.temporal_retrieval import search_with_time

from tests.test_memory_transfer_runtime_safety import _engine
from tests.test_temporal_retrieval import _Engine, _put


async def typed_memory(
    engine, *, event_live=False, scope="scope", persona="p", event_type=AtomType.PLANNED
):
    now = time.time()
    return await engine.add_memory(
        "past appointment; likes tea",
        session_id=scope,
        persona_id=persona,
        metadata={"key_facts": ["past appointment", "likes tea"], "create_time": now},
        atoms=[
            MemoryAtom(
                parent_memory_id=0,
                content="past appointment",
                atom_type=event_type,
                session_id=scope,
                persona_id=persona,
                expires_at=now + 10 if event_live else now - 10,
            ),
            MemoryAtom(
                parent_memory_id=0,
                content="likes tea",
                atom_type=AtomType.PREFERENCE,
                session_id=scope,
                persona_id=persona,
                expires_at=now + 3600,
            ),
        ],
        preserve_atom_lifecycle=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type", [AtomType.PLANNED, AtomType.EPISODIC, AtomType.FACTUAL]
)
@pytest.mark.parametrize("event_live", [False, True])
async def test_only_live_event_types_qualify_for_event_retrieval(
    tmp_path, event_type, event_live
):
    engine = await _engine(tmp_path / "event-policy.db")
    engine.config.update(memory_type_filter="event_only", recent_memory_count=0)
    try:
        memory_id = await typed_memory(
            engine, event_live=event_live, event_type=event_type
        )
        results = await engine.search_memories(
            "tea", k=5, session_id="scope", persona_id="p"
        )
        if event_live:
            assert [item.doc_id for item in results] == [memory_id]
            assert results[0].metadata["atom_types"] == [event_type.value, "preference"]
        else:
            assert results == []
        # Retrieval metadata is a projection; stored evidence keeps its history.
        original = await engine.get_memory(memory_id)
        assert original["metadata"]["atom_types"] == [event_type.value, "preference"]
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["all", "event_only"])
async def test_cached_results_reclassify_live_types_after_expiry(
    tmp_path, monkeypatch, policy
):
    engine = await _engine(tmp_path / "cached-types.db")
    engine.config.update(memory_type_filter=policy, recent_memory_count=0)
    engine._search_cache_ttl = 60
    retriever_search = engine.hybrid_retriever.search
    search_spy = AsyncMock(wraps=retriever_search)
    monkeypatch.setattr(engine.hybrid_retriever, "search", search_spy)
    try:
        now = time.time()
        memory_id = await typed_memory(engine, event_live=True)
        first = await engine.search_memories(
            "tea", k=5, session_id="scope", persona_id="p"
        )
        assert [item.doc_id for item in first] == [memory_id]
        monkeypatch.setattr(time, "time", lambda: now + 20)
        second = await engine.search_memories(
            "tea", k=5, session_id="scope", persona_id="p"
        )
        assert search_spy.await_count == 1
        if policy == "all":
            assert second[0].content == "likes tea" and second[0].metadata[
                "atom_types"
            ] == ["preference"]
        else:
            assert second == []
        assert first[0].metadata["atom_types"] == ["planned", "preference"]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_expired_event_type_cannot_take_the_reserved_recent_slot(tmp_path):
    engine = await _engine(tmp_path / "recent-types.db")
    engine.config.update(memory_type_filter="event_only", recent_memory_count=1)
    try:
        valid = await engine.add_memory(
            "current coffee event",
            session_id="scope",
            persona_id="p",
            metadata={"create_time": time.time() - 50},
            atoms=[
                MemoryAtom(
                    parent_memory_id=0,
                    content="current coffee event",
                    atom_type=AtomType.FACTUAL,
                )
            ],
        )
        invalid = await typed_memory(engine, event_live=False)
        results = await engine.search_memories(
            "coffee", k=1, session_id="scope", persona_id="p"
        )
        assert [item.doc_id for item in results] == [valid] and invalid != valid
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["semantic", "time", "hybrid"])
@pytest.mark.parametrize("record_access", [False, True])
async def test_time_and_read_only_retrieval_use_live_type_policy(
    tmp_path, mode, record_access
):
    engine = await _engine(tmp_path / "time-types.db")
    engine.config.update(memory_type_filter="event_only", recent_memory_count=0)
    try:
        await typed_memory(engine, event_live=False)
        now = time.time()
        results, details = await search_with_time(
            engine,
            "tea",
            k=5,
            session_id="scope",
            persona_id="p",
            mode=mode,
            start_time=now - 100,
            end_time=now + 100,
            record_access=record_access,
            now=datetime.now(timezone.utc),
        )
        assert results == [] and details["mode"] == mode
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("atom_enabled", [False, True])
async def test_untyped_legacy_memory_still_qualifies_for_event_policy(
    tmp_path, atom_enabled
):
    engine = await _engine(tmp_path / "legacy-types.db")
    engine.atom_enabled = atom_enabled
    engine.config.update(memory_type_filter="event_only", recent_memory_count=0)
    try:
        memory_id = await engine.add_memory(
            "legacy tea", session_id="scope", persona_id="p"
        )
        results = await engine.search_memories(
            "tea", k=5, session_id="scope", persona_id="p"
        )
        assert [item.doc_id for item in results] == [memory_id]
        assert not results[0].metadata.get("atom_types")
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("inside_type", [AtomType.PREFERENCE, AtomType.FACTUAL])
@pytest.mark.parametrize("policy", ["all", "event_only"])
@pytest.mark.parametrize("mode", ["time", "hybrid"])
@pytest.mark.parametrize("record_access", [False, True])
async def test_time_window_classification_and_ids_use_only_selected_evidence(
    tmp_path, inside_type, policy, mode, record_access
):
    engine = await _engine(tmp_path / "window-types.db")
    engine.config.update(memory_type_filter=policy, recent_memory_count=0)
    now = time.time()
    try:
        memory_id = await engine.add_memory(
            "tomorrow appointment; selected tea fact",
            session_id="scope",
            persona_id="p",
            metadata={"create_time": now},
            atoms=[
                MemoryAtom(
                    parent_memory_id=0, content="tomorrow appointment",
                    atom_type=AtomType.PLANNED, session_id="scope", persona_id="p",
                    event_time=now + 86400, expires_at=now + 172800,
                ),
                MemoryAtom(
                    parent_memory_id=0, content="selected tea fact",
                    atom_type=inside_type, session_id="scope", persona_id="p",
                    event_time=now - 2700, expires_at=now + 3600,
                ),
            ],
            preserve_atom_lifecycle=True,
        )
        stored_types = (await engine.get_memory(memory_id))["metadata"]["atom_types"]
        assert set(stored_types) == {"planned", inside_type.value}
        rows = await (await engine.db_connection.execute(
            "SELECT id FROM memory_atoms WHERE parent_memory_id=? AND content=?",
            (memory_id, "selected tea fact"),
        )).fetchall()
        selected_ids = [row[0] for row in rows]
        results, details = await search_with_time(
            engine, "tea", k=5, session_id="scope", persona_id="p", mode=mode,
            start_time=now - 3600, end_time=now - 1800, record_access=record_access,
        )
        assert details["mode"] == mode
        if policy == "event_only" and inside_type == AtomType.PREFERENCE:
            assert results == []
        else:
            assert [result.doc_id for result in results] == [memory_id]
            assert results[0].content == "selected tea fact"
            assert results[0].metadata["atom_types"] == [inside_type.value]
            assert results[0].metadata["retrieved_atom_ids"] == selected_ids
            assert results[0].metadata["temporal_atom_ids"] == selected_ids
        original = await engine.get_memory(memory_id)
        assert original["metadata"]["atom_types"] == stored_types
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("type_column", [False, True])
@pytest.mark.parametrize("record_access", [False, True])
async def test_window_projection_preserves_missing_or_null_legacy_types(
    type_column, record_access
):
    async with aiosqlite.connect(":memory:") as connection:
        await connection.execute(
            "CREATE TABLE documents(id INTEGER PRIMARY KEY,text TEXT,metadata TEXT)"
        )
        await connection.execute(
            "CREATE TABLE memory_atoms(id INTEGER, parent_memory_id INTEGER, "
            "content TEXT,status TEXT,expires_at REAL,session_id TEXT,persona_id TEXT,"
            "event_time REAL" + (",atom_type TEXT" if type_column else "") + ")"
        )
        engine = _Engine(":memory:", connection)
        engine.config.update(memory_type_filter="event_only", recent_memory_count=0)
        engine._filter_by_retrieval_policy = MethodType(
            MemoryEngineWriteOpsMixin._filter_by_retrieval_policy, engine
        )
        now = time.time()
        await _put(engine, 1, instant=now)
        rows = [
            (1, 1, "outside fact", "active", now + 172800, "scope", "p", now + 86400),
            (
                2, 1, "selected legacy fact", "active", now + 3600,
                "scope", "p", now - 2700,
            ),
        ]
        if type_column:
            rows = [(*row, None) for row in rows]
        await connection.executemany(
            "INSERT INTO memory_atoms VALUES(" + ",".join("?" for _ in rows[0]) + ")",
            rows,
        )
        await connection.commit()
        results, _ = await search_with_time(
            engine, "", session_id="scope", persona_id="p", mode="time",
            start_time=now - 3600, end_time=now - 1800, record_access=record_access,
        )
        assert len(results) == 1 and results[0].content == "selected legacy fact"
        assert not results[0].metadata.get("atom_types")
        assert results[0].metadata["retrieved_atom_ids"] == [2]
        assert results[0].metadata["temporal_atom_ids"] == [2]
