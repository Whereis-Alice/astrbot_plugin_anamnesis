"""Disabling atomization uses document graph semantics across reload and writes."""

import json
import sqlite3
import time
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom
from astrbot_plugin_anamnesis.storage.projection_outbox import ProjectionOutbox

from tests.test_memory_transfer_runtime_safety import _enable_graph, _engine


async def restart(engine, config):
    async def documents(metadata_filters, ids=None, limit=50, offset=0):
        async with aiosqlite.connect(engine.db_path) as db:
            db.row_factory = aiosqlite.Row
            records = [dict(row) for row in await (await db.execute("SELECT * FROM documents ORDER BY id")).fetchall()]
        for record in records:
            record["metadata"] = json.loads(record["metadata"])
        return [record for record in records if (ids is None or record["id"] in ids)
                and all(record["metadata"].get(key) == value for key, value in metadata_filters.items())][offset:offset+limit]

    engine.faiss_db.document_storage.get_documents = documents
    result = MemoryEngine(
        engine.db_path, engine.faiss_db, engine.graph_vector_db,
        config={"graph_memory_enabled": True, "alias_backfill_on_start": False, **config},
    )
    await result.initialize()

    async def patch_metadata(memory_id, patch):
        row = await (await result.db_connection.execute("SELECT metadata FROM documents WHERE id=?", (memory_id,))).fetchone()
        if row is None:
            return False
        metadata = {**json.loads(row[0]), **patch}
        await result.db_connection.execute("UPDATE documents SET metadata=? WHERE id=?", (json.dumps(metadata), memory_id))
        await result.db_connection.commit()
        result.faiss_db.docs[memory_id]["metadata"] = metadata
        return True

    result.hybrid_retriever.update_metadata = AsyncMock(side_effect=patch_metadata)
    return result


async def saved_source(engine, atomized):
    atoms = [
        MemoryAtom(parent_memory_id=0, content="oldsecret", expires_at=time.time()-3600),
        MemoryAtom(parent_memory_id=0, content="currentfact", expires_at=time.time()+86400),
    ] if atomized else None
    return await engine.add_memory(
        "oldsecret and currentfact", session_id="qq:GroupMessage:1",
        metadata={"key_facts": ["oldsecret", "currentfact"], "canonical_summary": "oldsecret and currentfact"},
        atoms=atoms, preserve_atom_lifecycle=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["atom_enabled", "graph_memory_atom_enabled"])
@pytest.mark.parametrize("atomized", [False, True])
@pytest.mark.parametrize("phase", ["update", "rebuild", "projection", "restore", "retry"])
async def test_disabled_atomization_uses_full_document_graph_after_restart(
    tmp_path, monkeypatch, switch, atomized, phase
):
    engine = await _engine(tmp_path / "switch.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    projection = ProjectionOutbox(engine)
    await projection.initialize()
    try:
        if phase == "retry":
            monkeypatch.setattr(engine.graph_memory_manager, "index_memory", AsyncMock(side_effect=RuntimeError("temporary graph write failure")))
        memory_id = await saved_source(engine, atomized)
        before = {atom.content: atom.expires_at for atom in await engine.atom_store.get_by_parent(memory_id)}
        if phase == "restore":
            assert await engine.archive_memories([memory_id]) == 1
        if phase == "retry":
            status = await (await engine.db_connection.execute("SELECT status FROM memory_write_ops WHERE memory_id=?", (memory_id,))).fetchone()
            assert status[0] == "needs_repair"
    finally:
        await engine.close()

    disabled = await restart(engine, {switch: False})
    try:
        assert not disabled.atom_enabled and not disabled.graph_memory_manager.atom_enabled
        assert disabled.atom_lifecycle_manager is None
        if phase == "update":
            assert await disabled.update_importance(memory_id, 0.9)
        elif phase == "rebuild":
            assert (await disabled.rebuild_graph_index())["rebuilt"] == 1
        elif phase == "projection":
            projection = ProjectionOutbox(disabled)
            assert (await projection.repair())["repaired"] == 1
        elif phase == "restore":
            assert await disabled.restore_memory(memory_id)
        else:
            status = await (await disabled.db_connection.execute("SELECT status FROM memory_write_ops WHERE memory_id=?", (memory_id,))).fetchone()
            assert status[0] == "completed"
        record = await disabled.get_memory(memory_id)
        assert await disabled.resolve_live_facts(memory_id, record["text"], record["metadata"]) == ["oldsecret and currentfact"]
        assert await disabled.graph_store.search_entries_by_bm25('"currentfact"', 10)
        assert await disabled.graph_store.search_entries_by_bm25('"oldsecret"', 10)
        after = {atom.content: atom.expires_at for atom in await disabled.atom_store.get_by_parent(memory_id)}
        assert after == before
    finally:
        await disabled.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["update", "rebuild", "projection"])
