"""Correction rollback retains the lifecycle that the authorized execution replaced."""

import asyncio
import time

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_administration import (
    MemoryAdministration,
)
from astrbot_plugin_anamnesis.core.models.memory_atom import (
    AtomStatus,
    AtomType,
    MemoryAtom,
)

pytest_plugins = ["tests.test_memory_administration"]
SCOPE = "qq:GroupMessage:1"
LIFECYCLE_FIELDS = (
    "status",
    "created_at",
    "last_accessed_at",
    "last_reinforced_at",
    "ttl_days",
    "expires_at",
    "reinforcement_count",
    "decay_type",
    "event_time",
)


async def preview(service, *, expires_in=3600):
    engine = service.engine
    engine.atom_enabled = True
    atom = MemoryAtom(
        parent_memory_id=0,
        content="喜欢咖啡",
        atom_type=AtomType.PREFERENCE,
        expires_at=time.time() + expires_in,
        session_id=SCOPE,
        persona_id="p",
    )
    memory_id = await engine.add_memory(
        content="喜欢咖啡",
        session_id=SCOPE,
        persona_id="p",
        metadata={"source_session_id": SCOPE, "key_facts": ["喜欢咖啡"]},
        atoms=[atom],
        preserve_atom_lifecycle=True,
        source_messages=[{"role": "user", "content": "喜欢咖啡"}],
    )
    planned = await service.preview_correction(
        memory_id, "改成茶", actor="webui:a", new_content="喜欢茶"
    )
    token = await service.confirm(planned["operation_id"], actor="webui:a")
    return memory_id, planned["operation_id"], token["confirmation_token"]


async def change_lifecycle(service, memory_id, change, monkeypatch=None):
    engine = service.engine
    before = await engine.snapshot_archived_atom_lifecycle(memory_id)
    atom_id = before[0]["id"]
    if change == "reinforced":
        await engine.atom_store.reinforce(atom_id, new_confidence=0.95)
    elif change == "purged":
        assert await engine.atom_store.delete_by_parent(memory_id) == 1
    elif change == "expired":
        monkeypatch.setattr(time, "time", lambda: before[0]["expires_at"] + 1)
        assert await engine.atom_store.expire_stale_atoms() == 1
    else:
        assert await engine.atom_store.update_status(atom_id, AtomStatus(change))
    latest = await engine.snapshot_archived_atom_lifecycle(memory_id)
    assert latest != before
    return latest


async def assert_rollback(service, operation_id, latest, change):
    result = await service.rollback_correction(operation_id, actor="webui:a")
    memory_id = result["memory_id"]
    current = await service.snapshot(memory_id)
    assert current["text"] == "喜欢咖啡"
    assert current["source_messages"] == [{"role": "user", "content": "喜欢咖啡"}]
    restored = await service.engine.snapshot_archived_atom_lifecycle(memory_id)
    if latest:
        assert [
            {field: row[field] for field in LIFECYCLE_FIELDS} for row in restored
        ] == [{field: row[field] for field in LIFECYCLE_FIELDS} for row in latest]
    else:
        assert restored and all(
            row["status"] == "forgotten" and row["expires_at"] <= time.time()
            for row in restored
        )
    facts = await service.engine.resolve_live_facts(
        memory_id, current["text"], current["metadata"]
    )
    assert facts == (["喜欢咖啡"] if change == "reinforced" else None)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["forgotten", "superseded", "reinforced", "purged", "expired"]
)
async def test_rollback_uses_execution_lifecycle_after_preview(
    administration, monkeypatch, change
):
    memory_id, operation_id, token = await preview(
        administration, expires_in=60 if change == "expired" else 3600
    )
    preview_atoms = (await administration.store.get(operation_id))["payload"][
        "before_atom_lifecycle"
    ]
    latest = await change_lifecycle(administration, memory_id, change, monkeypatch)
    await administration.execute_correction(operation_id, token, actor="webui:a")
    operation = await administration.store.get(operation_id)
    assert operation["payload"]["before_atom_lifecycle"] == latest != preview_atoms
    await assert_rollback(administration, operation_id, latest, change)
    with pytest.raises(ValueError, match="已过期、已使用或不匹配"):
        await administration.execute_correction(operation_id, token, actor="webui:a")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["forgotten", "reinforced", "purged"])
