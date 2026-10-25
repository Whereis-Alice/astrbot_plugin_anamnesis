"""Live facts, legacy FTS repair and cached graph reader regression coverage."""

import asyncio
import json
import time
from types import SimpleNamespace

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.managers.memory_engine_evidence import (
    MemoryEngineEvidenceMixin,
)
from astrbot_plugin_anamnesis.core.models.memory_atom import AtomType, MemoryAtom
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_anamnesis.core.utils.formatting import (
    format_memories_for_fake_tool_call,
    format_memories_for_injection,
)
from astrbot_plugin_anamnesis.storage.atom_store import AtomStore
from astrbot_plugin_anamnesis.storage.graph_store import GraphStore


def _result(memory_id=1, **metadata):
    return HybridResult(
        doc_id=memory_id, final_score=0.8, rrf_score=0.02,
        bm25_score=1.0, vector_score=0.8, content="expired appointment; likes tea",
        metadata={"status": "active", "atom_types": ["planned", "factual"],
                  "persona_summary": "expired appointment", "key_facts": ["expired appointment"],
                  "topics": ["expired appointment"], "time_tags": ["expired appointment"],
                  **metadata},
    )


async def _evidence_engine(db):
    await db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)")
    await db.execute(
        "CREATE TABLE memory_atoms(id INTEGER, parent_memory_id INTEGER, content TEXT, "
        "status TEXT, expires_at REAL, session_id TEXT, persona_id TEXT)"
    )
    engine = MemoryEngineEvidenceMixin()
    engine.db_connection = db
    engine.atom_enabled = True
    return engine


async def _store_documents(db, *results):
    await db.executemany("INSERT INTO documents VALUES(?,?,?)", [
        (result.doc_id, result.content, json.dumps(result.metadata)) for result in results
    ])


@pytest.mark.asyncio
async def test_partial_expiry_updates_every_injected_fact_field_without_mutating_cache():
    async with aiosqlite.connect(":memory:") as db:
        engine = await _evidence_engine(db)
        await db.executemany("INSERT INTO memory_atoms VALUES(?,?,?,?,?,?,?)", [
            (1, 1, "expired appointment", "active", time.time()-1, "group", "persona"),
            (2, 1, "likes tea", "active", time.time()+1000, "group", "persona"),
            (3, 1, "foreign group secret", "active", time.time()+1000, "other", "persona"),
        ])
        original = _result(session_id="group", persona_id="persona")
        await _store_documents(db, original)
        results = await engine.filter_recall_lifecycle([original])
        assert results[0].content == "likes tea"
        assert results[0].metadata["persona_summary"] == "likes tea"
        assert results[0].metadata["canonical_summary"] == "likes tea"
        assert results[0].metadata["key_facts"] == ["likes tea"]
        assert results[0].metadata["retrieved_atom_ids"] == [2]
        assert original.metadata["persona_summary"] == "expired appointment"
        assert await engine.resolve_live_facts(1, original.content, original.metadata) == ["likes tea"]
        for rendered in (
            format_memories_for_injection(results),
            json.dumps(format_memories_for_fake_tool_call(results, "tea")),
        ):
            assert "expired appointment" not in rendered
            assert "foreign group secret" not in rendered
            assert "likes tea" in rendered