async def test_disabled_document_graph_does_not_depend_on_atom_reader(tmp_path, monkeypatch, phase):
    engine = await _engine(tmp_path / "reader.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    await ProjectionOutbox(engine).initialize()
    try:
        memory_id = await saved_source(engine, True)
    finally:
        await engine.close()
    disabled = await restart(engine, {"atom_enabled": False})
    try:
        reader = AsyncMock(side_effect=sqlite3.OperationalError("atom reader unavailable"))
        monkeypatch.setattr(disabled.atom_store, "get_by_parent", reader)
        if phase == "update":
            assert await disabled.update_importance(memory_id, 0.9)
        elif phase == "rebuild":
            assert (await disabled.rebuild_graph_index())["rebuilt"] == 1
        else:
            assert (await ProjectionOutbox(disabled).repair())["repaired"] == 1
        reader.assert_not_awaited()
        assert await disabled.graph_store.search_entries_by_bm25('"oldsecret"', 10)
    finally:
        await disabled.close()


@pytest.mark.asyncio
async def test_disabled_graph_ignores_explicit_atoms_but_keeps_parent_status(tmp_path):
    engine = await _engine(tmp_path / "passed.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    try:
        memory_id = await saved_source(engine, True)
    finally:
        await engine.close()
    disabled = await restart(engine, {"atom_enabled": False})
    try:
        record = await disabled.get_memory(memory_id)
        atoms = await disabled.atom_store.get_by_parent(memory_id)
        await disabled.graph_memory_manager.index_memory(memory_id, record["text"], record["metadata"], atoms)
        assert await disabled.graph_store.search_entries_by_bm25('"oldsecret"', 10)
        assert await disabled.archive_memories([memory_id]) == 1
        record = await disabled.get_memory(memory_id)
        await disabled.graph_memory_manager.index_memory(memory_id, record["text"], record["metadata"], atoms)
        assert await disabled.graph_store.search_entries_by_bm25('"oldsecret"', 10) == []
    finally:
        await disabled.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["atom_enabled", "graph_memory_atom_enabled"])
async def test_reenabled_atomization_keeps_original_ttl_and_graph_filter(tmp_path, switch):
    engine = await _engine(tmp_path / "reenable.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    try:
        memory_id = await saved_source(engine, True)
        before = {atom.content: atom.expires_at for atom in await engine.atom_store.get_by_parent(memory_id)}
    finally:
        await engine.close()
    disabled = await restart(engine, {switch: False})
    try:
        assert await disabled.archive_memories([memory_id]) == 1
        assert await disabled.restore_memory(memory_id)
        assert (await disabled.rebuild_graph_index())["rebuilt"] == 1
        assert await disabled.graph_store.search_entries_by_bm25('"oldsecret"', 10)
    finally:
        await disabled.close()
    enabled = await restart(disabled, {switch: True})
    await enabled.atom_lifecycle_manager.stop()
    try:
        assert enabled.atom_enabled and enabled.graph_memory_manager.atom_enabled
        assert (await enabled.rebuild_graph_index())["rebuilt"] == 1
        assert await enabled.graph_store.search_entries_by_bm25('"oldsecret"', 10) == []
        assert await enabled.graph_store.search_entries_by_bm25('"currentfact"', 10)
        assert {atom.content: atom.expires_at for atom in await enabled.atom_store.get_by_parent(memory_id)} == before
    finally:
        await enabled.close()
