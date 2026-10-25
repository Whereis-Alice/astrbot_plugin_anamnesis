"""A consumed confirmation recovers safely across pre-journal interruption."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_administration import (
    MemoryAdministration,
)

from tests.test_correction_journal import plan

pytest_plugins = ["tests.test_memory_administration"]


async def consumed(service, operation_id):
    async with service.store.connection(write=True) as db:
        await db.execute(
            "UPDATE memory_admin_operations SET status='executing',token_hash=NULL,token_expires=NULL WHERE id=?",
            (operation_id,),
        )


async def fresh(service):
    result = MemoryAdministration(service.engine, service.config, service.processor)
    await result.initialize()
    return result


async def unjournaled_copy(service, operation_id):
    operation = await service.store.get(operation_id)
    payload = operation["payload"]
    before = payload["before"]
    metadata = dict(before["metadata"])
    metadata.update(
        canonical_summary=payload["content"], persona_summary=payload["content"],
        key_facts=payload["key_facts"], correction_operation_id=operation_id,
        previous_id=payload["memory_id"], updated_at=time.time(),
    )
    metadata.pop("archived_atom_lifecycle", None)
    return await service.engine.add_memory(
        payload["content"], session_id=metadata.get("session_id"),
        persona_id=metadata.get("persona_id"), metadata=metadata,
        importance=metadata.get("importance", 0.5), preserve_create_time=True,
        source_messages=before["source_messages"],
    )


@pytest.mark.asyncio
async def test_restart_releases_consumed_claim_when_original_is_untouched(administration):
    old_id, operation_id, _ = await plan(administration)
    await consumed(administration, operation_id)
    recovered = await fresh(administration)
    operation = await recovered.store.get(operation_id)
    assert operation["status"] == "failed"
    assert (await recovered.snapshot(old_id))["text"] == "喜欢咖啡"
    assert operation["payload"]["before"]["source_messages"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_claim_failure_before_replacement_is_reconciled_immediately(
    administration, monkeypatch, error
):
    old_id, operation_id, token = await plan(administration)
    monkeypatch.setattr(administration, "_execute_correction", AsyncMock(side_effect=error()))
    with pytest.raises(error):
        await administration.execute_correction(operation_id, token, actor="webui:a")
    assert (await administration.store.get(operation_id))["status"] == "failed"
    assert (await administration.snapshot(old_id))["text"] == "喜欢咖啡"


@pytest.mark.asyncio
@pytest.mark.parametrize("original_exists", [True, False])
async def test_current_completed_add_without_creation_intent_preserves_both_records(
    administration, original_exists
):
    old_id, operation_id, _ = await plan(administration)
    await consumed(administration, operation_id)
    new_id = await unjournaled_copy(administration, operation_id)
    if not original_exists:
        await administration.engine.delete_memory(old_id)
    recovered = await fresh(administration)
    operation = await recovered.store.get(operation_id)
    assert operation["status"] == "needs_review"
    assert operation["result"]["candidate_ids"] == [new_id]
    assert operation["payload"]["before"]["text"] == "喜欢咖啡"
    with pytest.raises(ValueError):
        await recovered.rollback_correction(operation_id, actor="webui:a")
    with pytest.raises(ValueError):
        await recovered.remove_operation(operation_id, actor="webui:a")
    assert await recovered.snapshot(new_id) is not None
    assert (await recovered.snapshot(old_id) is not None) == original_exists


@pytest.mark.asyncio
async def test_journal_and_replacement_compensation_failure_keep_audit_snapshot(
    administration, monkeypatch
):
    old_id, operation_id, token = await plan(administration)
    update = administration.store.update

    async def fail_prepared(*args, **kwargs):
        if kwargs.get("status") == "prepared_commit":
            raise OSError("journal unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(administration.store, "update", fail_prepared)
    monkeypatch.setattr(administration.engine, "delete_memory", AsyncMock(return_value=False))
    with pytest.raises(OSError):
        await administration.execute_correction(operation_id, token, actor="webui:a")
    operation = await administration.store.get(operation_id)
    assert operation["status"] == "needs_review"
    assert operation["payload"]["before"]["text"] == "喜欢咖啡"
    assert len(operation["result"]["candidate_ids"]) == 1
    assert await administration.snapshot(old_id) is not None
    assert await administration.snapshot(operation["result"]["candidate_ids"][0]) is not None
    with pytest.raises(ValueError):
        await administration.remove_operation(operation_id, actor="webui:a")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["text", "source", "persona", "metadata", "pending", "missing_write_log"])
async def test_unknown_copy_stays_auditable_and_is_never_deleted(
    administration, change
):
    old_id, operation_id, _ = await plan(administration)
    await consumed(administration, operation_id)
    new_id = await unjournaled_copy(administration, operation_id)
    engine = administration.engine
    if change == "source":
        await engine.save_memory_source(new_id, [{"role": "user", "content": "另一条证据"}])
    elif change in ("persona", "metadata"):
        await engine.update_memory(new_id, {"metadata": {"persona_id": "other"} if change == "persona" else {"independent_edit": True}})
    else:
        sql = {
            "text": "UPDATE documents SET text='另一条独立修改' WHERE id=?",
            "pending": "UPDATE memory_write_ops SET status='needs_repair' WHERE memory_id=?",
            "missing_write_log": "DELETE FROM memory_write_ops WHERE memory_id=?",
        }[change]
        await engine.db_connection.execute(sql, (new_id,))
        await engine.db_connection.commit()
    recovered = await fresh(administration)
    operation = await recovered.store.get(operation_id)
    assert operation["status"] == "needs_review"
    assert new_id in operation["result"]["candidate_ids"]
    assert operation["payload"]["before"]["text"] == "喜欢咖啡"
    with pytest.raises(ValueError):
        await recovered.rollback_correction(operation_id, actor="webui:a")
    assert await recovered.snapshot(old_id) is not None
    assert await recovered.snapshot(new_id) is not None


@pytest.mark.asyncio
async def test_pending_unbound_add_keeps_claim_for_later_repair(administration):
    old_id, operation_id, _ = await plan(administration)
    await consumed(administration, operation_id)
    await administration.engine._start_write_op("add", {
        "metadata": {"previous_id": old_id, "correction_operation_id": operation_id},
        "content_preview": "喜欢茶",
    })
    recovered = await fresh(administration)
    assert (await recovered.store.get(operation_id))["status"] == "needs_review"
    assert await recovered.snapshot(old_id) is not None


@pytest.mark.asyncio
async def test_changed_original_keeps_interrupted_snapshot(administration):
    old_id, operation_id, _ = await plan(administration)
    await consumed(administration, operation_id)
    await administration.engine.update_memory(old_id, {"metadata": {"independent_edit": True}})
    recovered = await fresh(administration)
    operation = await recovered.store.get(operation_id)
    assert operation["status"] == "needs_review"
    assert operation["payload"]["before"]["text"] == "喜欢咖啡"


@pytest.mark.asyncio
async def test_initialize_and_guarded_create_use_one_lock_order(administration, monkeypatch):
    service = MemoryAdministration(administration.engine, administration.config, administration.processor)
    initialize = service.store.initialize
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused_initialize():
        entered.set()
        await release.wait()
        await initialize()

    monkeypatch.setattr(service.store, "initialize", paused_initialize)
    initializing = asyncio.create_task(service.initialize())
    creating = None

    async def guarded_create():
        async with administration.engine.memory_mutation_guard():
            return await service._create("delete", "webui:a", {"memory_ids": [1]})

    try:
        await asyncio.wait_for(entered.wait(), 2)
        creating = asyncio.create_task(guarded_create())
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(initializing, creating), 2)
    finally:
        release.set()
        await asyncio.gather(*(task for task in (initializing, creating) if task), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [True, False])
async def test_completed_correction_cleanup_only_after_recovery_deadline(administration, expired):
    old_id, operation_id, token = await plan(administration)
    executed = await administration.execute_correction(operation_id, token, actor="webui:a")
    new_id = executed["new_memory_id"]
    if expired:
        async with administration.store.connection(write=True) as db:
            await db.execute("UPDATE memory_admin_operations SET expires_at=0 WHERE id=?", (operation_id,))
        assert await administration.remove_operation(operation_id, actor="webui:a") == {"removed": True}
        assert await administration.store.get(operation_id) is None
    else:
        with pytest.raises(ValueError):
            await administration.remove_operation(operation_id, actor="webui:a")
        assert (await administration.store.get(operation_id))["status"] == "completed"
    assert await administration.snapshot(old_id) is None
    assert (await administration.snapshot(new_id))["text"] == "喜欢茶"


@pytest.mark.asyncio
async def test_restart_recovers_old_claim_behind_over_500_newer_corrections(administration):
    old_id, operation_id, _ = await plan(administration)
    await consumed(administration, operation_id)
    now = time.time()
    async with administration.store.connection(write=True) as db:
        await db.executemany(
            "INSERT INTO memory_admin_operations(id,kind,actor,status,payload,created_at,updated_at,expires_at) "
            "VALUES(?,'correction','webui:a',?,'{}',?,?,?)",
            [(f"newer_{index:04d}", "completed" if index % 2 else "preview",
              now + index + 1, now + index + 1, now + 86400) for index in range(550)],
        )
    assert operation_id not in {row["id"] for row in await administration.store.list(kind="correction", limit=500)}
    recovered = await fresh(administration)
    assert (await recovered.store.get(operation_id))["status"] == "failed"
    assert (await recovered.snapshot(old_id))["text"] == "喜欢咖啡"


@pytest.mark.asyncio
async def test_restart_keyset_pagination_does_not_skip_claims_when_statuses_change(
    administration, monkeypatch
):
    _, operation_id, _ = await plan(administration)
    operation = await administration.store.get(operation_id)
    now = time.time()
    async with administration.store.connection(write=True) as db:
        await db.executemany(
            "INSERT INTO memory_admin_operations(id,kind,actor,status,payload,created_at,updated_at,expires_at) "
            "VALUES(?,'correction','webui:a','executing',?,?,?,?)",
            [(f"claim_{index:04d}", json.dumps(operation["payload"]),
              now + index + 1, now + index + 1, now + 86400) for index in range(205)],
        )
    recovered = MemoryAdministration(administration.engine, administration.config, administration.processor)
    list_page = recovered.store.list_unresolved_corrections
    page_sizes = []

    async def observed_page(**kwargs):
        page = await list_page(**kwargs)
        page_sizes.append(len(page))
        return page

    monkeypatch.setattr(recovered.store, "list_unresolved_corrections", observed_page)
    await recovered.initialize()
    assert page_sizes == [100, 100, 5, 0]
    async with recovered.store.connection() as db:
        row = await (await db.execute(
            "SELECT COUNT(*) FROM memory_admin_operations WHERE id LIKE 'claim_%' AND status='failed'"
        )).fetchone()
    assert row[0] == 205
