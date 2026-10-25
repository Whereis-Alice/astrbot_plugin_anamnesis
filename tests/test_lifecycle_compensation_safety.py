"""Summary compensation must preserve newer user and lifecycle operations."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_archive_access_integration import make_engine
from tests.test_dedup_snapshot_and_cursor_sync import _dedup_engine, _write_options
from tests.test_upstream_adaptations import event, fact


@pytest.mark.asyncio
async def test_dedup_undo_preserves_concurrent_access_statistics(tmp_path):
    engine, before = await _dedup_engine(tmp_path / "access.db")
    try:
        _, previous = await engine.add_memory_conservatively(
            **_write_options(before, [{"content": "new source"}]),
        )
        assert await engine.update_access_time(1)
        accessed = (await engine.get_memory(1))["metadata"]
        await engine.undo_summary_write(1, previous)
        restored = (await engine.get_memory(1))["metadata"]
        assert restored["access_count"] == accessed["access_count"]
        assert restored["last_access_time"] == accessed["last_access_time"]
        assert "dedup_count" not in restored
        assert await engine.get_memory_source(1) == []
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stable_ids", [True, False])
async def test_revert_new_summary_window_preserves_old_memory_and_source(
    tmp_path, stable_ids,
):
    old_source = [{"content": "original coffee statement"}]
    engine, before = await _dedup_engine(tmp_path / "revert-dedup.db", old_source)
    before["source_revision"] = 0
    if stable_ids:
        before["source_message_ids"] = [1, 2]
    await engine.db_connection.execute(
        "UPDATE documents SET metadata=? WHERE id=1", (json.dumps(before),),
    )
    await engine.db_connection.commit()

    async def add_new_memory(**kwargs):
        metadata = {**kwargs["metadata"], "importance": kwargs["importance"]}
        await engine.db_connection.execute(
            "INSERT INTO documents(id,doc_id,text,metadata) VALUES(9,'uuid-9',?,?)",
            (kwargs["content"], json.dumps(metadata)),
        )
        await engine.db_connection.commit()
        await engine.bm25_retriever.add_document(9, kwargs["content"], metadata)
        await engine.save_memory_source(9, kwargs["source_messages"])
        return 9

    async def delete_host_document(uuid):
        await engine.db_connection.execute(
            "DELETE FROM documents WHERE doc_id=?", (uuid,),
        )
        await engine.db_connection.commit()

    engine.add_memory.side_effect = add_new_memory
    engine.faiss_db.delete = delete_host_document
    options = _write_options(before, [{"content": "new coffee statement"}])
    if stable_ids:
        options["metadata"]["source_message_ids"] = [3, 4]
    try:
        await engine.add_memory_conservatively(**options)
        provenance = {"removed_message_ids": [3, 4]} if stable_ids else {}
        removed = await engine.delete_reverted_summaries(
            before["source_session_id"], 2, 0, **provenance,
        )
        original = await engine.get_memory(1)
        assert original is not None
        assert original["metadata"] == before
        assert await engine.get_memory_source(1) == old_source
        assert removed == [9]
        assert await engine.get_memory(9) is None
        assert await engine.get_memory_source(9) == []
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_extra,new_extra,merge", [
    ({"source_message_ids": [1, 2]}, {"source_message_ids": [2, 1, 1]}, True),
    ({"source_message_ids": [1, 2]}, {"source_message_ids": [3, 4]}, False),
    ({"source_message_ids": [1, 2]}, {"source_message_ids": [1]}, False),
    ({}, {"source_message_ids": [1, 2]}, False),
    ({"source_message_ids": [1, 2]}, {}, False),
    ({"source_message_ids": []}, {"source_message_ids": []}, False),
    ({"source_message_ids": None}, {"source_message_ids": None}, False),
    ({"source_message_ids": "1,2"}, {"source_message_ids": "1,2"}, False),
    ({"source_message_ids": [True, 2]}, {"source_message_ids": [1, 2]}, False),
    ({"source_message_ids": [1, 2]}, {"source_message_ids": [True, 2]}, False),
    ({"source_message_ids": [1.0, 2]}, {"source_message_ids": [1, 2]}, False),
    ({"source_message_ids": [1, 2]}, {"source_message_ids": [1, 2.0]}, False),
    ({"source_message_ids": [0, 2]}, {"source_message_ids": [0, 2]}, False),
    ({"source_message_ids": [-1, 2]}, {"source_message_ids": [-1, 2]}, False),
])
async def test_summary_dedup_requires_same_valid_message_id_set(
    tmp_path, old_extra, new_extra, merge,
):
    engine, before = await _dedup_engine(tmp_path / "message-id-dedup.db")
    before.update(source_revision=0, **old_extra)
    await engine.db_connection.execute(
        "UPDATE documents SET metadata=? WHERE id=1", (json.dumps(before),),
    )
    await engine.db_connection.commit()
    options = _write_options(before, [{"content": "new source"}])
    options["metadata"].pop("source_message_ids", None)
    options["metadata"].update(new_extra)
    try:
        memory_id, previous = await engine.add_memory_conservatively(**options)
        if merge:
            assert memory_id == 1 and previous is not None
            engine.add_memory.assert_not_awaited()
        else:
            assert memory_id == 9 and previous is None
            engine.add_memory.assert_awaited_once_with(**options)
            assert (await engine.get_memory(1))["metadata"] == before
            assert await engine.get_memory_source(1) == []
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_window,merge", [
    ({"start_index": 0, "end_index": 2}, True),
    ({"start_index": 2, "end_index": 4}, False),
    ({"start_index": 0, "end_index": 2, "message_count": 2}, False),
    ({"start_index": False, "end_index": 2}, False),
    ({"start_index": 0, "end_index": 2.0}, False),
    ({"start_index": 0, "end_index": 0}, False),
    ({"start_index": -1, "end_index": 2}, False),
    ({"end_index": 2}, False),
    (None, False),
])
async def test_legacy_revision_dedup_requires_identical_valid_window(
    tmp_path, new_window, merge,
):
    engine, before = await _dedup_engine(tmp_path / "legacy-window-dedup.db")
    before["source_revision"] = 0
    await engine.db_connection.execute(
        "UPDATE documents SET metadata=? WHERE id=1", (json.dumps(before),),
    )
    await engine.db_connection.commit()
    options = _write_options(before, [{"content": "new source"}])
    options["metadata"]["source_window"] = new_window
    try:
        memory_id, previous = await engine.add_memory_conservatively(**options)
        if merge:
            assert memory_id == 1 and previous is not None
            engine.add_memory.assert_not_awaited()
        else:
            assert memory_id == 9 and previous is None
            engine.add_memory.assert_awaited_once_with(**options)
            assert (await engine.get_memory(1))["metadata"] == before
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_restore_keeps_metadata_updated_while_embedding_runs(tmp_path):
    engine = await make_engine(tmp_path / "restore.db")
    try:
        assert await engine.archive_memories([1]) == 1

        async def embed(content):
            assert await engine.update_access_time(1)
            await engine.db_connection.execute(
                "UPDATE documents SET metadata=json_set(metadata,'$.user_note','new note') WHERE id=1",
            )
            await engine.db_connection.commit()
            return [1., 0.]

        engine.faiss_db.embedding_provider.get_embedding = embed
        assert await engine.restore_memory(1)
        metadata = (await engine.get_memory(1))["metadata"]
        assert metadata["status"] == "active"
        assert metadata["access_count"] == 1
        assert metadata["user_note"] == "new note"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_concurrent_archive_counts_each_memory_once(tmp_path):
    engine = await make_engine(tmp_path / "archive-concurrent.db")
    try:
        counts = await asyncio.gather(engine.archive_memories([1]), engine.archive_memories([1]))
        assert sum(counts) == 1
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_concurrent_restore_does_not_insert_duplicate_vectors(tmp_path):
    engine = await make_engine(tmp_path / "restore-concurrent.db")
    try:
        assert await engine.archive_memories([1]) == 1
        assert all(await asyncio.gather(engine.restore_memory(1), engine.restore_memory(1)))
        assert engine.faiss_db.embedding_storage.insert.await_count == 1
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_restore_refuses_soft_deleted_memory_with_old_restore_marker(tmp_path):
    engine = await make_engine(tmp_path / "deleted.db")
    try:
        await engine.db_connection.execute(
            "UPDATE documents SET metadata=json_set(metadata,'$.status','deleted','$.restored_at',123) WHERE id=1",
        )
        await engine.db_connection.commit()
        assert await engine.restore_memory(1) is False
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["archive", "scope", "delete"])
async def test_dedup_undo_does_not_resurrect_or_move_newer_memory_state(tmp_path, changed):
    engine, before = await _dedup_engine(tmp_path / "state.db")
    try:
        _, previous = await engine.add_memory_conservatively(
            **_write_options(before, [{"content": "new source"}]),
        )
        if changed == "archive":
            assert await engine.archive_memories([1]) == 1
        elif changed == "scope":
            await engine.db_connection.execute(
                "UPDATE documents SET metadata=json_set(metadata,'$.session_id','new-scope') WHERE id=1",
            )
            await engine.db_connection.commit()
        else:
            await engine.db_connection.execute("DELETE FROM documents WHERE id=1")
            await engine.db_connection.commit()
        engine.graph_memory_manager = SimpleNamespace(index_memory=AsyncMock())
        await engine.undo_summary_write(1, previous)
        memory = await engine.get_memory(1)
        if changed == "archive":
            assert memory["metadata"]["status"] == "archived"
            assert "archived_at" in memory["metadata"]
            engine.graph_memory_manager.index_memory.assert_not_awaited()
        elif changed == "scope":
            assert memory["metadata"]["session_id"] == "new-scope"
            assert engine.graph_memory_manager.index_memory.await_args.args[2]["session_id"] == "new-scope"
        else:
            assert memory is None
            engine.graph_memory_manager.index_memory.assert_not_awaited()
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_dedup_undo_preserves_later_metadata_and_source_corrections(tmp_path):
    engine, before = await _dedup_engine(tmp_path / "correction.db")
    try:
        _, previous = await engine.add_memory_conservatively(
            **_write_options(before, [{"content": "new source"}]),
        )
        current = (await engine.get_memory(1))["metadata"]
        current.update(importance=.9, user_note="keep this correction", source_message_count=2)
        await engine.db_connection.execute("UPDATE documents SET metadata=? WHERE id=1", (json.dumps(current),))
        await engine.db_connection.commit()
        later_source = [{"content": "new source"}, {"content": "later correction"}]
        await engine.save_memory_source(1, later_source)
        await engine.undo_summary_write(1, previous)
        restored = (await engine.get_memory(1))["metadata"]
        assert restored["importance"] == .9
        assert restored["user_note"] == "keep this correction"
        assert restored["has_source"] is True and restored["source_message_count"] == 2
        assert await engine.get_memory_source(1) == later_source
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "archived", "deleted"])
async def test_profiles_recheck_source_state_before_injection(tmp_path, state):
    from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
    from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
        UserProfileManager,
    )
    from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore

    engine = await make_engine(tmp_path / "source.db")
    store = ConversationStore(str(tmp_path / "conversation.db"))
    await store.initialize()
    profiles = UserProfileManager(ConfigManager({"user_profile": {"enabled": True}}), SimpleNamespace(store=store))
    profiles.memory_engine = engine
    owner = event()
    try:
        await profiles.upsert_facts(owner, [fact()], source_memory_id=1)
        await profiles.upsert_facts(owner, [fact("manual", "manual fact")])
        assert len(await profiles.get_profile(owner)) == 2
        if state == "missing":
            await engine.db_connection.execute("DELETE FROM documents WHERE id=1")
        else:
            await engine.db_connection.execute(
                "UPDATE documents SET metadata=json_set(metadata,'$.status',?) WHERE id=1", (state,),
            )
        await engine.db_connection.commit()
        assert [row["value"] for row in await profiles.get_profile(owner)] == ["manual fact"]
    finally:
        await store.close()
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["scope", "archive"])
async def test_metadata_graph_update_uses_latest_parent_state(tmp_path, changed):
    engine, before = await _dedup_engine(tmp_path / "graph-state.db")
    original = engine.hybrid_retriever.update_metadata

    async def update_then_change(memory_id, patch):
        assert await original(memory_id, patch)
        path, value = ("$.session_id", "new-scope") if changed == "scope" else ("$.status", "archived")
        await engine.db_connection.execute(
            "UPDATE documents SET metadata=json_set(metadata,?,?) WHERE id=?", (path, value, memory_id),
        )
        await engine.db_connection.commit()
        return True

    engine.hybrid_retriever.update_metadata = update_then_change
    engine.graph_memory_manager = SimpleNamespace(index_memory=AsyncMock())
    try:
        assert await engine.update_memory(1, {"importance": .8})
        if changed == "scope":
            assert engine.graph_memory_manager.index_memory.await_args.args[2]["session_id"] == "new-scope"
        else:
            engine.graph_memory_manager.index_memory.assert_not_awaited()
    finally:
        await engine.close()