async def test_restart_reconciles_post_commit_failure_with_execution_lifecycle(
    administration,
    monkeypatch,
    change,
):
    memory_id, operation_id, token = await preview(administration)
    latest = await change_lifecycle(administration, memory_id, change)
    update = administration.store.update

    async def fail_completed(*args, **kwargs):
        if kwargs.get("status") == "completed":
            raise OSError("completion unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(administration.store, "update", fail_completed)
    with pytest.raises(OSError, match="completion unavailable"):
        await administration.execute_correction(operation_id, token, actor="webui:a")
    operation = await administration.store.get(operation_id)
    assert operation["status"] == "prepared_commit"
    assert operation["payload"]["before_atom_lifecycle"] == latest
    assert await administration.snapshot(memory_id) is None
    fresh = MemoryAdministration(
        administration.engine, administration.config, administration.processor
    )
    await fresh.initialize()
    assert (await fresh.store.get(operation_id))["status"] == "completed"
    await assert_rollback(fresh, operation_id, latest, change)


@pytest.mark.asyncio
async def test_execution_snapshot_journal_failure_preserves_original_and_lifecycle(
    administration,
    monkeypatch,
):
    memory_id, operation_id, token = await preview(administration)
    latest = await change_lifecycle(administration, memory_id, "forgotten")
    original = await administration.snapshot(memory_id)
    update = administration.store.update

    async def fail_lifecycle(*args, **kwargs):
        if kwargs.get("expected_status") == ("executing",) and "payload" in kwargs:
            raise OSError("execution lifecycle unavailable")
        return await update(*args, **kwargs)

    monkeypatch.setattr(administration.store, "update", fail_lifecycle)
    with pytest.raises(OSError, match="execution lifecycle unavailable"):
        await administration.execute_correction(operation_id, token, actor="webui:a")
    assert await administration.snapshot(memory_id) == original
    assert (
        await administration.engine.snapshot_archived_atom_lifecycle(memory_id)
        == latest
    )
    async with administration.engine.db_connection.execute(
        "SELECT id FROM documents"
    ) as cursor:
        assert [row[0] for row in await cursor.fetchall()] == [memory_id]
    assert (await administration.store.get(operation_id))["status"] == "failed"


@pytest.mark.asyncio
async def test_cancellation_after_execution_snapshot_commit_settles_single_execution(
    administration,
    monkeypatch,
):
    memory_id, operation_id, token = await preview(administration)
    latest = await change_lifecycle(administration, memory_id, "superseded")
    committed, release = asyncio.Event(), asyncio.Event()
    update = administration.store.update

    async def pause_committed_lifecycle(*args, **kwargs):
        result = await update(*args, **kwargs)
        if kwargs.get("expected_status") == ("executing",) and "payload" in kwargs:
            committed.set()
            await release.wait()
        return result

    monkeypatch.setattr(administration.store, "update", pause_committed_lifecycle)
    executing = asyncio.create_task(
        administration.execute_correction(operation_id, token, actor="webui:a")
    )
    try:
        await asyncio.wait_for(committed.wait(), 5)
        executing.cancel()
        await asyncio.sleep(0)
        executing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(executing, 5)
        operation = await administration.store.get(operation_id)
        assert operation["status"] == "completed"
        assert operation["payload"]["before_atom_lifecycle"] == latest
        assert await administration.snapshot(memory_id) is None
        fresh = MemoryAdministration(
            administration.engine, administration.config, administration.processor
        )
        await fresh.initialize()
        await assert_rollback(fresh, operation_id, latest, "superseded")
        with pytest.raises(ValueError, match="已过期、已使用或不匹配"):
            await fresh.execute_correction(operation_id, token, actor="webui:a")
    finally:
        release.set()
        if not executing.done():
            executing.cancel()
        await asyncio.gather(executing, return_exceptions=True)


@pytest.mark.asyncio
async def test_changed_primary_source_keeps_preview_contract_and_original_lifecycle(
    administration,
):
    memory_id, operation_id, token = await preview(administration)
    original_payload = (await administration.store.get(operation_id))["payload"]
    latest = await change_lifecycle(administration, memory_id, "forgotten")
    await administration.engine.save_memory_source(
        memory_id, [{"role": "user", "content": "新的原文"}]
    )
    source = await administration.snapshot(memory_id)
    with pytest.raises(ValueError, match="变化"):
        await administration.execute_correction(operation_id, token, actor="webui:a")
    operation = await administration.store.get(operation_id)
    assert operation["status"] == "stale" and operation["payload"] == original_payload
    assert await administration.snapshot(memory_id) == source
    assert (
        await administration.engine.snapshot_archived_atom_lifecycle(memory_id)
        == latest
    )
