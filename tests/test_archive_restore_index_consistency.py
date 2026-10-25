"""Public lifecycle operations preserve real host indexes and recovery evidence."""

import json
import time

import faiss
import numpy as np
import pytest
from astrbot.core.db.vec_db.faiss_impl.embedding_storage import EmbeddingStorage
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.managers.memory_administration import (
    MemoryAdministration,
)
from astrbot_plugin_anamnesis.core.memory_source import serialize_source_messages
from astrbot_plugin_anamnesis.core.models.memory_atom import (
    AtomStatus,
    AtomType,
    MemoryAtom,
)
from quart import Quart, g

from tests.test_memory_transfer_runtime_safety import _api, _engine

APP = Quart(__name__)
SCOPE = "qq:GroupMessage:1"


async def request_admin(engine, action, body):
    async with APP.test_request_context(
        f"/operations/{action}", method="POST", json=body
    ):
        g.username = "a"
        return await _api(engine).administration_request(action)


async def make_service(tmp_path, config=None):
    engine = await _engine(tmp_path / "memory.db")
    service = MemoryAdministration(engine, ConfigManager(config or {}))
    engine.memory_administration = service
    await service.initialize()
    return engine, service


async def add_indexed_source(engine, tmp_path):
    engine.atom_enabled = True
    embedding = EmbeddingStorage(2, str(tmp_path / "memory.faiss"))
    engine.faiss_db.embedding_storage = embedding
    source = serialize_source_messages(
        [{"role": "user", "content": "exact source", "session_id": SCOPE}]
    )
    memory_id = await engine.add_memory(
        "valid fact",
        session_id=SCOPE,
        persona_id="p",
        metadata={"key_facts": ["valid fact"]},
        source_messages=source,
        atoms=[
            MemoryAtom(
                parent_memory_id=0,
                content="valid fact",
                atom_type=AtomType.PREFERENCE,
                status=AtomStatus.EXPIRED,
                created_at=123,
                last_accessed_at=456,
                expires_at=time.time() - 100,
                reinforcement_count=3,
            )
        ],
        preserve_atom_lifecycle=True,
    )
    lifecycle = await engine.snapshot_archived_atom_lifecycle(memory_id)
    await embedding.insert(np.asarray([1.0, 0.0], dtype=np.float32), memory_id)
    return memory_id, embedding, source, lifecycle


