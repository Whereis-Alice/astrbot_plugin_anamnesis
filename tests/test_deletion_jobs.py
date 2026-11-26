"""Verify durable progress, recovery, and fixed document identity for WebUI deletion."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.managers import deletion_jobs
from astrbot_plugin_anamnesis.core.managers.memory_administration import MemoryAdministration

from tests.test_memory_administration import add, administration  # noqa: F401


@pytest.mark.asyncio
async def test_recoverable_deletion_publishes_batch_progress_and_restores_sources(administration, monkeypatch):
    monkeypatch.setattr(deletion_jobs, "RECOVERABLE_BATCH_SIZE", 2)
    ids = [await add(administration, str(index)) for index in range(3)]
    first_source = await administration.engine.get_memory_source(ids[0])
    later_batch = asyncio.Event()
    finish = asyncio.Event()
    archive = administration.engine.archive_memories

    async def staged_archive(memory_ids, **options):
        if memory_ids == ids[2:]:
            later_batch.set()
            await finish.wait()
        return await archive(memory_ids, **options)

    monkeypatch.setattr(administration.engine, "archive_memories", staged_archive)
    administration.engine.faiss_db.embedding_storage.delete.reset_mock()
    accepted = await administration.start_deletion(ids, actor="webui:tester", reason="群记忆清理")
    operation_id = accepted["operation_id"]
    task = administration._jobs[operation_id]
    try:
        await asyncio.wait_for(later_batch.wait(), 5)
        running = await administration.deletion_status(operation_id, actor="webui:tester")
        assert running["status"] == "running"
        assert (running["processed_count"], running["deleted_count"], running["total"]) == (2, 2, 3)
        assert running["can_restore"] is False
    finally:
        finish.set()
        await task

    completed = await administration.deletion_status(operation_id, actor="webui:tester")
    assert completed["status"] == "completed" and completed["can_restore"]
    assert completed["deleted_count"] == 3
    embedding_delete = administration.engine.faiss_db.embedding_storage.delete
    assert [call.args[0] for call in embedding_delete.await_args_list] == [ids[:2], ids[2:]]
    assert (await administration.store.get(operation_id))["payload"]["reason"] == "群记忆清理"

    # Editing an archived source after deletion invalidates only that receipt.
    await administration.engine.update_memory(ids[1], {"metadata": {"session_id": "new-scope"}})
    restored = await administration.restore(operation_id, actor="webui:tester")
    assert restored["ids"] == [ids[0], ids[2]] and restored["conflicts"] == [ids[1]]
    assert await administration.engine.get_memory_source(ids[0]) == first_source
    assert (await administration.deletion_status(operation_id, actor="webui:tester"))["deleted_count"] == 3


@pytest.mark.asyncio
async def test_failed_receipt_rolls_back_entire_archive_batch(administration, monkeypatch):
    ids = [await add(administration, str(index)) for index in range(2)]
    monkeypatch.setattr(
        administration.store, "seal_deletion_snapshots",
        AsyncMock(side_effect=RuntimeError("receipt interrupted")),
    )
    accepted = await administration.start_deletion(ids, actor="webui:tester")
    operation_id = accepted["operation_id"]
    await administration._jobs[operation_id]
    status = await administration.deletion_status(operation_id, actor="webui:tester")
    assert status["status"] == "failed" and not status["can_restore"]
    assert set(status["failed_ids"]) == set(ids) and status["processed_count"] == 2
    for memory_id in ids:
        record = await administration.snapshot(memory_id)
        assert record["metadata"]["status"] == "active"
        assert "recovery_operation_id" not in record["metadata"]


@pytest.mark.asyncio
async def test_permanent_deletion_never_targets_reused_integer_id(administration):
    removed_id = await add(administration, "remove")
    replaced_id = await add(administration, "keep")
    administration._deletion_semaphore = asyncio.Semaphore(0)
    accepted = await administration.start_deletion(
        [removed_id, replaced_id, 99999], actor="webui:tester", mode="permanent",
    )
    operation_id = accepted["operation_id"]
    db = administration.engine.db_connection
    await db.execute("UPDATE documents SET doc_id='replacement-uuid' WHERE id=?", (replaced_id,))
    await db.commit()
    administration._deletion_semaphore.release()
    await administration._jobs[operation_id]

    status = await administration.deletion_status(operation_id, actor="webui:tester")
    assert status["status"] == "partial" and not status["can_restore"]
    assert status["deleted_count"] == 1 and status["processed_count"] == 3
    assert set(status["failed_ids"]) == {replaced_id, 99999}
    assert await administration.snapshot(removed_id) is None
    assert await administration.snapshot(replaced_id) is not None


@pytest.mark.asyncio
async def test_restart_reports_interrupted_and_keeps_committed_restore_receipts(administration):
    memory_id = await add(administration, "restore after restart")
    accepted = await administration.start_deletion([memory_id], actor="webui:tester")
    operation_id = accepted["operation_id"]
    await administration._jobs[operation_id]
    await administration.store.update(operation_id, status="running")

    fresh = MemoryAdministration(administration.engine, administration.config)
    await fresh.initialize()
    status = await fresh.deletion_status(operation_id, actor="webui:tester")
    assert status["status"] == "interrupted" and status["can_restore"]
    assert status["deleted_count"] == 1
    assert (await fresh.restore(operation_id, actor="webui:tester"))["ids"] == [memory_id]
    restored = await fresh.deletion_status(operation_id, actor="webui:tester")
    assert restored["status"] == "restored" and not restored["can_restore"]
    assert restored["deleted_count"] == 1


@pytest.mark.asyncio
async def test_shutdown_settles_current_batch_without_starting_another(administration, monkeypatch):
    monkeypatch.setattr(deletion_jobs, "RECOVERABLE_BATCH_SIZE", 1)
    first, second = await add(administration), await add(administration, "untouched")
    started = asyncio.Event()
    finish = asyncio.Event()
    archive = administration.engine.archive_memories

    async def staged_archive(memory_ids, **options):
        started.set()
        await finish.wait()
        return await archive(memory_ids, **options)

    monkeypatch.setattr(administration.engine, "archive_memories", staged_archive)
    accepted = await administration.start_deletion([first, second], actor="webui:tester")
    operation_id = accepted["operation_id"]
    await asyncio.wait_for(started.wait(), 5)
    closing = asyncio.create_task(administration.close_job_tasks())
    await asyncio.sleep(0)
    finish.set()
    await asyncio.wait_for(closing, 5)

    status = await administration.deletion_status(operation_id, actor="webui:tester")
    assert status["status"] == "interrupted" and status["can_restore"]
    assert status["deleted_count"] == 1 and status["processed_count"] == 1
    assert (await administration.snapshot(second))["metadata"]["status"] == "active"


@pytest.mark.asyncio
async def test_restart_reconciles_permanent_deletions_by_document_uuid(administration):
    removed_id = await add(administration, "already deleted")
    remaining_id = await add(administration, "attempt interrupted")
    pending_id = await add(administration, "not attempted")
    ids = [removed_id, remaining_id, pending_id]
    documents = await administration._deletion_document_ids(ids)
    operation_id = await administration._create(
        "permanent_delete", "webui:tester",
        {"mode": "permanent", "memory_ids": ids, "document_ids": documents}, "running",
    )
    await administration.store.reserve_deletion_items(operation_id, ids)
    await administration.store.mark_deletion_items(operation_id, ids[:2], "deleting")
    assert await administration.engine.batch_delete_memories([removed_id]) == 1

    fresh = MemoryAdministration(administration.engine, administration.config)
    await fresh.initialize()
    status = await fresh.deletion_status(operation_id, actor="webui:tester")
    assert status["status"] == "interrupted"
    assert status["deleted_count"] == 1 and status["processed_count"] == 2
    assert status["failed_ids"] == [remaining_id]
    assert await administration.snapshot(remaining_id) is not None
    assert await administration.snapshot(pending_id) is not None


@pytest.mark.asyncio
async def test_permanent_delete_repair_preserves_new_document_with_reused_id(administration):
    engine = administration.engine
    memory_id = await add(administration, "a replacement under the old integer ID")
    payload = {"memory_ids": [memory_id], "document_ids": {str(memory_id): "removed-original-uuid"}}
    op_id = await engine._start_write_op("batch_delete", payload)
    assert await engine._repair_batch_delete_write_op(op_id, payload)
    assert await administration.snapshot(memory_id) is not None


@pytest.mark.asyncio
async def test_delete_job_receipt_capacity_rejection_does_not_archive(administration):
    memory_id = await add(administration, "cannot reserve recovery snapshot")
    administration.store.max_snapshot_bytes = 1
    accepted = await administration.start_deletion([memory_id], actor="webui:tester")
    operation_id = accepted["operation_id"]
    await administration._jobs[operation_id]
    status = await administration.deletion_status(operation_id, actor="webui:tester")
    assert status["failed_ids"] == [memory_id] and status["deleted_count"] == 0
    assert (await administration.snapshot(memory_id))["metadata"]["status"] == "active"


@pytest.mark.asyncio
async def test_corrupt_source_does_not_block_other_snapshots_in_batch(administration):
    corrupt, valid = await add(administration), await add(administration, "valid source")
    db = administration.engine.db_connection
    await db.execute("UPDATE memory_sources SET source_json='{}' WHERE memory_id=?", (corrupt,))
    await db.commit()
    accepted = await administration.start_deletion([corrupt, valid], actor="webui:tester")
    operation_id = accepted["operation_id"]
    await administration._jobs[operation_id]
    status = await administration.deletion_status(operation_id, actor="webui:tester")
    assert status["status"] == "partial" and status["failed_ids"] == [corrupt]
    assert status["deleted_count"] == 1 and status["can_restore"]
    assert (await administration.snapshot(corrupt))["metadata"]["status"] == "active"
    assert (await administration.restore(operation_id, actor="webui:tester"))["ids"] == [valid]
