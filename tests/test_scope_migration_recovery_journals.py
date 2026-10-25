"""Scope changes must not invalidate retained delete and correction snapshots."""

import time
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_administration import (
    MemoryAdministration,
)
from astrbot_plugin_anamnesis.core.managers.scope_migrator import ScopeMigrator

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]
SOURCE = "qq:GroupMessage:1"
TARGET = "qq:GroupMessage:2"


async def migrator_for(service, tmp_path, *, persona=None):
    migrator = ScopeMigrator(service.engine, backup_dir=tmp_path / "migration-backups")
    plan = await migrator.preview(SOURCE, TARGET, persona)
    return migrator, plan


async def reject_unchanged(service, tmp_path, *, correction_pending=False):
    migrator, plan = await migrator_for(service, tmp_path)
    async with service.engine.db_connection.execute(
        "SELECT id,text,metadata FROM documents ORDER BY id"
    ) as cursor:
        before = [tuple(row) for row in await cursor.fetchall()]
    reason = (
        "correction recovery is pending or still retained"
        if correction_pending
        else "recoverable deletion is unresolved"
    )
    with pytest.raises(ValueError, match=reason):
        await migrator.apply(SOURCE, TARGET, plan["fingerprint"])
    assert not migrator.backup_dir.exists()
    async with service.engine.db_connection.execute(
        "SELECT id,text,metadata FROM documents ORDER BY id"
    ) as cursor:
        assert [tuple(row) for row in await cursor.fetchall()] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_retained_delete_snapshot_blocks_migration_until_restored_or_purged(
    administration, tmp_path, monkeypatch, expired
):
    memory_id = await add(administration)
    survivor_id = await add(administration, "保留书籍")
    deleted = await administration.delete([memory_id], actor="webui:a")
    operation_id = deleted["operation_id"]
    if expired:
        operation = await administration.store.get(operation_id)
        monkeypatch.setattr(time, "time", lambda: operation["expires_at"] + 1)
    await reject_unchanged(administration, tmp_path)
    resolved = await administration.restore(
        operation_id, actor="webui:a", purge=expired
    )
    assert resolved["ids"] == [memory_id] and resolved["conflicts"] == []
    migrator, plan = await migrator_for(administration, tmp_path)
    assert (await migrator.apply(SOURCE, TARGET, plan["fingerprint"]))["applied"]
    assert (await administration.snapshot(survivor_id))["metadata"][
        "session_id"
    ] == TARGET


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "restoring"])
async def test_interrupted_delete_items_require_reconciliation_before_migration(
    administration, tmp_path, monkeypatch, phase
):
    memory_id = await add(administration)
    if phase == "prepared":
        archive = administration.engine.archive_memories
        monkeypatch.setattr(
            administration.engine,
            "archive_memories",
            AsyncMock(side_effect=RuntimeError("archive interrupted")),
        )
        deleted = await administration.delete([memory_id], actor="webui:a")
        monkeypatch.setattr(administration.engine, "archive_memories", archive)
        assert deleted["failed"]
    else:
        deleted = await administration.delete([memory_id], actor="webui:a")
        put_item = administration.store.put_item

        async def fail_restored(*args, **kwargs):
            if kwargs.get("status") == "restored":
                raise OSError("completion unavailable")
            return await put_item(*args, **kwargs)

        monkeypatch.setattr(administration.store, "put_item", fail_restored)
        with pytest.raises(OSError):
            await administration.restore(deleted["operation_id"], actor="webui:a")
        monkeypatch.setattr(administration.store, "put_item", put_item)
    assert (await administration.store.items(deleted["operation_id"]))[0][
        "status"
    ] == phase
    await reject_unchanged(administration, tmp_path)
    assert (await administration.restore(deleted["operation_id"], actor="webui:a"))[
        "ids"
    ] == [memory_id]
    migrator, plan = await migrator_for(administration, tmp_path)
    assert (await migrator.apply(SOURCE, TARGET, plan["fingerprint"]))["applied"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal", ["restored", "purged", "removed", "expired_purged"]
)
async def test_terminal_delete_journals_do_not_block_existing_source_migration(
    administration, tmp_path, monkeypatch, terminal
):
    memory_id = await add(administration)
    deleted = await administration.delete([memory_id], actor="webui:a")
    operation_id = deleted["operation_id"]
    await administration.restore(operation_id, actor="webui:a")
    if terminal in {"purged", "expired_purged"}:
        if terminal == "expired_purged":
            operation = await administration.store.get(operation_id)
            monkeypatch.setattr(time, "time", lambda: operation["expires_at"] + 1)
        await administration.restore(operation_id, actor="webui:a", purge=True)
    elif terminal == "removed":
        await administration.remove_operation(operation_id, actor="webui:a")
    migrator, plan = await migrator_for(administration, tmp_path)
    assert (await migrator.apply(SOURCE, TARGET, plan["fingerprint"]))["applied"]
    assert (await administration.snapshot(memory_id))["metadata"][
        "session_id"
    ] == TARGET


