"""Public writes, restore and startup repair share graph atom evidence policy."""

import json
import time

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_anamnesis.core.models.memory_atom import AtomStatus, MemoryAtom
from astrbot_plugin_anamnesis.storage.projection_outbox import ProjectionOutbox

from tests.test_memory_transfer_runtime_safety import _enable_graph, _engine


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["update", "rebuild", "projection"])
async def test_graph_has_no_summary_fallback_for_expired_atom_parent(tmp_path, phase):
    engine = await _engine(tmp_path / "expired-graph.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    engine.graph_memory_manager.atom_store = engine.atom_store
    projection = ProjectionOutbox(engine)
    await projection.initialize()
    try:
        memory_id = await engine.add_memory(
            "oldsecret",
            session_id="qq:GroupMessage:1",
            atoms=[
                MemoryAtom(
                    parent_memory_id=0,
                    content="oldsecret",
                    expires_at=time.time() - 3600,
                )
            ],
            preserve_atom_lifecycle=True,
        )
        if phase == "update":
            assert await engine.update_importance(memory_id, 0.9)
        elif phase == "rebuild":
            assert await engine.rebuild_graph_index() == {"rebuilt": 0, "skipped": 1}
        else:
            assert (await projection.repair())["repaired"] == 1
        assert await engine.graph_store.search_entries_by_bm25('"oldsecret"', 10) == []
        record = await engine.get_memory(memory_id)
        assert (
            await engine.resolve_live_facts(
                memory_id, record["text"], record["metadata"]
            )
            is None
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["add", "update", "rebuild"])
async def test_unatomized_legacy_source_still_builds_its_summary_graph(tmp_path, phase):
    engine = await _engine(tmp_path / "legacy-graph.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    engine.graph_memory_manager.atom_store = engine.atom_store
    try:
        memory_id = await engine.add_memory(
            "legacyfact", session_id="qq:GroupMessage:1"
        )
        if phase == "update":
            assert await engine.update_importance(memory_id, 0.9)
        elif phase == "rebuild":
            assert (await engine.rebuild_graph_index())["rebuilt"] == 1
        assert await engine.atom_store.get_by_parent(memory_id) == []
        assert await engine.graph_store.search_entries_by_bm25('"legacyfact"', 10)
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", [None, "[%None]", "selected"])
@pytest.mark.parametrize("phase", ["add", "restore", "projection", "update", "rebuild"])
async def test_graph_writes_keep_only_active_atoms_owned_by_parent(
    tmp_path, persona, phase
):
    engine = await _engine(tmp_path / "scoped-graph.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    engine.graph_memory_manager.atom_store = engine.atom_store
    projection = ProjectionOutbox(engine)
    await projection.initialize()
    scope = "qq:GroupMessage:1"
    matching = "[%None]" if persona is None else persona
    try:
        memory_id = await engine.add_memory(
            "all historical details",
            session_id=scope,
            persona_id=persona,
            atoms=[
                MemoryAtom(
                    parent_memory_id=0,
                    content="currentfact",
                    persona_id=matching,
                    expires_at=time.time() + 86400,
                ),
                MemoryAtom(
                    parent_memory_id=0,
                    content="otherpersona",
                    persona_id="other",
                    expires_at=time.time() + 86400,
                ),
                MemoryAtom(
                    parent_memory_id=0,
                    content="otherscope",
                    session_id="qq:GroupMessage:2",
                    persona_id=matching,
                    expires_at=time.time() + 86400,
                ),
                MemoryAtom(
                    parent_memory_id=0,
                    content="oldsecret",
                    expires_at=time.time() - 3600,
                ),
                MemoryAtom(
                    parent_memory_id=0,
                    content="forgottenfact",
                    status=AtomStatus.FORGOTTEN,
                    expires_at=time.time() + 86400,
                ),
            ],
            preserve_atom_lifecycle=True,
        )
        if phase == "restore":
            assert await engine.archive_memories([memory_id]) == 1
            assert await engine.restore_memory(memory_id)
        elif phase == "projection":
            # AtomStore's public writer preserves old omitted provenance.
            await engine.atom_store.insert(
                MemoryAtom(
                    parent_memory_id=memory_id,
                    content="legacyfact",
                    expires_at=time.time() + 86400,
                ),
                preserve_lifecycle=True,
            )
            result = await projection.repair()
            assert result == {"repaired": 1, "failed": [], "failed_count": 0}
            assert await engine.graph_store.search_entries_by_bm25('"legacyfact"', 10)
        elif phase == "update":
            assert await engine.update_memory(memory_id, {"importance": 0.9})
        elif phase == "rebuild":
            assert (await engine.rebuild_graph_index())["rebuilt"] == 1
        for excluded in ("otherpersona", "otherscope", "oldsecret", "forgottenfact"):
            assert (
                await engine.graph_store.search_entries_by_bm25(f'"{excluded}"', 10)
                == []
            )
        assert await engine.graph_store.search_entries_by_bm25('"currentfact"', 10)
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_live", [False, True])
async def test_restore_does_not_republish_expired_graph_atoms(tmp_path, has_live):
    engine = await _engine(tmp_path / "restore-graph.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    try:
        atoms = [
            MemoryAtom(
                parent_memory_id=0, content="oldsecret", expires_at=time.time() - 3600
            )
        ]
        if has_live:
            atoms.append(
                MemoryAtom(
                    parent_memory_id=0,
                    content="currentfact",
                    expires_at=time.time() + 86400,
                )
            )
        memory_id = await engine.add_memory(
            "oldsecret and currentfact",
            session_id="qq:GroupMessage:1",
            atoms=atoms,
            preserve_atom_lifecycle=True,
        )
        assert await engine.archive_memories([memory_id]) == 1
        assert await engine.graph_store.search_entries_by_bm25('"oldsecret"', 10) == []
        assert await engine.restore_memory(memory_id)
        record = await engine.get_memory(memory_id)
        assert await engine.resolve_live_facts(
            memory_id, record["text"], record["metadata"]
        ) == (["currentfact"] if has_live else None)
        stale = await engine.graph_store.search_entries_by_bm25('"oldsecret"', 10)
        assert stale == [], stale
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_restart_partial_atom_repair_keeps_all_saved_graph_facts(
    tmp_path, monkeypatch
):
    engine = await _engine(tmp_path / "partial-graph.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    insert = engine.atom_store.insert

    async def fail_batch(*args, **kwargs):
        raise RuntimeError("batch write unavailable")

    async def fail_second(atom, *args, **kwargs):
        if atom.content == "retryfact":
            raise RuntimeError("temporary atom write failure")
        return await insert(atom, *args, **kwargs)

    monkeypatch.setattr(engine.atom_store, "insert_many", fail_batch)
    monkeypatch.setattr(engine.atom_store, "insert", fail_second)
    try:
        memory_id = await engine.add_memory(
            "savedfact and retryfact",
            session_id="qq:GroupMessage:1",
            atoms=[
                MemoryAtom(
                    parent_memory_id=0,
                    content="savedfact",
                    expires_at=time.time() + 86400,
                ),
                MemoryAtom(
                    parent_memory_id=0,
                    content="retryfact",
                    expires_at=time.time() + 86400,
                ),
            ],
            preserve_atom_lifecycle=True,
        )
        assert [
            atom.content for atom in await engine.atom_store.get_by_parent(memory_id)
        ] == ["savedfact"]
        status = await (
            await engine.db_connection.execute(
                "SELECT status FROM memory_write_ops WHERE memory_id=?", (memory_id,)
            )
        ).fetchone()
        assert status[0] == "needs_repair"
    finally:
        await engine.close()

    async def documents(metadata_filters, ids=None, limit=50, offset=0):
        async with aiosqlite.connect(engine.db_path) as db:
            db.row_factory = aiosqlite.Row
            records = [
                dict(row)
                for row in await (
                    await db.execute("SELECT * FROM documents ORDER BY id")
                ).fetchall()
            ]
        for record in records:
            record["metadata"] = json.loads(record["metadata"])
        return [
            record
            for record in records
            if (ids is None or record["id"] in ids)
            and all(
                record["metadata"].get(key) == value
                for key, value in metadata_filters.items()
            )
        ][offset : offset + limit]

    engine.faiss_db.document_storage.get_documents = documents
    restarted = MemoryEngine(
        engine.db_path,
        engine.faiss_db,
        engine.graph_vector_db,
        config={"graph_memory_enabled": True, "alias_backfill_on_start": False},
    )
    try:
        await restarted.initialize()
        assert restarted.graph_memory_manager.atom_store is restarted.atom_store
        assert {
            atom.content for atom in await restarted.atom_store.get_by_parent(memory_id)
        } == {"savedfact", "retryfact"}
        assert await restarted.graph_store.search_entries_by_bm25('"savedfact"', 10)
        assert await restarted.graph_store.search_entries_by_bm25('"retryfact"', 10)
    finally:
        await restarted.close()