def vector_ids(embedding):
    return list(faiss.vector_to_array(embedding.index.id_map))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_vector",
    [
        [float("nan"), 0.0],
        [float("inf"), 0.0],
        [],
        [[1.0, 0.0]],
        [1.0],
        [1.0, 0.0, 0.0],
        None,
    ],
)
async def test_restore_rejects_invalid_provider_vectors_before_index_changes(
    tmp_path, invalid_vector
):
    engine, service = await make_service(tmp_path)
    try:
        memory_id, embedding, source, lifecycle = await add_indexed_source(
            engine, tmp_path
        )
        deleted = await request_admin(engine, "delete", {"memory_ids": [memory_id]})
        operation_id = deleted["data"]["operation_id"]
        archived = await service.snapshot(memory_id)
        assert embedding.index.ntotal == 0
        engine.faiss_db.embedding_provider.get_embedding.return_value = invalid_vector
        failed = await request_admin(engine, "restore", {"operation_id": operation_id})
        assert failed["status"] == "error" and "Embedding" in failed["message"]
        assert await service.snapshot(memory_id) == archived
        assert embedding.index.ntotal == 0 and vector_ids(embedding) == []
        assert (await service.store.items(operation_id))[0]["status"] == "restoring"
        assert await engine.atom_store.get_by_parent(memory_id) == []
        engine.faiss_db.embedding_provider.get_embedding.return_value = [1.0, 0.0]
        restored = await request_admin(
            engine, "restore", {"operation_id": operation_id}
        )
        assert restored["status"] == "ok" and restored["data"]["ids"] == [memory_id]
        assert vector_ids(embedding) == [memory_id]
        assert await engine.get_memory_source(memory_id) == source
        atoms = await engine.atom_store.get_by_parent(memory_id)
        assert len(atoms) == 1 and atoms[0].status is AtomStatus.EXPIRED
        assert atoms[0].expires_at == lifecycle[0]["expires_at"]
        assert atoms[0].created_at == 123 and atoms[0].last_accessed_at == 456
        assert atoms[0].reinforcement_count == 3
        assert (await service.store.get(operation_id))["status"] == "restored"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_restore_checks_configured_and_loaded_host_index_dimensions(tmp_path):
    engine, service = await make_service(tmp_path)
    try:
        memory_id, embedding, source, _ = await add_indexed_source(engine, tmp_path)
        deleted = await request_admin(engine, "delete", {"memory_ids": [memory_id]})
        operation_id = deleted["data"]["operation_id"]
        archived = await service.snapshot(memory_id)
        embedding.dimension = 3
        failed = await request_admin(engine, "restore", {"operation_id": operation_id})
        assert failed["status"] == "error" and "维度" in failed["message"]
        assert (
            embedding.index.ntotal == 0
            and await service.snapshot(memory_id) == archived
        )
        embedding.dimension = 2
        restored = await request_admin(
            engine, "restore", {"operation_id": operation_id}
        )
        assert restored["data"]["ids"] == [memory_id]
        assert vector_ids(embedding) == [memory_id]
        assert await engine.get_memory_source(memory_id) == source
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_restore_after_archive_cleanup_failure_replaces_existing_host_vector(
    tmp_path, monkeypatch
):
    engine, service = await make_service(tmp_path)
    try:
        memory_id, embedding, source, _ = await add_indexed_source(engine, tmp_path)
        actual_delete = embedding.delete
        calls = 0

        async def fail_first_delete(ids):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary archive persistence failure")
            return await actual_delete(ids)

        monkeypatch.setattr(embedding, "delete", fail_first_delete)
        deleted = await request_admin(engine, "delete", {"memory_ids": [memory_id]})
        assert deleted["data"]["deleted_ids"] == [memory_id]
        assert vector_ids(embedding) == [memory_id]
        restored = await request_admin(
            engine, "restore", {"operation_id": deleted["data"]["operation_id"]}
        )
        assert restored["data"]["ids"] == [memory_id] and calls == 2
        assert embedding.index.ntotal == 1 and vector_ids(embedding) == [memory_id]
        assert await engine.get_memory_source(memory_id) == source
        repair = await request_admin(engine, "repair_projection", {})
        await service._jobs[repair["data"]["operation_id"]]
        assert embedding.index.ntotal == 1 and vector_ids(embedding) == [memory_id]
        assert await service.projection.pending() == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_restore_progress_retry_does_not_add_a_second_host_vector(
    tmp_path, monkeypatch
):
    engine, service = await make_service(tmp_path)
    try:
        memory_id, embedding, source, _ = await add_indexed_source(engine, tmp_path)
        deleted = await request_admin(engine, "delete", {"memory_ids": [memory_id]})
        operation_id = deleted["data"]["operation_id"]
        put_item = service.store.put_item

        async def fail_progress(*args, **kwargs):
            if kwargs.get("status") == "restored":
                raise OSError("completion temporarily unavailable")
            return await put_item(*args, **kwargs)

        monkeypatch.setattr(service.store, "put_item", fail_progress)
        failed = await request_admin(engine, "restore", {"operation_id": operation_id})
        assert failed["status"] == "error" and vector_ids(embedding) == [memory_id]
        assert (await engine.get_memory(memory_id))["metadata"]["status"] == "active"
        monkeypatch.setattr(service.store, "put_item", put_item)
        restored = await request_admin(
            engine, "restore", {"operation_id": operation_id}
        )
        assert restored["data"]["ids"] == [memory_id]
        assert vector_ids(embedding) == [memory_id]
        assert await engine.get_memory_source(memory_id) == source
        assert (await service.store.get(operation_id))["status"] == "restored"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_restore_missing_delete_interface_keeps_archived_source(
    tmp_path, monkeypatch
):
    engine, service = await make_service(tmp_path)
    try:
        memory_id, embedding, _, _ = await add_indexed_source(engine, tmp_path)
        deleted = await request_admin(engine, "delete", {"memory_ids": [memory_id]})
        archived = await service.snapshot(memory_id)
        monkeypatch.setattr(embedding, "delete", None)
        failed = await request_admin(
            engine, "restore", {"operation_id": deleted["data"]["operation_id"]}
        )
        assert failed["status"] == "error"
        assert (
            await service.snapshot(memory_id) == archived
            and embedding.index.ntotal == 0
        )
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_batch_delete_capacity_failure_reports_partial_outcome_and_restores(
    tmp_path,
):
    engine, service = await make_service(
        tmp_path, {"memory_administration": {"max_storage_mib": 8}}
    )
    try:
        ids, sources = [], {}
        for index in range(7):
            source = [{"role": "user", "content": f"exact source {index}"}]
            memory_id = await engine.add_memory(
                f"fact {index}",
                session_id=SCOPE,
                metadata={"note": "x" * 750000},
                source_messages=source,
            )
            ids.append(memory_id)
            sources[memory_id] = source
            assert (
                len(json.dumps(await service.snapshot(memory_id)).encode())
                < service.store.max_snapshot_bytes
            )
        result = await request_admin(engine, "delete", {"memory_ids": ids})
        assert result["status"] == "ok"
        data = result["data"]
        assert data["deleted_ids"] == ids[:5]
        assert [failure["id"] for failure in data["failed"]] == ids[5:]
        operation_id = data["operation_id"]
        assert (await service.store.get(operation_id))["status"] == "partial"
        assert len(await service.store.items(operation_id)) == 6
        assert (await engine.get_memory(ids[-1]))["metadata"].get(
            "recovery_operation_id"
        ) is None
        for memory_id in ids:
            assert await engine.get_memory_source(memory_id) == sources[memory_id]
        restored = await request_admin(
            engine, "restore", {"operation_id": operation_id}
        )
        assert restored["status"] == "ok" and restored["data"]["ids"] == ids[:6]
        for memory_id in ids:
            assert (await engine.get_memory(memory_id))["metadata"][
                "status"
            ] == "active"
            assert await engine.get_memory_source(memory_id) == sources[memory_id]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_archive_completion_and_result_quota_keep_snapshots_and_audit_state(
    tmp_path, monkeypatch
):
    engine, service = await make_service(tmp_path)
    try:
        memory_id = await engine.add_memory(
            "fact",
            session_id=SCOPE,
            source_messages=[{"role": "user", "content": "exact source"}],
        )
        source = await engine.get_memory_source(memory_id)
        put_item = service.store.put_item
        original_budget = service.store.max_bytes

        async def consume_capacity_before_completion(*args, **kwargs):
            if kwargs.get("status") == "deleted":
                # Exercise the actual SQLite quota trigger, rather than replacing
                # the final write with an artificial success or removing evidence.
                service.store.max_bytes = (await service.store.storage_usage())[
                    "used_bytes"
                ]
            return await put_item(*args, **kwargs)

        monkeypatch.setattr(
            service.store, "put_item", consume_capacity_before_completion
        )
        result = await request_admin(engine, "delete", {"memory_ids": [memory_id]})
        assert result["status"] == "ok"
        data = result["data"]
        assert (
            data["deleted_ids"] == [memory_id] and data["failed"][0]["id"] == memory_id
        )
        assert data["result_persisted"] is False
        operation_id = data["operation_id"]
        operation = await service.store.get(operation_id)
        assert operation["status"] == "partial" and operation["result"] == {}
        item = (await service.store.items(operation_id))[0]
        assert (
            item["status"] == "prepared"
            and item["payload"]["before"]["source_messages"] == source
        )
        assert "tagged" in item["payload"]
        assert (await engine.get_memory(memory_id))["metadata"]["status"] == "archived"
        usage = await service.store.storage_usage()
        assert usage["used_bytes"] <= usage["max_bytes"]
        service.store.max_bytes = original_budget
        restored = await request_admin(
            engine, "restore", {"operation_id": operation_id}
        )
        assert restored["status"] == "ok" and restored["data"]["ids"] == [memory_id]
        assert await engine.get_memory_source(memory_id) == source
    finally:
        await engine.close()
