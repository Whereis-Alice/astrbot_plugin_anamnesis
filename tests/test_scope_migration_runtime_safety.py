"""Scope migration must respect provenance and concurrent graph generations."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.managers.graph_memory_manager import (
    GraphMemoryManager,
)
from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_anamnesis.core.managers.memory_engine_batch import (
    MemoryEngineBatchMixin,
)
from astrbot_plugin_anamnesis.core.managers.scope_migrator import ScopeMigrator
from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom
from astrbot_plugin_anamnesis.core.processors.graph_extractor import GraphExtractor
from astrbot_plugin_anamnesis.core.retrieval.graph_vector_retriever import (
    GraphVectorRetriever,
)
from astrbot_plugin_anamnesis.storage.graph_store import GraphStore

from tests.test_graph_memory import _FakeFaissDB
from tests.test_scope_migrator import _metadata, _setup

PRIVATE = "qq:FriendMessage:123"
GROUP = "qq:GroupMessage:123"


async def _legacy_setup(tmp_path, source_anchor=PRIVATE, window_anchor=None):
    engine, migrator = await _setup(tmp_path)
    connection = await aiosqlite.connect(migrator.main_path)
    connection.row_factory = aiosqlite.Row
    engine.db_connection = connection
    await connection.execute(
        "CREATE TABLE migration_status(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)",
    )
    for memory_id in (1, 2):
        metadata = await _metadata(migrator.main_path, memory_id)
        metadata["session_id"] = "123"
        if memory_id == 1:
            if source_anchor is not None:
                metadata["source_session_id"] = source_anchor
            if window_anchor is not None:
                metadata["source_window"] = {"session_id": window_anchor}
        await connection.execute(
            "UPDATE documents SET metadata=? WHERE id=?",
            (json.dumps(metadata), memory_id),
        )
        for table in ("memory_atoms", "graph_entries"):
            await connection.execute(
                f"UPDATE {table} SET session_id='123',metadata=json_set("
                "metadata,'$.session_id','123') WHERE id=?", (memory_id,),
            )
    await connection.commit()
    async with aiosqlite.connect(migrator.graph_path) as db:
        await db.execute(
            "UPDATE documents SET metadata=json_set(metadata,'$.session_id','123') "
            "WHERE id IN (101,102)",
        )
        await db.commit()
    return engine, migrator


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor,window", [
    (PRIVATE, None), (None, None), (GROUP, PRIVATE),
])
async def test_runtime_migration_does_not_guess_or_override_source_chat(
    tmp_path, anchor, window,
):
    engine, migrator = await _legacy_setup(tmp_path, anchor, window)
    before = await _metadata(migrator.main_path)
    try:
        await MemoryEngineBatchMixin._migrate_session_data_if_needed(engine, GROUP)
        assert await _metadata(migrator.main_path) == before
        assert (await _metadata(migrator.main_path, 2))["session_id"] == "123"
        engine._invalidate_search_cache.assert_not_called()
    finally:
        await engine.db_connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor,window", [
    (PRIVATE, None), (None, PRIVATE), ("123", PRIVATE),
])
async def test_runtime_migration_moves_only_anchored_documents_and_all_derived_rows(
    tmp_path, anchor, window,
):
    engine, migrator = await _legacy_setup(tmp_path, anchor, window)
    unknown = await _metadata(migrator.main_path, 2)
    try:
        await MemoryEngineBatchMixin._migrate_session_data_if_needed(engine, PRIVATE)
        document = await _metadata(migrator.main_path)
        assert document["session_id"] == document["source_session_id"] == PRIVATE
        assert document["old_session_id"] == "123"
        assert await _metadata(migrator.main_path, 2) == unknown
        for table in ("memory_atoms", "graph_entries"):
            cursor = await engine.db_connection.execute(
                f"SELECT session_id,metadata FROM {table} WHERE id=1",
            )
            row = await cursor.fetchone()
            assert row[0] == json.loads(row[1])["session_id"] == PRIVATE
        assert (await _metadata(migrator.graph_path, 101))["session_id"] == PRIVATE
        assert (await _metadata(migrator.graph_path, 102))["session_id"] == "123"
        engine._invalidate_search_cache.assert_called_once()
    finally:
        await engine.db_connection.close()


@pytest.mark.asyncio
async def test_runtime_notice_is_limited_and_repaired_anchor_can_be_rechecked(
    tmp_path, caplog,
):
    engine, migrator = await _legacy_setup(tmp_path, source_anchor=None)
    try:
        for _ in range(3):
            await MemoryEngineBatchMixin._migrate_session_data_if_needed(engine, PRIVATE)
        notices = [record for record in caplog.records
                   if "缺少一致的会话来源" in record.getMessage()]
        assert len(notices) == 1
        await engine.db_connection.execute(
            "UPDATE documents SET metadata=json_set(metadata,'$.source_session_id',?) "
            "WHERE id=1", (PRIVATE,),
        )
        # Old releases permanently marked even an empty/ambiguous check. Such
        # a marker must not prevent correctly anchored later data from migrating.
        await engine.db_connection.execute(
            "INSERT INTO migration_status VALUES(?, 'true', 'old check')",
            (f"migrated_umo_{PRIVATE}",),
        )
        await engine.db_connection.commit()
        engine._legacy_scope_migration_checks[PRIVATE] = -float("inf")
        await MemoryEngineBatchMixin._migrate_session_data_if_needed(engine, PRIVATE)
        assert (await _metadata(migrator.main_path))["session_id"] == PRIVATE
        assert (await _metadata(migrator.main_path, 2))["session_id"] == "123"
    finally:
        await engine.db_connection.close()


@pytest.mark.asyncio
async def test_runtime_source_change_after_preview_preserves_new_metadata(
    tmp_path, monkeypatch,
):
    engine, migrator = await _legacy_setup(tmp_path)
    preview = ScopeMigrator.preview

    async def preview_then_change_source(self, *args, **kwargs):
        result = await preview(self, *args, **kwargs)
        await engine.db_connection.execute(
            "UPDATE documents SET metadata=json_set(metadata,'$.source_session_id',?,"
            "'$.status','deleted','$.access_count',9) WHERE id=1", (GROUP,),
        )
        await engine.db_connection.commit()
        return result

    monkeypatch.setattr(ScopeMigrator, "preview", preview_then_change_source)
    try:
        await MemoryEngineBatchMixin._migrate_session_data_if_needed(engine, PRIVATE)
        document = await _metadata(migrator.main_path)
        assert document["session_id"] == "123"
        assert document["source_session_id"] == GROUP
        assert document["status"] == "deleted" and document["access_count"] == 9
        assert not (migrator.main_path.parent / "scope_migration_backups").exists()
        engine._invalidate_search_cache.assert_not_called()
    finally:
        await engine.db_connection.close()


@pytest.mark.asyncio
async def test_anchored_selection_fingerprint_cannot_authorize_an_unfiltered_move(tmp_path):
    engine, migrator = await _legacy_setup(tmp_path)
    anchored = ScopeMigrator(engine, tmp_path / "anchored-backups", source_anchor=PRIVATE)
    try:
        plan = await anchored.preview("123", PRIVATE)
        assert plan["document_count"] == plan["unanchored_document_count"] == 1
        with pytest.raises(ValueError, match="stale"):
            await migrator.apply("123", PRIVATE, plan["fingerprint"])
        assert (await _metadata(migrator.main_path))["session_id"] == "123"
        assert not migrator.backup_dir.exists()
    finally:
        await engine.db_connection.close()


@pytest.mark.asyncio
async def test_runtime_backup_failure_preserves_rows_and_allows_retry(tmp_path, monkeypatch):
    engine, migrator = await _legacy_setup(tmp_path)
    backups = ScopeMigrator._backups

    async def fail_backup(self):
        raise RuntimeError("backup unavailable")

    monkeypatch.setattr(ScopeMigrator, "_backups", fail_backup)
    try:
        before = await _metadata(migrator.main_path)
        await MemoryEngineBatchMixin._migrate_session_data_if_needed(engine, PRIVATE)
        assert await _metadata(migrator.main_path) == before
        engine._invalidate_search_cache.assert_not_called()
        monkeypatch.setattr(ScopeMigrator, "_backups", backups)
        await MemoryEngineBatchMixin._migrate_session_data_if_needed(engine, PRIVATE)
        assert (await _metadata(migrator.main_path))["session_id"] == PRIVATE
        engine._invalidate_search_cache.assert_called_once()
    finally:
        await engine.db_connection.close()


async def _graph_setup(tmp_path):
    main, graph = tmp_path / "main.db", tmp_path / "vectors.db"
    for path in (main, graph):
        async with aiosqlite.connect(path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                "CREATE TABLE documents(id INTEGER PRIMARY KEY,doc_id TEXT,"
                "text TEXT,metadata TEXT)",
            )
            await db.commit()
    metadata = {
        "session_id": "source", "persona_id": "p", "key_facts": ["likes coffee"],
        "canonical_summary": "coffee",
    }
    async with aiosqlite.connect(main) as db:
        await db.execute(
            "INSERT INTO documents VALUES(1,'source-1','coffee',?)",
            (json.dumps(metadata),),
        )
        await db.commit()
    store = GraphStore(str(main))
    await store.initialize()
    vectors = _FakeFaissDB()
    vectors._sqlite_path = str(graph)
    vectors.document_storage.db_path = str(graph)
    manager = GraphMemoryManager(
        store, GraphVectorRetriever(vectors), GraphExtractor(),
    )
    await manager.index_memory(1, "coffee", metadata)
    engine = SimpleNamespace(
        db_path=str(main), config={}, graph_vector_db=vectors,
        faiss_db=SimpleNamespace(document_storage=SimpleNamespace(db_path=str(main))),
        graph_memory_manager=manager, _invalidate_search_cache=Mock(),
    )
    return ScopeMigrator(engine, tmp_path / "backups"), manager, store, metadata


@pytest.mark.asyncio
async def test_migration_rejects_active_shadow_graph_and_can_retry_after_switch(tmp_path):
    migrator, manager, store, metadata = await _graph_setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def batches():
        yield [(1, "coffee", metadata)]
        entered.set()
        await release.wait()

    rebuild = asyncio.create_task(manager.rebuild_memory_batches(batches()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        plan = await migrator.preview("source", "target")
        with pytest.raises(ValueError, match="graph rebuild"):
            await migrator.apply("source", "target", plan["fingerprint"])
        assert (await _metadata(migrator.main_path))["session_id"] == "source"
        assert not migrator.backup_dir.exists()
        release.set()
        await asyncio.wait_for(rebuild, 5)
        plan = await migrator.preview("source", "target")
        await migrator.apply("source", "target", plan["fingerprint"])
        async with aiosqlite.connect(migrator.main_path) as db:
            scopes = await (await db.execute(
                "SELECT DISTINCT session_id FROM graph_entries",
            )).fetchall()
        assert scopes == [("target",)]
        assert (await _metadata(migrator.main_path))["session_id"] == "target"
    finally:
        release.set()
        await asyncio.gather(rebuild, return_exceptions=True)
        await store.close()


@pytest.mark.asyncio
async def test_migration_waits_for_multistore_memory_write_to_finish(tmp_path, monkeypatch):
    path = tmp_path / "live-write.db"
    vectors = _FakeFaissDB()
    vectors._sqlite_path = str(path)
    vectors.document_storage.db_path = str(path)
    engine = MemoryEngine(
        str(path), vectors, config={"alias_backfill_on_start": False},
    )
    await engine.initialize()
    entered, release = asyncio.Event(), asyncio.Event()
    insert = engine.atom_store.insert_many

    async def gated_insert(atoms):
        entered.set()
        await release.wait()
        return await insert(atoms)

    monkeypatch.setattr(engine.atom_store, "insert_many", gated_insert)
    write = asyncio.create_task(engine.add_memory(
        "likes coffee", session_id="source", persona_id="p",
        atoms=[MemoryAtom(parent_memory_id=0, content="likes coffee")],
    ))
    migrator = ScopeMigrator(engine, tmp_path / "backups")
    try:
        await asyncio.wait_for(entered.wait(), 5)
        plan = await migrator.preview("source", "target")
        with pytest.raises(ValueError, match="memory write"):
            await migrator.apply("source", "target", plan["fingerprint"])
        assert (await _metadata(path))["session_id"] == "source"
        assert not migrator.backup_dir.exists()
        release.set()
        memory_id = await asyncio.wait_for(write, 5)
        plan = await migrator.preview("source", "target")
        await migrator.apply("source", "target", plan["fingerprint"])
        atom = (await engine.atom_store.get_by_parent(memory_id))[0]
        assert atom.session_id == (await _metadata(path))["session_id"] == "target"
    finally:
        release.set()
        await asyncio.gather(write, return_exceptions=True)
        await engine.close()
