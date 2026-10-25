"""Independent atom candidates and archive lifecycle preservation."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.managers.memory_engine_evidence import (
    MemoryEngineEvidenceMixin,
)
from astrbot_plugin_anamnesis.core.models.memory_atom import AtomType, MemoryAtom
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_anamnesis.storage.atom_store import AtomStore


@pytest.mark.asyncio
async def test_atom_candidates_enforce_parent_scope_persona_status_expiry_and_rank():
    async with aiosqlite.connect(":memory:") as db:
        await db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)")
        await db.execute("CREATE TABLE memory_atoms(id INTEGER, parent_memory_id INTEGER, content TEXT, status TEXT, expires_at REAL, session_id TEXT, persona_id TEXT)")
        await db.executemany("INSERT INTO memory_atoms VALUES(?,?,?,?,?,?,?)", [
            (2, 2, "independent live fact", "active", time.time()+1000, "group", "persona"),
            (6, 6, "expired fact", "active", 0, "group", "persona"),
        ])
        engine = MemoryEngineEvidenceMixin()
        engine.db_connection, engine.atom_enabled = db, True
        engine.atom_retriever = SimpleNamespace(search=AsyncMock(return_value=[
            SimpleNamespace(parent_memory_id=i, final_score=.95) for i in range(2, 7)
        ]))
        documents = [{"id": i, "text": f"original {i}", "metadata": {
            "session_id": "foreign" if i == 3 else "group",
            "persona_id": "foreign" if i == 4 else "persona",
            "status": "archived" if i == 5 else "active",
            "atom_types": ["factual"],
        }} for i in range(2, 7)]
        engine.faiss_db = SimpleNamespace(document_storage=SimpleNamespace(
            get_documents=AsyncMock(return_value=documents),
        ))
        existing = HybridResult(1, .8, .02, 1, .8, "existing", {"status": "active"})
        await db.executemany("INSERT INTO documents VALUES(?,?,?)", [
            (doc["id"], doc["text"], json.dumps(doc["metadata"])) for doc in documents
        ])
        await db.execute("INSERT INTO documents VALUES(?,?,?)", (1, existing.content, json.dumps(existing.metadata)))
        result = await engine.merge_atom_recall_candidates("fact", [existing], 5, "group", "persona")
        assert [r.doc_id for r in result] == [2, 1]
        assert result[0].content == "independent live fact"
        assert result[0].metadata["persona_summary"] == "independent live fact"
        engine.atom_retriever.search.assert_awaited_once_with("fact", 10, "group", "persona")


async def _archive_engine(path):
    store = AtomStore(str(path))
    await store.initialize()
    db = await aiosqlite.connect(path)
    engine = MemoryEngineEvidenceMixin()
    engine.db_connection, engine.atom_store, engine.atom_enabled = db, store, True
    return engine, store, db


@pytest.mark.asyncio
async def test_archive_restore_preserves_expired_fact_times_instead_of_refreshing_ttl(tmp_path):
    engine, store, db = await _archive_engine(tmp_path / "archive.db")
    try:
        atom = MemoryAtom(parent_memory_id=1, atom_type=AtomType.FACTUAL, content="past fact", session_id="group", persona_id="persona")
        await store.insert(atom)
        original_expiry = time.time()-86400
        await db.execute("UPDATE memory_atoms SET expires_at=?,created_at=123,last_accessed_at=456,reinforcement_count=2 WHERE id=?", (original_expiry, atom.atom_id))
        await db.commit()
        snapshots = await engine.snapshot_archived_atom_lifecycle(1)
        await store.delete_by_parent(1)
        await store.insert(MemoryAtom(parent_memory_id=1, content="freshly reconstructed", session_id="group", persona_id="persona"))

        assert await engine.restore_archived_atom_lifecycle(1, snapshots, session_id="group", persona_id="persona") == 1
        restored = (await store.get_by_parent(1))[0]
        assert restored.content == "past fact"
        assert restored.expires_at == original_expiry
        assert restored.created_at == 123
        assert restored.last_accessed_at == 456
        assert restored.reinforcement_count == 2
        assert await engine.resolve_live_facts(1, "original summary", {
            "atom_types": ["factual"], "session_id": "group", "persona_id": "persona",
        }) is None
        assert await store.search_fts("fact") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_empty_archive_snapshot_removes_newly_reconstructed_atoms(tmp_path):
    engine, store, db = await _archive_engine(tmp_path / "empty.db")
    try:
        await store.insert(MemoryAtom(parent_memory_id=1, content="should not resurrect"))
        assert await engine.restore_archived_atom_lifecycle(1, []) == 0
        assert await store.get_by_parent(1) == []
        assert await store.search_fts("resurrect") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_archive_restore_rolls_back_both_source_and_fts(tmp_path):
    engine, store, db = await _archive_engine(tmp_path / "rollback.db")
    try:
        original = MemoryAtom(parent_memory_id=1, content="existing source")
        await store.insert(original)
        await db.execute("CREATE TRIGGER reject_bad BEFORE INSERT ON memory_atoms WHEN NEW.content='bad snapshot' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        await db.commit()
        snapshots = [{"content": "good snapshot"}, {"content": "bad snapshot"}]
        with pytest.raises(aiosqlite.IntegrityError, match="injected failure"):
            await engine.restore_archived_atom_lifecycle(1, snapshots)
        assert [atom.content for atom in await store.get_by_parent(1)] == ["existing source"]
        assert [atom.content for atom in await store.search_fts("source")] == ["existing source"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_archive_restore_waits_for_concurrent_atom_writer(tmp_path):
    engine, store, db = await _archive_engine(tmp_path / "writer.db")
    restoration = None
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await store.insert(MemoryAtom(parent_memory_id=1, content="existing source"))
        snapshots = await engine.snapshot_archived_atom_lifecycle(1)
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("UPDATE memory_atoms SET reinforcement_count=7 WHERE parent_memory_id=1")
        restoration = asyncio.create_task(engine.restore_archived_atom_lifecycle(1, snapshots))
        # SQLite's busy handler must wait for the writer, instead of failing
        # immediately while upgrading an FTS read snapshot to a transaction.
        await asyncio.sleep(0.05)
        await db.commit()
        assert await asyncio.wait_for(restoration, timeout=3) == 1
        assert [atom.content for atom in await store.get_by_parent(1)] == ["existing source"]
        assert [atom.content for atom in await store.search_fts("source")] == ["existing source"]
    finally:
        await db.rollback()
        if restoration is not None and not restoration.done():
            restoration.cancel()
            await asyncio.gather(restoration, return_exceptions=True)
        await db.close()


@pytest.mark.asyncio
async def test_archive_restore_fallback_preserves_callers_transaction(tmp_path):
    engine, store, db = await _archive_engine(tmp_path / "outer.db")
    try:
        await store.insert(MemoryAtom(parent_memory_id=1, content="existing source"))
        engine.atom_store = None
        await db.execute("CREATE TABLE caller_work(value TEXT)")
        await db.commit()
        await db.execute("INSERT INTO caller_work VALUES('uncommitted')")
        assert await engine.restore_archived_atom_lifecycle(1, [{"content": "temporary source"}]) == 1
        assert db.in_transaction
        await db.rollback()
        async with db.execute("SELECT COUNT(*) FROM caller_work") as cursor:
            assert (await cursor.fetchone())[0] == 0
        assert [atom.content for atom in await store.get_by_parent(1)] == ["existing source"]
    finally:
        await db.close()