@pytest.mark.asyncio
@pytest.mark.parametrize("unrelated", ["session", "persona", "tag_only"])
async def test_unrelated_delete_journals_or_unreferenced_tags_do_not_block(
    administration, tmp_path, unrelated
):
    memory_id = await add(administration)
    if unrelated == "tag_only":
        assert await administration.engine.update_memory(
            memory_id,
            {"metadata": {"recovery_operation_id": "unreferenced-legacy-tag"}},
        )
    elif unrelated == "session":
        other_id = await add(administration, "其他会话", scope="qq:GroupMessage:3")
        await administration.delete([other_id], actor="webui:a")
    else:
        other_id = await administration.engine.add_memory(
            "其他人格", session_id=SOURCE, persona_id="q"
        )
        await administration.delete([other_id], actor="webui:a")
    migrator, plan = await migrator_for(administration, tmp_path, persona="p")
    assert (await migrator.apply(SOURCE, TARGET, plan["fingerprint"], "p"))["applied"]
    assert (await administration.snapshot(memory_id))["metadata"][
        "session_id"
    ] == TARGET


async def correction(service, *, completion_failure=False, monkeypatch=None):
    memory_id = await add(service)
    preview = await service.preview_correction(
        memory_id, "改为茶", actor="webui:a", new_content="喜欢茶"
    )
    operation_id = preview["operation_id"]
    token = await service.confirm(operation_id, actor="webui:a")
    if completion_failure:
        update = service.store.update

        async def fail_completed(*args, **kwargs):
            if kwargs.get("status") == "completed":
                raise OSError("completion unavailable")
            return await update(*args, **kwargs)

        monkeypatch.setattr(service.store, "update", fail_completed)
        with pytest.raises(OSError):
            await service.execute_correction(
                operation_id, token["confirmation_token"], actor="webui:a"
            )
        monkeypatch.setattr(service.store, "update", update)
    else:
        await service.execute_correction(
            operation_id, token["confirmation_token"], actor="webui:a"
        )
    return operation_id


@pytest.mark.asyncio
async def test_valid_completed_correction_keeps_rollback_available_before_migration(
    administration, tmp_path
):
    operation_id = await correction(administration)
    await reject_unchanged(administration, tmp_path, correction_pending=True)
    rolled_back = await administration.rollback_correction(
        operation_id, actor="webui:a"
    )
    migrator, plan = await migrator_for(administration, tmp_path)
    assert (await migrator.apply(SOURCE, TARGET, plan["fingerprint"]))["applied"]
    assert (await administration.snapshot(rolled_back["memory_id"]))[
        "text"
    ] == "喜欢咖啡"


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", [False, True])
async def test_expired_completed_correction_can_migrate_or_clear_its_log(
    administration, tmp_path, monkeypatch, cleanup
):
    operation_id = await correction(administration)
    operation = await administration.store.get(operation_id)
    monkeypatch.setattr(time, "time", lambda: operation["expires_at"] + 1)
    if cleanup:
        await administration.remove_operation(operation_id, actor="webui:a")
    migrator, plan = await migrator_for(administration, tmp_path)
    assert (await migrator.apply(SOURCE, TARGET, plan["fingerprint"]))["applied"]


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_prepared_correction_requires_reconciliation_even_after_retention(
    administration, tmp_path, monkeypatch, expired
):
    operation_id = await correction(
        administration, completion_failure=True, monkeypatch=monkeypatch
    )
    operation = await administration.store.get(operation_id)
    assert operation["status"] == "prepared_commit"
    if expired:
        monkeypatch.setattr(time, "time", lambda: operation["expires_at"] + 1)
    await reject_unchanged(administration, tmp_path, correction_pending=True)
    fresh = MemoryAdministration(
        administration.engine, administration.config, administration.processor
    )
    await fresh.initialize()
    assert (await fresh.store.get(operation_id))["status"] == "completed"
    if not expired:
        await fresh.rollback_correction(operation_id, actor="webui:a")
    migrator, plan = await migrator_for(fresh, tmp_path)
    assert (await migrator.apply(SOURCE, TARGET, plan["fingerprint"]))["applied"]