@pytest.mark.asyncio
async def test_fully_expired_and_missing_atom_parents_are_hidden_but_legacy_rows_work():
    async with aiosqlite.connect(":memory:") as db:
        engine = await _evidence_engine(db)
        await db.execute("INSERT INTO memory_atoms VALUES(1,1,'expired','expired',0,NULL,NULL)")
        expired, missing = _result(1), _result(2)
        legacy = _result(3, atom_types=[], persona_summary="legacy")
        archived = _result(4, status="archived", atom_types=[])
        await _store_documents(db, expired, missing, legacy, archived)
        assert await engine.filter_recall_lifecycle([expired, missing, legacy, archived]) == [legacy]
        assert await engine.resolve_live_facts(1, "expired", expired.metadata) is None
        assert await engine.resolve_live_facts(2, "missing", missing.metadata) is None
        assert await engine.get_memory_atom_lifecycle(1) == {"total": 1, "active": 0, "hidden": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("atom_enabled", [True, False])
async def test_cached_legacy_results_recheck_current_archive_and_document_existence(atom_enabled):
    async with aiosqlite.connect(":memory:") as db:
        engine = await _evidence_engine(db)
        engine.atom_enabled = atom_enabled
        archived, removed, live = [_result(i, atom_types=[]) for i in (1, 2, 3)]
        await _store_documents(db, archived, removed, live)
        await db.execute("UPDATE documents SET metadata=? WHERE id=1", (json.dumps({"status": "archived"}),))
        await db.execute("DELETE FROM documents WHERE id=2")

        result = await engine.filter_recall_lifecycle([archived, removed, live])

        assert [item.doc_id for item in result] == [3]
        assert archived.metadata["status"] == "active"
        # Skipping an already resolved atom must never skip its parent guard.
        assert await engine.filter_recall_lifecycle([archived, removed], skip_doc_ids={1, 2}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_key", ["session_id", "persona_id"])
async def test_cached_parent_cannot_follow_scope_migration(scope_key):
    async with aiosqlite.connect(":memory:") as db:
        engine = await _evidence_engine(db)
        original = _result(atom_types=[], session_id="group", persona_id="persona")
        await _store_documents(db, original)
        metadata = {**original.metadata, scope_key: "moved"}
        await db.execute("UPDATE documents SET metadata=? WHERE id=1", (json.dumps(metadata),))
        assert await engine.filter_recall_lifecycle([original]) == []


@pytest.mark.asyncio
async def test_cached_legacy_parent_uses_current_content_and_display_metadata():
    async with aiosqlite.connect(":memory:") as db:
        engine = await _evidence_engine(db)
        original = _result(atom_types=[])
        await _store_documents(db, original)
        current = {"status": "active", "key_facts": ["current fact"], "persona_summary": "current fact"}
        await db.execute("UPDATE documents SET text=?,metadata=? WHERE id=1", ("current fact", json.dumps(current)))
        result = (await engine.filter_recall_lifecycle([original]))[0]
        assert result.content == "current fact"
        assert result.metadata == current
        assert original.content == "expired appointment; likes tea"
        assert original.metadata["persona_summary"] == "expired appointment"


@pytest.mark.asyncio
async def test_parent_guard_batches_and_deduplicates_document_ids():
    async with aiosqlite.connect(":memory:") as db:
        engine = await _evidence_engine(db)
        engine.atom_enabled = False
        results = [_result(i, atom_types=[]) for i in range(1, 502)]
        await _store_documents(db, *results)
        statements = []
        await db.set_trace_callback(statements.append)
        assert len(await engine.filter_recall_lifecycle([*results, results[0]])) == 502
        queries = [sql for sql in statements if sql.startswith("SELECT id, text, metadata FROM documents")]
        assert len(queries) == 2


@pytest.mark.asyncio
async def test_uninitialized_legacy_fixture_keeps_status_filter_without_sql():
    engine = MemoryEngineEvidenceMixin()
    engine.db_connection = None
    live, archived = _result(1), _result(2, status="archived")
    assert await engine.filter_recall_lifecycle([live, archived]) == [live]


@pytest.mark.asyncio
async def test_lifecycle_sql_failure_propagates_instead_of_injecting_unchecked_facts():
    engine = MemoryEngineEvidenceMixin()
    engine.db_connection = SimpleNamespace(execute=lambda *args: (_ for _ in ()).throw(RuntimeError("broken")))
    with pytest.raises(RuntimeError, match="broken"):
        await engine.filter_recall_lifecycle([_result()])


@pytest.mark.asyncio
async def test_atom_fts_repairs_equal_count_stale_and_missing_rows_without_forgotten(tmp_path):
    path = tmp_path / "atoms.db"
    store = AtomStore(str(path))
    await store.initialize()
    atoms = [MemoryAtom(parent_memory_id=i, atom_type=AtomType.FACTUAL, content=f"tea {i}")
             for i in range(1, 4)]
    await store.insert_many(atoms)
    async with aiosqlite.connect(path) as db:
        await db.execute("DELETE FROM memory_atoms_fts WHERE atom_id=?", (atoms[0].atom_id,))
        await db.execute("UPDATE memory_atoms_fts SET content='stale' WHERE atom_id=?", (atoms[1].atom_id,))
        await db.execute("INSERT INTO memory_atoms_fts(atom_id,content) VALUES(999,'orphan')")
        await db.execute("UPDATE memory_atoms SET status='forgotten' WHERE id=?", (atoms[2].atom_id,))
        await db.commit()
    await store.initialize()
    async with aiosqlite.connect(path) as db:
        rows = await (await db.execute("SELECT atom_id,content FROM memory_atoms_fts ORDER BY atom_id")).fetchall()
    assert rows == [(atoms[0].atom_id, "tea 1"), (atoms[1].atom_id, "tea 2")]
    assert {a.parent_memory_id for a in await store.search_fts("tea")} == {1, 2}
    await store.initialize()
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM memory_atoms_fts")).fetchone())[0] == 2


@pytest.mark.asyncio
async def test_graph_read_connection_is_reused_query_only_and_closed_after_reader(tmp_path):
    store = GraphStore(str(tmp_path / "graph.db"), cache_reads=True)
    await store.initialize()
    await asyncio.gather(*(store.get_memory_entry_stats() for _ in range(5)))
    connection = store._read_conn
    assert connection is not None
    await store.get_memory_entry_stats()
    assert store._read_conn is connection
    with pytest.raises(aiosqlite.OperationalError, match="readonly"):
        await connection.execute("DELETE FROM graph_nodes")
    entered, release = asyncio.Event(), asyncio.Event()

    async def reader():
        async with store._read_connect():
            entered.set()
            await release.wait()

    reading = asyncio.create_task(reader())
    await entered.wait()
    closing = asyncio.create_task(store.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await reading
    await closing
    assert store._read_conn is None
    with pytest.raises(RuntimeError, match="closed"):
        await store.get_memory_entry_stats()
    await store.close()


@pytest.mark.asyncio
async def test_cancelled_first_graph_read_finishes_open_and_can_be_closed(tmp_path, monkeypatch):
    store = GraphStore(str(tmp_path / "opening.db"), cache_reads=True)
    await store.initialize()
    entered, release = asyncio.Event(), asyncio.Event()
    open_connection = store._open_read_connection

    async def gated_open():
        entered.set()
        await release.wait()
        await open_connection()

    monkeypatch.setattr(store, "_open_read_connection", gated_open)
    task = asyncio.create_task(store.get_memory_entry_stats())
    try:
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert store._read_conn is not None
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
        await store.close()
    assert store._read_conn is None
