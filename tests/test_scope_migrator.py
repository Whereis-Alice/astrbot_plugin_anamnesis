"""Preview/CAS/backups for explicit scope migration across attached SQLite files."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.managers.scope_migrator import (
    ScopeMigrationError,
    ScopeMigrator,
)


async def _setup(tmp_path):
    main, graph = tmp_path / "memory.db", tmp_path / "graph.db"
    async with aiosqlite.connect(main) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY,text TEXT,metadata TEXT)")
        await db.execute("CREATE TABLE memory_atoms(id INTEGER PRIMARY KEY,parent_memory_id INTEGER,session_id TEXT,persona_id TEXT,metadata TEXT)")
        await db.execute("CREATE TABLE graph_entries(id INTEGER PRIMARY KEY,source_memory_id INTEGER,session_id TEXT,persona_id TEXT,metadata TEXT)")
        await db.execute("CREATE TABLE user_profiles(id INTEGER PRIMARY KEY,scope TEXT,profile TEXT)")
        await db.execute("INSERT INTO user_profiles VALUES(1,'source','personal profile')")
        documents = [(1, "source", "p"), (2, "source", "other"), (3, "target", "p")]
        for memory_id, scope, persona in documents:
            metadata = {"session_id": scope, "persona_id": persona, "status": "active", "unrelated": "preserve"}
            if memory_id == 1:
                metadata["archived_atom_lifecycle"] = [{
                    "id": 99, "content": "archived fact", "session_id": "source", "persona_id": "p",
                    "expires_at": 123, "metadata": "{}",
                }]
            await db.execute("INSERT INTO documents VALUES(?,?,?)", (memory_id, f"content-{memory_id}", json.dumps(metadata)))
            await db.execute("INSERT INTO memory_atoms VALUES(?,?,?,?,?)", (memory_id, memory_id, scope, persona, json.dumps({"session_id": scope, "persona_id": persona})))
            await db.execute("INSERT INTO graph_entries VALUES(?,?,?,?,?)", (memory_id, memory_id, scope, persona, json.dumps({"session_id": scope, "persona_id": persona})))
        await db.commit()
    async with aiosqlite.connect(graph) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY,text TEXT,metadata TEXT)")
        await db.executemany("INSERT INTO documents VALUES(?,?,?)", [
            (100+i, f"graph-{i}", json.dumps({"source_memory_id": i, "session_id": scope, "persona_id": persona}))
            for i, scope, persona in documents
        ])
        await db.commit()
    engine = SimpleNamespace(
        db_path=str(main), config={},
        faiss_db=SimpleNamespace(document_storage=SimpleNamespace(db_path=str(main))),
        graph_vector_db=SimpleNamespace(document_storage=SimpleNamespace(db_path=str(graph))),
        _invalidate_search_cache=Mock(),
    )
    return engine, ScopeMigrator(engine, tmp_path / "backups")


async def _metadata(path, memory_id=1):
    async with aiosqlite.connect(path) as db:
        return json.loads((await (await db.execute("SELECT metadata FROM documents WHERE id=?", (memory_id,))).fetchone())[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("source,target", [("", "target"), ("source", ""), ("source", "source"), (" source", "target")])
async def test_scope_migration_rejects_empty_same_or_whitespace_scopes(tmp_path, source, target):
    _, migrator = await _setup(tmp_path)
    with pytest.raises(ValueError):
        await migrator.preview(source, target)
    assert not migrator.backup_dir.exists()


@pytest.mark.asyncio
async def test_preview_is_read_only_and_apply_migrates_all_derived_scopes_with_backups(tmp_path):
    engine, migrator = await _setup(tmp_path)
    plan = await migrator.preview("source", "target", "p")
    assert plan["document_count"] == plan["atom_count"] == plan["graph_entry_count"] == plan["graph_vector_count"] == 1
    assert plan["target_document_count"] == 1
    assert plan["profile_scopes_changed"] is False
    assert not migrator.backup_dir.exists()
    assert (await _metadata(migrator.main_path))["session_id"] == "source"

    result = await asyncio.wait_for(migrator.apply("source", "target", plan["fingerprint"], "p"), 5)
    assert result["applied"] is True
    assert len(result["backup_paths"]) == 2
    document = await _metadata(migrator.main_path)
    assert document["session_id"] == "target"
    assert document["source_session_id"] == "source"
    assert document["persona_id"] == document["source_persona_id"] == "p"
    assert document["unrelated"] == "preserve"
    snapshot = document["archived_atom_lifecycle"][0]
    assert snapshot["session_id"] == "target"
    assert snapshot["expires_at"] == 123
    assert json.loads(snapshot["metadata"])["source_session_id"] == "source"
    assert (await _metadata(migrator.main_path, 2))["session_id"] == "source"
    assert (await _metadata(migrator.graph_path, 101))["session_id"] == "target"
    async with aiosqlite.connect(migrator.main_path) as db:
        for table in ("memory_atoms", "graph_entries"):
            assert (await (await db.execute(f"SELECT session_id FROM {table} WHERE id=1")).fetchone())[0] == "target"
        assert (await (await db.execute("SELECT scope FROM user_profiles")).fetchone())[0] == "source"
    assert (await _metadata(Path(result["backup_paths"][0])))["session_id"] == "source"
    assert (await _metadata(Path(result["backup_paths"][1]), 101))["session_id"] == "source"
    engine._invalidate_search_cache.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["graph", "target", "source"])
async def test_changed_preview_fingerprint_blocks_apply_before_backup(tmp_path, changed):
    engine, migrator = await _setup(tmp_path)
    plan = await migrator.preview("source", "target")
    path = migrator.graph_path if changed == "graph" else migrator.main_path
    memory_id = 101 if changed == "graph" else 3 if changed == "target" else 1
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE documents SET text='changed' WHERE id=?", (memory_id,))
        await db.commit()
    with pytest.raises(ValueError, match="stale"):
        await migrator.apply("source", "target", plan["fingerprint"])
    assert not migrator.backup_dir.exists()
    engine._invalidate_search_cache.assert_not_called()


@pytest.mark.asyncio
async def test_attached_graph_failure_rolls_back_main_and_preserves_verified_backups(tmp_path):
    engine, migrator = await _setup(tmp_path)
    async with aiosqlite.connect(migrator.graph_path) as db:
        await db.execute("CREATE TRIGGER reject_migration BEFORE UPDATE ON documents WHEN NEW.id=101 BEGIN SELECT RAISE(ABORT,'injected graph failure'); END")
        await db.commit()
    plan = await migrator.preview("source", "target")
    with pytest.raises(ScopeMigrationError, match="injected graph failure") as error:
        await migrator.apply("source", "target", plan["fingerprint"])
    assert len(error.value.backup_paths) == 2
    assert all(Path(path).is_file() for path in error.value.backup_paths)
    assert (await _metadata(migrator.main_path))["session_id"] == "source"
    assert (await _metadata(migrator.graph_path, 101))["session_id"] == "source"
    engine._invalidate_search_cache.assert_not_called()


@pytest.mark.asyncio
async def test_backup_only_preserves_scopes_and_fingerprint_is_bound_to_target(tmp_path):
    engine, migrator = await _setup(tmp_path)
    plan = await migrator.preview("source", "target")
    result = await migrator.backup("source", "target", plan["fingerprint"])
    assert result["applied"] is False
    assert len(result["backup_paths"]) == 2
    assert (await _metadata(migrator.main_path))["session_id"] == "source"
    with pytest.raises(ValueError, match="stale"):
        await migrator.apply("source", "different target", plan["fingerprint"])
    engine._invalidate_search_cache.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_apply_finishes_transaction_and_invalidates_cache(tmp_path, monkeypatch):
    engine, migrator = await _setup(tmp_path)
    plan = await migrator.preview("source", "target")
    entered, release = asyncio.Event(), asyncio.Event()
    update = migrator._update_rows

    async def gated_update(*args):
        entered.set()
        await release.wait()
        await update(*args)

    monkeypatch.setattr(migrator, "_update_rows", gated_update)
    task = asyncio.create_task(migrator.apply("source", "target", plan["fingerprint"]))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert (await _metadata(migrator.main_path))["session_id"] == "target"
        assert (await _metadata(migrator.graph_path, 101))["session_id"] == "target"
        engine._invalidate_search_cache.assert_called_once()
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
