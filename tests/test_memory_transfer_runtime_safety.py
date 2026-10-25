"""Portable transfers must preserve lifecycle and stay safe when retried."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.managers.graph_memory_manager import (
    GraphMemoryManager,
)
from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_anamnesis.core.memory_transfer import (
    parse_transfer_content,
    serialize_transfer_csv,
    serialize_transfer_json,
)
from astrbot_plugin_anamnesis.core.models.memory_atom import AtomType, MemoryAtom
from astrbot_plugin_anamnesis.core.page_api import PluginPageApi
from astrbot_plugin_anamnesis.core.processors.atom_classifier import classify_atoms
from astrbot_plugin_anamnesis.core.processors.graph_extractor import GraphExtractor
from astrbot_plugin_anamnesis.core.retrieval.graph_vector_retriever import (
    GraphVectorRetriever,
)
from astrbot_plugin_anamnesis.storage.graph_store import GraphStore

from tests.test_graph_memory import _FakeFaissDB
from tests.test_page_api import FakePlugin, _mock_page_request, _patch_page_request


async def _engine(path):
    vectors = _FakeFaissDB()
    vectors._sqlite_path = str(path)
    vectors.document_storage.db_path = str(path)
    vectors.embedding_storage = SimpleNamespace(insert=AsyncMock(), delete=AsyncMock())
    vectors.embedding_provider = SimpleNamespace(
        get_embedding=AsyncMock(return_value=[1.0, 0.0])
    )
    engine = MemoryEngine(str(path), vectors, config={"alias_backfill_on_start": False})
    await engine.initialize()

    async def get_documents(metadata_filters, ids=None, limit=50, offset=0):
        rows = await (
            await engine.db_connection.execute("SELECT * FROM documents ORDER BY id")
        ).fetchall()
        records = [
            {**dict(row), "metadata": json.loads(row["metadata"])} for row in rows
        ]
        return [
            record
            for record in records
            if (ids is None or record["id"] in ids)
            and all(
                record["metadata"].get(key) == value
                for key, value in metadata_filters.items()
            )
        ][offset : offset + limit]

    vectors.document_storage.get_documents = get_documents

    async def patch_metadata(memory_id, patch):
        row = await (
            await engine.db_connection.execute(
                "SELECT metadata FROM documents WHERE id=?",
                (memory_id,),
            )
        ).fetchone()
        if row is None:
            return False
        metadata = {**json.loads(row[0]), **patch}
        await engine.db_connection.execute(
            "UPDATE documents SET metadata=? WHERE id=?",
            (json.dumps(metadata), memory_id),
        )
        await engine.db_connection.commit()
        engine.faiss_db.docs[memory_id]["metadata"] = metadata
        return True

    engine.hybrid_retriever.update_metadata = AsyncMock(side_effect=patch_metadata)
    return engine


def _api(engine):
    plugin = FakePlugin(memory_engine=engine)
    plugin.initializer.memory_processor = SimpleNamespace(
        classify_atoms_from_metadata=lambda metadata, parent_importance, session_id, persona_id: (
            classify_atoms(
                metadata.get("key_facts", []),
                parent_importance=parent_importance,
                session_id=session_id,
                persona_id=persona_id,
            )
        ),
    )
    return PluginPageApi(plugin)


async def _import(api, records, **options):
    request = _mock_page_request(
        get_json={
            "format": "json",
            "content": serialize_transfer_json(records, "2026-10-25"),
            "dry_run": False,
            **options,
        }
    )
    with _patch_page_request(request):
        return await api.import_memories()


async def _enable_graph(engine):
    engine.graph_store = GraphStore(engine.db_path)
    await engine.graph_store.initialize()
    engine.graph_vector_db = _FakeFaissDB()
    engine.graph_memory_manager = GraphMemoryManager(
        engine.graph_store,
        GraphVectorRetriever(engine.graph_vector_db),
        GraphExtractor(),
    )


@pytest.mark.asyncio
async def test_native_import_indexes_live_atoms_with_original_confidence_and_entities(
    tmp_path,
):
    engine = await _engine(tmp_path / "native-graph.db")
    await _enable_graph(engine)
    try:
        result = await _import(
            _api(engine),
            [
                {
                    "content": "old fact and current fact",
                    "session_id": "s1",
                    "persona_id": "p1",
                    "metadata": {
                        "key_facts": ["old fact", "current fact"],
                        "topics": ["old topic"],
                    },
                    "atoms": [
                        {
                            "content": "old fact",
                            "status": "forgotten",
                            "expires_at": time.time() + 3600,
                        },
                        {
                            "content": "current fact",
                            "status": "active",
                            "expires_at": time.time() + 3600,
                            "confidence": 0.37,
                            "entities": ["precise topic"],
                        },
                    ],
                }
            ],
        )
        memory_id = result["data"]["imported_ids"][0]
        snapshot = await engine.graph_store.get_subgraph_for_memories([memory_id])
        contents = [entry["content"] for entry in snapshot["entries"]]
        assert any("current fact" in content for content in contents)
        assert any("precise topic" in content for content in contents)
        assert all("old fact" not in content for content in contents)
        assert any(
            entry["metadata"].get("graph_confidence") == 0.37
            for entry in snapshot["entries"]
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_archived_import_keeps_snapshot_but_removes_retrieval_artifacts(tmp_path):
    engine = await _engine(tmp_path / "import-archive-index.db")
    await _enable_graph(engine)
    expiry = time.time() + 3600
    try:
        result = await _import(
            _api(engine),
            [
                {
                    "content": "archived fact",
                    "session_id": "s1",
                    "metadata": {"status": "archived", "key_facts": ["archived fact"]},
                    "atoms": [
                        {
                            "content": "archived fact",
                            "status": "active",
                            "expires_at": expiry,
                        }
                    ],
                }
            ],
        )
        memory_id = result["data"]["imported_ids"][0]
        document = await engine.get_memory(memory_id)
        assert document["metadata"]["status"] == "archived"
        assert (
            document["metadata"]["archived_atom_lifecycle"][0]["expires_at"] == expiry
        )
        assert await engine.atom_store.get_by_parent(memory_id) == []
        snapshot = await engine.graph_store.get_subgraph_for_memories([memory_id])
        assert snapshot["entries"] == []
        assert engine.graph_vector_db.docs == {}
        fts = await (
            await engine.db_connection.execute(
                "SELECT COUNT(*) FROM livingmemory_memories_fts"
            )
        ).fetchone()
        assert fts[0] == 0
        engine.faiss_db.embedding_storage.delete.assert_awaited_with([memory_id])
        assert await engine.restore_memory(memory_id)
        atom = (await engine.atom_store.get_by_parent(memory_id))[0]
        assert atom.expires_at == expiry
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["expired", "forgotten", "superseded"])
async def test_native_round_trip_does_not_revive_hidden_atoms(tmp_path, status):
    source = await _engine(tmp_path / "source.db")
    target = await _engine(tmp_path / "target.db")
    try:
        memory_id = await source.add_memory(
            "likes coffee",
            session_id="s1",
            persona_id="p1",
            metadata={"key_facts": ["likes coffee"]},
            atoms=[
                MemoryAtom(0, atom_type=AtomType.PREFERENCE, content="likes coffee")
            ],
        )
        expiry = time.time() - 60 if status == "expired" else time.time() + 3600
        await source.db_connection.execute(
            "UPDATE memory_atoms SET expires_at=?,status=?,created_at=123,"
            "last_accessed_at=456,reinforcement_count=3 WHERE parent_memory_id=?",
            (expiry, status, memory_id),
        )
        await source.db_connection.commit()
        result = await _import(
            _api(target), await source.get_memory_transfer_records([memory_id])
        )
        assert result["data"]["imported_count"] == 1
        imported_id = result["data"]["imported_ids"][0]
        atom = (await target.atom_store.get_by_parent(imported_id))[0]
        assert atom.expires_at == expiry
        assert atom.status.value == status
        assert atom.created_at == 123 and atom.last_accessed_at == 456
        assert atom.reinforcement_count == 3
        document = await target.get_memory(imported_id)
        assert (
            await target.resolve_live_facts(
                imported_id, document["text"], document["metadata"]
            )
            is None
        )
    finally:
        await source.close()
        await target.close()


@pytest.mark.asyncio
async def test_concurrent_skip_imports_write_one_copy(tmp_path, monkeypatch):
    engine = await _engine(tmp_path / "concurrent.db")
    api = _api(engine)
    existing = engine.get_memory_import_keys

    async def delayed_keys():
        keys = await existing()
        await asyncio.sleep(0.02)
        return keys

    monkeypatch.setattr(engine, "get_memory_import_keys", delayed_keys)
    request = _mock_page_request(
        get_json={
            "content": json.dumps(
                [{"content": "one shared import", "session_id": "s1"}]
            ),
            "dry_run": False,
            "duplicate_strategy": "skip",
        }
    )
    try:
        with _patch_page_request(request):
            results = await asyncio.gather(api.import_memories(), api.import_memories())
        assert sum(result["data"]["imported_count"] for result in results) == 1
        assert sum(result["data"]["skipped_duplicate_count"] for result in results) == 1
        count = await (
            await engine.db_connection.execute("SELECT COUNT(*) FROM documents")
        ).fetchone()
        assert count[0] == 1
    finally:
        await engine.close()


def test_import_rejects_conflicting_top_level_and_metadata_scope():
    entries, errors = parse_transfer_content(
        json.dumps(
            [
                {
                    "content": "scoped fact",
                    "session_id": "reviewed-scope",
                    "persona_id": "reviewed-persona",
                    "metadata": {
                        "session_id": "other-scope",
                        "persona_id": "other-persona",
                    },
                }
            ]
        )
    )
    assert entries == [] and len(errors) == 1


@pytest.mark.asyncio
async def test_imported_foreign_message_ids_cannot_trigger_local_rollback(tmp_path):
    engine = await _engine(tmp_path / "foreign-ids.db")
    try:
        result = await _import(
            _api(engine),
            [
                {
                    "content": "fact from another database",
                    "session_id": "s1",
                    "metadata": {
                        "source_session_id": "s1",
                        "source_revision": 0,
                        "source_message_ids": [1, 2],
                        "source_window": {"start_index": 0, "end_index": 2},
                    },
                }
            ],
        )
        memory_id = result["data"]["imported_ids"][0]
        removed = await engine.delete_reverted_summaries(
            "s1", 0, 0, removed_message_ids=[1, 2]
        )
        assert removed == []
        assert await engine.get_memory(memory_id) is not None
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_llm_generated_import_metadata_keeps_reviewed_scope_and_foreign_provenance(
    tmp_path,
):
    engine = await _engine(tmp_path / "generated-scope.db")
    api = _api(engine)
    processor = api.plugin.initializer.memory_processor
    processor.process_conversation = AsyncMock(
        return_value=(
            "summary of imported conversation",
            {
                "key_facts": ["likes coffee"],
                "session_id": "other-session",
                "persona_id": "other-persona",
                "source_session_id": "s1",
                "source_message_ids": [1, 2],
                "source_revision": 0,
                "source_window": {"start_index": 0, "end_index": 2},
                "status": "deleted",
            },
            0.5,
        )
    )
    try:
        result = await _import(
            api,
            [
                {
                    "session_id": "s1",
                    "persona_id": "p1",
                    "source_messages": [
                        {"role": "user", "content": "likes coffee"},
                        {"role": "assistant", "content": "remembered"},
                    ],
                }
            ],
        )
        assert result["data"]["imported_count"] == 1
        memory_id = result["data"]["imported_ids"][0]
        metadata = (await engine.get_memory(memory_id))["metadata"]
        assert metadata["session_id"] == "s1" and metadata["persona_id"] == "p1"
        assert metadata["status"] == "active"
        assert "source_message_ids" not in metadata
        assert metadata["import_provenance"]["source_message_ids"] == [1, 2]
        assert metadata["import_provenance"]["source_revision"] == 0
        atom = (await engine.atom_store.get_by_parent(memory_id))[0]
        assert atom.session_id == "s1" and atom.persona_id == "p1"
        assert (
            await engine.delete_reverted_summaries(
                "s1", 0, 0, removed_message_ids=[1, 2]
            )
            == []
        )
        assert await engine.get_memory(memory_id) is not None
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_csv_round_trip_preserves_lifecycle_source_and_null_scope(tmp_path):
    engine = await _engine(tmp_path / "csv.db")
    expiry = time.time() - 10
    records = [
        {
            "content": "old fact",
            "session_id": None,
            "persona_id": None,
            "metadata": {"key_facts": ["old fact"]},
            "atoms": [
                {
                    "content": "old fact",
                    "status": "expired",
                    "expires_at": expiry,
                    "session_id": "foreign-scope",
                    "persona_id": "foreign-persona",
                }
            ],
            "source_messages": [{"content": "exact source", "role": "user", "id": 91}],
        }
    ]
    request = _mock_page_request(
        get_json={
            "format": "csv",
            "content": serialize_transfer_csv(records),
            "dry_run": False,
        }
    )
    try:
        with _patch_page_request(request):
            result = await _api(engine).import_memories()
        memory_id = result["data"]["imported_ids"][0]
        atom = (await engine.atom_store.get_by_parent(memory_id))[0]
        assert atom.expires_at == expiry and atom.status.value == "expired"
        assert atom.session_id is atom.persona_id is None
        assert atom.metadata["session_id"] is atom.metadata["persona_id"] is None
        assert (await engine.get_memory_source(memory_id))[0][
            "content"
        ] == "exact source"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_archived_export_import_restore_keeps_original_expired_snapshot(tmp_path):
    source = await _engine(tmp_path / "archive-source.db")
    target = await _engine(tmp_path / "archive-target.db")
    try:
        memory_id = await source.add_memory(
            "archived fact",
            session_id="s1",
            metadata={"key_facts": ["archived fact"]},
            atoms=[MemoryAtom(0, content="archived fact")],
        )
        expiry = time.time() - 60
        await source.db_connection.execute(
            "UPDATE memory_atoms SET expires_at=?,status='expired' WHERE parent_memory_id=?",
            (expiry, memory_id),
        )
        await source.db_connection.commit()
        assert await source.archive_memories([memory_id]) == 1
        result = await _import(
            _api(target), await source.get_memory_transfer_records([memory_id])
        )
        imported = result["data"]["imported_ids"][0]
        assert (await target.get_memory(imported))["metadata"]["status"] == "archived"
        assert await target.atom_store.get_by_parent(imported) == []
        assert await target.restore_memory(imported)
        atom = (await target.atom_store.get_by_parent(imported))[0]
        assert atom.expires_at == expiry and atom.status.value == "expired"
        document = await target.get_memory(imported)
        assert (
            await target.resolve_live_facts(
                imported, document["text"], document["metadata"]
            )
            is None
        )
    finally:
        await source.close()
        await target.close()


@pytest.mark.parametrize(
    "patch",
    [
        {"expires_at": float("nan")},
        {"expires_at": True},
        {"expires_at": -1},
        {"status": "made-up"},
        {"reinforcement_count": -1},
        {"metadata": []},
        {"entities": {}},
        {"content": ""},
    ],
)
def test_invalid_atom_snapshot_is_rejected_before_import(patch):
    entries, errors = parse_transfer_content(
        json.dumps(
            [
                {
                    "content": "fact",
                    "atoms": [{"content": "fact", "expires_at": 100, **patch}],
                }
            ]
        )
    )
    assert entries == [] and len(errors) == 1


@pytest.mark.parametrize("version", [0, 999, True, "2"])
def test_unsupported_native_schema_does_not_silently_import(version):
    with pytest.raises(ValueError):
        parse_transfer_content(
            json.dumps(
                {
                    "format": "anamnesis",
                    "schema_version": version,
                    "memories": [{"content": "fact"}],
                }
            )
        )


@pytest.mark.asyncio
async def test_cancelled_import_finishes_invisible_staging_and_retry_skips_it(
    tmp_path, monkeypatch
):
    engine = await _engine(tmp_path / "cancel.db")
    records = [
        {
            "content": "fact",
            "session_id": "s1",
            "atoms": [
                {"content": "fact", "expires_at": time.time() + 3600},
            ],
        }
    ]
    entered, release = asyncio.Event(), asyncio.Event()
    restore = engine.restore_archived_atom_lifecycle

    async def gated_restore(*args, **kwargs):
        entered.set()
        await release.wait()
        return await restore(*args, **kwargs)

    monkeypatch.setattr(engine, "restore_archived_atom_lifecycle", gated_restore)
    task = asyncio.create_task(_import(_api(engine), records))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        row = await (
            await engine.db_connection.execute("SELECT id,text,metadata FROM documents")
        ).fetchone()
        assert json.loads(row[2])["status"] == "importing"
        assert (
            await engine.resolve_live_facts(row[0], row[1], json.loads(row[2])) is None
        )
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        result = await _import(_api(engine), records)
        assert result["data"]["imported_count"] == 0
        assert result["data"]["skipped_duplicate_count"] == 1
        assert (await engine.get_memory(row[0]))["metadata"]["status"] == "active"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await engine.close()


@pytest.mark.asyncio
async def test_engine_shutdown_cancels_and_rolls_back_staged_import_before_closing_database(
    tmp_path, monkeypatch
):
    path = tmp_path / "shutdown.db"
    engine = await _engine(path)
    entered = asyncio.Event()

    async def parked_restore(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(engine, "restore_archived_atom_lifecycle", parked_restore)
    task = asyncio.create_task(
        _import(
            _api(engine),
            [
                {
                    "content": "fact",
                    "source_messages": [{"role": "user", "content": "exact"}],
                    "atoms": [{"content": "fact", "expires_at": 100}],
                }
            ],
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert any(not pending.done() for pending in engine._pending_tasks)
        await asyncio.wait_for(engine.close(), 5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        async with aiosqlite.connect(path) as connection:
            for table in (
                "documents",
                "memory_atoms",
                "memory_sources",
                "livingmemory_memories_fts",
                "memory_atoms_fts",
            ):
                count = await (
                    await connection.execute(f"SELECT COUNT(*) FROM {table}")
                ).fetchone()
                assert count[0] == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await engine.close()


@pytest.mark.asyncio
async def test_shutdown_during_add_does_not_leave_an_invisible_partial_import(
    tmp_path, monkeypatch
):
    path = tmp_path / "shutdown-add.db"
    engine = await _engine(path)
    entered = asyncio.Event()
    release = asyncio.Event()
    insert_many = engine.atom_store.insert_many

    async def parked_atoms(*args, **kwargs):
        entered.set()
        await release.wait()
        return await insert_many(*args, **kwargs)

    monkeypatch.setattr(engine.atom_store, "insert_many", parked_atoms)
    task = asyncio.create_task(
        _import(
            _api(engine),
            [
                {
                    "content": "fact",
                    "metadata": {"key_facts": ["fact"]},
                    "source_messages": [{"role": "user", "content": "exact"}],
                }
            ],
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        closing = asyncio.create_task(engine.close())
        await asyncio.sleep(0.01)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        async with aiosqlite.connect(path) as connection:
            count = await (
                await connection.execute("SELECT COUNT(*) FROM documents")
            ).fetchone()
            assert count[0] == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lifecycle", "activation"])
async def test_failed_import_rolls_back_document_source_and_atoms(
    tmp_path, monkeypatch, failure
):
    engine = await _engine(tmp_path / "rollback.db")
    records = [
        {
            "content": "fact",
            "source_messages": [{"role": "user", "content": "exact"}],
            "atoms": [{"content": "fact", "expires_at": 100}],
        }
    ]
    try:
        if failure == "lifecycle":
            monkeypatch.setattr(
                engine,
                "restore_archived_atom_lifecycle",
                AsyncMock(side_effect=RuntimeError("restore failed")),
            )
        else:
            monkeypatch.setattr(engine, "update_memory", AsyncMock(return_value=False))
        result = await _import(_api(engine), records)
        assert (
            result["data"]["imported_count"] == 0
            and result["data"]["failed_count"] == 1
        )
        for table in (
            "documents",
            "memory_atoms",
            "memory_sources",
            "livingmemory_memories_fts",
            "memory_atoms_fts",
        ):
            count = await (
                await engine.db_connection.execute(f"SELECT COUNT(*) FROM {table}")
            ).fetchone()
            assert count[0] == 0
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", [None, 0, False, True, -1, "3"])
async def test_invalid_import_id_does_not_delete_an_unrelated_memory(
    tmp_path, monkeypatch, invalid_id
):
    engine = await _engine(tmp_path / "invalid-import-id.db")
    delete = AsyncMock()
    monkeypatch.setattr(engine, "add_memory", AsyncMock(return_value=invalid_id))
    monkeypatch.setattr(engine, "delete_memory", delete)
    try:
        result = await _import(_api(engine), [{"content": "fact"}])
        assert result["data"]["imported_count"] == 0
        assert result["data"]["failed_count"] == 1
        delete.assert_not_awaited()
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_incomplete_atom_write_is_not_reported_as_successful_import(
    tmp_path, monkeypatch
):
    engine = await _engine(tmp_path / "incomplete-import.db")
    try:
        monkeypatch.setattr(
            engine.atom_store,
            "insert_many",
            AsyncMock(side_effect=RuntimeError("batch failed")),
        )
        monkeypatch.setattr(
            engine.atom_store,
            "insert",
            AsyncMock(side_effect=RuntimeError("single failed")),
        )
        result = await _import(
            _api(engine),
            [
                {
                    "content": "fact",
                    "metadata": {"key_facts": ["fact"]},
                    "source_messages": [{"role": "user", "content": "exact"}],
                }
            ],
        )
        assert (
            result["data"]["imported_count"] == 0
            and result["data"]["failed_count"] == 1
        )
        for table in (
            "documents",
            "memory_sources",
            "memory_atoms",
            "livingmemory_memories_fts",
        ):
            count = await (
                await engine.db_connection.execute(f"SELECT COUNT(*) FROM {table}")
            ).fetchone()
            assert count[0] == 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_export_uses_one_snapshot_for_document_and_atom_state(
    tmp_path, monkeypatch
):
    engine = await _engine(tmp_path / "snapshot.db")
    try:
        memory_id = await engine.add_memory(
            "fact", session_id="s1", atoms=[MemoryAtom(0, content="fact")]
        )
        atom = (await engine.atom_store.get_by_parent(memory_id))[0]
        execute = aiosqlite.Connection.execute
        changed = False

        def interleaved_execute(connection, sql, parameters=None):
            if "FROM documents AS d" not in sql:
                return execute(connection, sql, parameters)

            async def read_then_write():
                nonlocal changed
                cursor = await execute(connection, sql, parameters)
                if not changed:
                    changed = True
                    async with aiosqlite.connect(engine.db_path) as writer:
                        await writer.execute(
                            "UPDATE documents SET metadata=json_set(metadata,'$.new_note','new value')"
                        )
                        await writer.execute(
                            "UPDATE memory_atoms SET expires_at=999,status='expired'"
                        )
                        await writer.commit()
                return cursor

            return read_then_write()

        monkeypatch.setattr(aiosqlite.Connection, "execute", interleaved_execute)
        record = (await engine.get_memory_transfer_records([memory_id]))[0]
        assert "new_note" not in record["metadata"]
        assert record["atoms"][0]["expires_at"] == atom.expires_at
        assert record["atoms"][0]["status"] == "active"
        assert (await engine.atom_store.get_by_parent(memory_id))[0].expires_at == 999
    finally:
        await engine.close()
