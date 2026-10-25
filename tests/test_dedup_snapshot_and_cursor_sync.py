"""Exact dedup rollback, source flags and mutable cursor filter regressions."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_anamnesis.core.tools import agentic_recall
from astrbot_plugin_anamnesis.storage.atom_store import AtomStore

from tests.test_archive_access_integration import make_engine


async def _dedup_engine(path, old_source=None):
    engine = await make_engine(path)
    metadata = {
        "session_id": "qq:GroupMessage:1", "persona_id": "p", "status": "active",
        "key_facts": ["likes coffee"], "source_session_id": "qq:GroupMessage:1",
        "source_window": {"start_index": 0, "end_index": 2},
        "importance": .2, "create_time": 123.0,
        "has_source": bool(old_source), "source_message_count": len(old_source or []),
    }
    await engine.db_connection.execute(
        "UPDATE documents SET text=?,metadata=? WHERE id=1",
        ("likes coffee", json.dumps(metadata)),
    )
    await engine.db_connection.commit()
    if old_source:
        await engine.save_memory_source(1, old_source)
    engine.search_memories = AsyncMock(return_value=[HybridResult(
        1, .9, .1, .5, .9, "likes coffee", dict(metadata),
    )])
    engine.add_memory = AsyncMock(return_value=9)

    async def update_parent_metadata(memory_id, patch):
        stored = await engine.get_memory(memory_id)
        current = {**stored["metadata"], **patch}
        await engine.db_connection.execute(
            "UPDATE documents SET metadata=? WHERE id=?", (json.dumps(current), memory_id),
        )
        await engine.db_connection.commit()
        return True

    # The integration fake exposes SQLite reads, rather than the host's
    # SQLAlchemy session API. Persist the real update_memory patch in SQLite.
    engine.hybrid_retriever.update_metadata = AsyncMock(side_effect=update_parent_metadata)
    return engine, metadata


def _write_options(metadata, source):
    return {
        "content": "likes coffee", "session_id": metadata["session_id"],
        "persona_id": "p", "importance": .8, "source_messages": source,
        "metadata": {**metadata, "source_window": {"start_index": 2, "end_index": 4}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("old_source", [[], [{"content": "old source"}]])
async def test_dedup_updates_unique_source_flags_for_legacy_atomless_parent(tmp_path, old_source):
    engine, before = await _dedup_engine(tmp_path / "dedup.db", old_source)
    try:
        assert await engine.get_memory_atom_lifecycle(1) is None
        new_source = [*old_source, {"content": "new source"}]
        memory_id, previous = await engine.add_memory_conservatively(**_write_options(before, new_source))
        metadata = (await engine.get_memory(1))["metadata"]
        assert memory_id == 1 and previous["metadata"] == before
        assert previous["source_messages"] == old_source
        assert metadata["has_source"] is True
        assert metadata["source_message_count"] == len(old_source) + 1
        assert metadata["dedup_count"] == 1
        assert metadata["create_time"] == before["create_time"]
        assert await engine.get_memory_source(1) == new_source
        engine.add_memory.assert_not_awaited()
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_source", [[], [{"content": "old source"}]])
async def test_successful_dedup_undo_restores_exact_metadata_source_and_graph(tmp_path, old_source):
    engine, before = await _dedup_engine(tmp_path / "undo.db", old_source)
    engine.graph_memory_manager = SimpleNamespace(index_memory=AsyncMock())
    try:
        _, previous = await engine.add_memory_conservatively(
            **_write_options(before, [{"content": "new source"}]),
        )
        await engine.undo_summary_write(1, previous)
        assert (await engine.get_memory(1))["metadata"] == before
        assert await engine.get_memory_source(1) == old_source
        restored = engine.graph_memory_manager.index_memory.await_args.args
        assert restored == (1, "likes coffee", before, None)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_dedup_graph_undo_uses_original_atoms_without_refreshing_lifecycle(tmp_path):
    engine, before = await _dedup_engine(tmp_path / "atom_undo.db")
    engine.atom_store = AtomStore(engine.db_path)
    await engine.atom_store.initialize()
    atom = MemoryAtom(parent_memory_id=1, content="likes coffee")
    await engine.atom_store.insert(atom)
    expiry = time.time() + 5000
    await engine.db_connection.execute(
        "UPDATE memory_atoms SET expires_at=?,created_at=123,last_accessed_at=456 WHERE id=?",
        (expiry, atom.atom_id),
    )
    await engine.db_connection.commit()
    engine.graph_memory_manager = SimpleNamespace(index_memory=AsyncMock())
    try:
        _, previous = await engine.add_memory_conservatively(
            **_write_options(before, [{"content": "new source"}]),
        )
        await engine.undo_summary_write(1, previous)
        restored = (await engine.atom_store.get_by_parent(1))[0]
        assert restored.expires_at == expiry
        assert restored.created_at == 123 and restored.last_accessed_at == 456
        supplied_atoms = engine.graph_memory_manager.index_memory.await_args.args[3]
        assert len(supplied_atoms) == 1
        assert supplied_atoms[0].atom_id == atom.atom_id and supplied_atoms[0].expires_at == expiry
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_source_write_restores_exact_snapshot_after_committed_source(tmp_path, cancelled):
    old_source = [{"content": "old source"}]
    engine, before = await _dedup_engine(tmp_path / "failed.db", old_source)
    engine.graph_memory_manager = SimpleNamespace(index_memory=AsyncMock())
    save = engine.save_memory_source

    async def committed_then_failed(memory_id, messages):
        await save(memory_id, messages)
        if cancelled:
            raise asyncio.CancelledError
        raise OSError("injected source failure")

    engine.save_memory_source = committed_then_failed
    try:
        with pytest.raises(asyncio.CancelledError if cancelled else OSError):
            await engine.add_memory_conservatively(
                **_write_options(before, [{"content": "new source"}]),
            )
        assert (await engine.get_memory(1))["metadata"] == before
        assert await engine.get_memory_source(1) == old_source
        assert engine.graph_memory_manager.index_memory.await_args.args == (
            1, "likes coffee", before, None,
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_partially_expired_parent_receives_new_atoms_instead_of_dedup(tmp_path):
    engine, before = await _dedup_engine(tmp_path / "partial.db")
    engine.atom_store = AtomStore(engine.db_path)
    await engine.atom_store.initialize()
    atoms = [MemoryAtom(parent_memory_id=1, content=value) for value in ("likes coffee", "old appointment")]
    await engine.atom_store.insert_many(atoms)
    await engine.db_connection.execute(
        "UPDATE memory_atoms SET expires_at=? WHERE id=?", (time.time() - 1, atoms[1].atom_id),
    )
    await engine.db_connection.commit()
    try:
        memory_id, previous = await engine.add_memory_conservatively(
            **_write_options(before, [{"content": "repeated current facts"}]), atoms=atoms,
        )
        assert memory_id == 9 and previous is None
        assert engine.add_memory.await_args.kwargs["atoms"] == atoms
        assert (await engine.get_memory(1))["metadata"] == before
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_marked_atom_parent_with_missing_rows_is_not_deduplicated(tmp_path):
    engine, before = await _dedup_engine(tmp_path / "missing.db")
    before["atom_types"] = ["factual"]
    await engine.db_connection.execute("UPDATE documents SET metadata=? WHERE id=1", (json.dumps(before),))
    await engine.db_connection.commit()
    try:
        memory_id, previous = await engine.add_memory_conservatively(**_write_options(before, []))
        assert memory_id == 9 and previous is None
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["speaker", "time", "status", "scope"])
async def test_cursor_rechecks_current_filters_without_repeating_search(monkeypatch, changed):
    current = SimpleNamespace(unified_msg_origin="qq:GroupMessage:1")
    metadata = {
        "session_id": current.unified_msg_origin, "persona_id": "p", "status": "active",
        "participants": ["alice"], "create_time": 150,
    }
    documents = {i: {"text": "likes coffee", "metadata": dict(metadata)} for i in (1, 2)}
    memories = [HybridResult(i, .9, .1, .5, .9, "likes coffee", dict(metadata)) for i in (1, 2)]
    engine = SimpleNamespace(
        search_memories=AsyncMock(return_value=memories),
        get_memory=AsyncMock(side_effect=lambda memory_id: documents[memory_id]),
        resolve_live_facts=AsyncMock(return_value=["likes coffee"]),
    )
    tool = SimpleNamespace(
        context=object(), memory_engine=engine,
        config_manager=ConfigManager({"filtering_settings": {"memory_scope_mode": "session"}}),
    )
    monkeypatch.setattr(agentic_recall, "get_persona_id", AsyncMock(return_value="p"))
    filters = {"speaker": "alice", "start_time": 100, "end_time": 200}
    first = json.loads(await agentic_recall.agentic_search(tool, current, "coffee", 1, **filters))
    assert first["results"][0]["id"] == 1
    if changed == "speaker":
        documents[2]["metadata"]["participants"] = ["bob"]
    elif changed == "time":
        documents[2]["metadata"]["create_time"] = 300
    elif changed == "status":
        documents[2]["metadata"]["status"] = "archived"
    else:
        documents[2]["metadata"]["session_id"] = "qq:GroupMessage:other"
    second = json.loads(await agentic_recall.agentic_search(
        tool, current, "coffee", 1, cursor=first["next_cursor"], **filters,
    ))
    assert second["results"] == []
    engine.search_memories.assert_awaited_once()
    engine.resolve_live_facts.assert_awaited_once()


@pytest.mark.parametrize("metadata", [
    {"participants": ["Alice"]},
    {"participants": "Alice"},
    {"participant_identities": [{"sender_id": "Alice"}]},
    {"participant_identities": {"display_name": "Alice"}},
])
def test_speaker_filter_is_case_insensitive_and_supports_legacy_identity_forms(metadata):
    assert agentic_recall.speaker_matches(metadata, "alice")
    assert not agentic_recall.speaker_matches(metadata, "bob")
