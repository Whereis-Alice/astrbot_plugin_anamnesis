"""Retry owned import stages without falsely skipping invisible memories."""

import asyncio
import json
import time

import pytest
from astrbot_plugin_anamnesis.core.memory_transfer import ImportEntry
from astrbot_plugin_anamnesis.core.page_api_modules.memory_handler import MemoryHandler
from astrbot_plugin_anamnesis.core.page_api_modules.utils import PageApiUtils

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]
SCOPE = "qq:GroupMessage:1"
CONTENT = "喜欢咖啡"


async def leave_stage(service, monkeypatch, record=None):
    engine = service.engine
    update, delete = engine.update_memory, engine.delete_memory

    async def failed_commit(memory_id, changes):
        if changes.get("metadata", {}).get("status") in ("active", "archived"):
            raise RuntimeError("temporary commit failure")
        return await update(memory_id, changes)

    async def failed_cleanup(memory_id):
        return False

    monkeypatch.setattr(engine, "update_memory", failed_commit)
    monkeypatch.setattr(engine, "delete_memory", failed_cleanup)
    record = record or {"content": CONTENT, "session_id": SCOPE, "persona_id": "p"}
    job = await service.create_job(
        "import", {"format": "json", "content": json.dumps([record])}
    )
    operation_id = job["operation_id"]
    await asyncio.wait_for(asyncio.shield(service._jobs[operation_id]), 3)
    failed = await service.store.get(operation_id)
    assert failed["status"] == "partial" and failed["result"]["failed_count"] == 1
    rows = await engine.get_memory_transfer_records()
    assert len(rows) == 1 and rows[0]["metadata"]["status"] == "importing"
    monkeypatch.setattr(engine, "update_memory", update)
    monkeypatch.setattr(engine, "delete_memory", delete)
    return operation_id, rows[0]["original_id"]


async def retry(service, operation_id):
    await service.job_action(operation_id, "retry")
    await asyncio.wait_for(asyncio.shield(service._jobs[operation_id]), 3)
    return await service.store.get(operation_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,atom_status",
    [
        ("active", None),
        ("active", "expired"),
        ("active", "forgotten"),
        ("archived", "active"),
    ],
)
async def test_owned_stage_retry_preserves_id_sources_time_and_atom_lifecycle(
    administration, monkeypatch, status, atom_status
):
    administration.engine.atom_enabled = True
    record = {
        "content": CONTENT,
        "session_id": SCOPE,
        "persona_id": "p",
        "metadata": {"create_time": 123.0, "status": status},
        "source_messages": [
            {"role": "user", "content": CONTENT, "sender_id": "u", "session_id": SCOPE}
        ],
    }
    expiry = time.time() - 10 if atom_status == "expired" else time.time() + 3600
    if atom_status is not None:
        record["atoms"] = [
            {
                "content": CONTENT,
                "atom_type": "preference",
                "status": atom_status,
                "created_at": 100.0,
                "expires_at": expiry,
                "reinforcement_count": 3,
            }
        ]
    operation_id, memory_id = await leave_stage(administration, monkeypatch, record)
    operation = await retry(administration, operation_id)
    assert operation["status"] == "completed"
    assert (
        operation["result"]["imported_count"] == 1
        and operation["result"]["skipped_duplicate_count"] == 0
    )
    item = (await administration.store.items(operation_id))[0]
    assert item["result"]["imported_ids"] == [memory_id]
    rows = await administration.engine.get_memory_transfer_records()
    assert len(rows) == 1 and rows[0]["metadata"]["status"] == status
    assert rows[0]["metadata"]["create_time"] == 123.0
    assert rows[0]["source_messages"][0]["content"] == CONTENT
    if atom_status is not None and status == "active":
        atom = (await administration.engine.atom_store.get_by_parent(memory_id))[0]
        assert atom.status.value == atom_status and atom.expires_at == expiry
        assert atom.created_at == 100.0 and atom.reinforcement_count == 3
    if status == "archived" or atom_status in ("expired", "forgotten"):
        current = await administration.snapshot(memory_id)
        assert (
            await administration.engine.resolve_live_facts(
                memory_id, current["text"], current["metadata"]
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["metadata", "source", "receipt", "pending", "other_duplicate", "text", "scope"],
)
async def test_uncertain_stage_remains_failed_with_its_memory_retained(
    administration, monkeypatch, change
):
    operation_id, memory_id = await leave_stage(administration, monkeypatch)
    engine = administration.engine
    if change == "metadata":
        assert await engine.update_memory(
            memory_id, {"metadata": {"independent_edit": True}}
        )
    elif change == "source":
        await engine.save_memory_source(
            memory_id, [{"role": "user", "content": "changed"}]
        )
    elif change == "receipt":
        assert await engine.update_memory(
            memory_id, {"metadata": {"import_operation_id": "another-operation"}}
        )
    elif change == "text":
        assert await engine.update_memory(memory_id, {"content": "新的暂存内容"})
        memory_id = (await engine.get_memory_transfer_records())[0]["original_id"]
    elif change == "scope":
        assert await engine.update_memory(
            memory_id, {"metadata": {"session_id": "elsewhere"}}
        )
    elif change == "pending":
        await engine.db_connection.execute(
            "UPDATE memory_write_ops SET status='needs_repair' WHERE memory_id=?",
            (memory_id,),
        )
        await engine.db_connection.commit()
    else:
        await add(administration)
    operation = await retry(administration, operation_id)
    assert operation["status"] in ("failed", "partial")
    current = await administration.snapshot(memory_id)
    assert current is not None and current["metadata"]["status"] == "importing"
    assert len(await engine.get_memory_transfer_records()) == (
        2 if change == "other_duplicate" else 1
    )


@pytest.mark.asyncio
async def test_unowned_stage_is_an_error_instead_of_a_successful_duplicate(
    administration, monkeypatch
):
    _, memory_id = await leave_stage(administration, monkeypatch)
    entry = ImportEntry(0, CONTENT, session_id=SCOPE, persona_id="p")
    result = (
        await MemoryHandler(PageApiUtils())._import_entries(
            administration.engine, None, [entry], [], "skip"
        )
    )["data"]
    assert result["failed_count"] == 1
    assert result["imported_count"] == result["skipped_duplicate_count"] == 0
    assert str(memory_id) in result["errors"][0]["error"]


@pytest.mark.asyncio
async def test_cancelled_stage_resume_finishes_commit_and_recovers_receipt_on_retry(
    administration, monkeypatch
):
    operation_id, memory_id = await leave_stage(administration, monkeypatch)
    engine = administration.engine
    update = engine.update_memory
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused_commit(*args, **kwargs):
        entered.set()
        await release.wait()
        return await update(*args, **kwargs)

    monkeypatch.setattr(engine, "update_memory", paused_commit)
    await administration.job_action(operation_id, "retry")
    task = administration._jobs[operation_id]
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        monkeypatch.setattr(engine, "update_memory", update)
        operation = await retry(administration, operation_id)
        assert (
            operation["status"] == "completed"
            and operation["result"]["imported_count"] == 1
        )
        assert (await administration.store.items(operation_id))[0]["result"][
            "imported_ids"
        ] == [memory_id]
        assert len(await engine.get_memory_transfer_records()) == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
