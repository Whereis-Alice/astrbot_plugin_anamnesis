"""Time-constrained recall uses current evidence across all retrieval routes."""

import time

import pytest
from astrbot_plugin_anamnesis.core.models.memory_atom import AtomType, MemoryAtom
from astrbot_plugin_anamnesis.core.recall_persona import NO_PERSONA
from astrbot_plugin_anamnesis.core.retrieval import temporal_retrieval as temporal

from tests.test_memory_transfer_runtime_safety import _engine


async def add_events(engine, now, offsets, *, types=None, persona="p", metadata=None):
    types = types or [AtomType.FACTUAL] * len(offsets)
    return await engine.add_memory(
        "tea events",
        session_id="scope",
        persona_id=persona,
        metadata={"create_time": now, "participants": ["Alice"], **(metadata or {})},
        atoms=[
            MemoryAtom(
                parent_memory_id=0,
                content=f"tea event {index}",
                atom_type=kind,
                event_time=None if offset is None else now + offset,
                expires_at=now + 172800,
                session_id="scope",
                persona_id=persona,
            )
            for index, (offset, kind) in enumerate(zip(offsets, types, strict=True))
        ],
        preserve_atom_lifecycle=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["historic", "future", "mixed"])
@pytest.mark.parametrize("mode", ["semantic", "time", "hybrid"])
@pytest.mark.parametrize("record_access", [False, True])
async def test_atom_event_time_overrides_conflicting_parent_time(
    tmp_path, case, mode, record_access
):
    engine = await _engine(tmp_path / "parent-conflict.db")
    engine.config.update(recent_memory_count=0)
    now = time.time()
    offsets = {"historic": [-2700], "future": [86400], "mixed": [-30, 86400]}[case]
    bounds = (-3600, -1800) if case == "historic" else (-60, 60)
    try:
        memory_id = await add_events(engine, now, offsets)
        original = await engine.get_memory(memory_id)
        results, _ = await temporal.search_with_time(
            engine,
            "tea",
            session_id="scope",
            persona_id="p",
            mode=mode,
            start_time=now + bounds[0],
            end_time=now + bounds[1],
            record_access=record_access,
        )
        if case == "future":
            assert results == []
        else:
            assert [result.doc_id for result in results] == [memory_id]
            assert results[0].content == "tea event 0"
            assert results[0].metadata["temporal_time_basis"] == "atom_event_time"
            assert len(results[0].metadata["retrieved_atom_ids"]) == 1
            assert (
                results[0].metadata["retrieved_atom_ids"]
                == results[0].metadata["temporal_atom_ids"]
            )
        assert (await engine.get_memory(memory_id))["metadata"][
            "atom_types"
        ] == original["metadata"]["atom_types"]
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_inside", [False, True])
@pytest.mark.parametrize("mode", ["semantic", "time", "hybrid"])
@pytest.mark.parametrize("record_access", [False, True])
async def test_multiple_in_window_atoms_determine_types_and_evidence(
    tmp_path, event_inside, mode, record_access
):
    engine = await _engine(tmp_path / "multi-evidence.db")
    engine.config.update(recent_memory_count=0, memory_type_filter="event_only")
    now = time.time()
    types = [
        AtomType.PREFERENCE,
        AtomType.FACTUAL if event_inside else AtomType.PREFERENCE,
        AtomType.PLANNED,
    ]
    try:
        memory_id = await add_events(engine, now, [-30, -20, 86400], types=types)
        results, _ = await temporal.search_with_time(
            engine,
            "tea",
            session_id="scope",
            persona_id="p",
            mode=mode,
            start_time=now - 60,
            end_time=now + 60,
            record_access=record_access,
        )
        if not event_inside:
            assert results == []
        else:
            assert [result.doc_id for result in results] == [memory_id]
            assert results[0].content.splitlines() == ["tea event 1", "tea event 0"]
            assert set(results[0].metadata["atom_types"]) == {"factual", "preference"}
            assert len(results[0].metadata["retrieved_atom_ids"]) == 2
            assert (
                results[0].metadata["retrieved_atom_ids"]
                == results[0].metadata["temporal_atom_ids"]
            )
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "speaker", "session", "persona"])
@pytest.mark.parametrize("mode", ["time", "hybrid"])
@pytest.mark.parametrize("record_access", [False, True])
async def test_final_projection_reloads_source_and_rejects_moved_authorization(
    tmp_path, monkeypatch, change, mode, record_access
):
    engine = await _engine(tmp_path / "source-change.db")
    engine.config.update(recent_memory_count=0)
    now = time.time()
    try:
        memory_id = await add_events(
            engine, now, [-2700], metadata={"source_session_id": "before"}
        )
        original = temporal.temporal_atom_evidence
        changed = False

        async def change_before_projection(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                patch = {
                    "source": {"source_session_id": "after"},
                    "speaker": {"participants": ["Bob"]},
                    "session": {"session_id": "other"},
                    "persona": {"persona_id": "other"},
                }[change]
                assert await engine.hybrid_retriever.update_metadata(memory_id, patch)
            return await original(*args, **kwargs)

        monkeypatch.setattr(
            temporal, "temporal_atom_evidence", change_before_projection
        )
        results, _ = await temporal.search_with_time(
            engine,
            "tea",
            session_id="scope",
            persona_id="p",
            speaker="Alice",
            mode=mode,
            start_time=now - 3600,
            end_time=now - 1800,
            record_access=record_access,
        )
        assert changed
        if change == "source":
            assert len(results) == 1
            assert results[0].metadata["source_session_id"] == "after"
        else:
            assert results == []
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["semantic", "time", "hybrid"])
@pytest.mark.parametrize("record_access", [False, True])
async def test_undated_atoms_keep_parent_time_fallback(tmp_path, mode, record_access):
    engine = await _engine(tmp_path / "undated.db")
    engine.config.update(recent_memory_count=0)
    now = time.time()
    try:
        memory_id = await add_events(engine, now, [None])
        results, _ = await temporal.search_with_time(
            engine,
            "tea",
            session_id="scope",
            persona_id="p",
            mode=mode,
            start_time=now - 60,
            end_time=now + 60,
            record_access=record_access,
        )
        assert [result.doc_id for result in results] == [memory_id]
        assert results[0].content == "tea event 0"
        assert results[0].metadata.get("temporal_time_basis") != "atom_event_time"
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["semantic", "time", "hybrid"])
@pytest.mark.parametrize("restriction", ["speaker", "exclude", "null_persona"])
async def test_conflicting_parent_time_does_not_relax_access_filters(
    tmp_path, mode, restriction
):
    engine = await _engine(tmp_path / "authorization.db")
    engine.config.update(recent_memory_count=0)
    now = time.time()
    try:
        memory_id = await add_events(engine, now, [-2700])
        options = {"persona_id": "p"}
        if restriction == "speaker":
            options["speaker"] = "Bob"
        elif restriction == "exclude":
            options["exclude_ids"] = [memory_id]
        else:
            options.update(persona_id=NO_PERSONA, persona_filtered=True)
        results, _ = await temporal.search_with_time(
            engine,
            "tea",
            session_id="scope",
            mode=mode,
            start_time=now - 3600,
            end_time=now - 1800,
            **options,
        )
        assert results == []
    finally:
        await engine.close()
