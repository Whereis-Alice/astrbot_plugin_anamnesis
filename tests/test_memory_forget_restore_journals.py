"""Agent restore distinguishes current deletion intent from historical audit tags."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.managers.memory_mutation import memory_mutation_guard
from astrbot_plugin_anamnesis.core.tools.agentic_recall import request_state
from astrbot_plugin_anamnesis.core.tools.memory_forget_tool import MemoryForgetTool

from tests.test_memory_administration import add
from tests.test_memory_forget_tool import _run_context

pytest_plugins = ["tests.test_memory_administration"]
SCOPE = "qq:GroupMessage:1"
OWNER = f"tool:{SCOPE}:alice"
OTHER_OWNER = f"tool:{SCOPE}:bob"


def tool_for(service):
    config = ConfigManager(
        {
            "agent_tools": {
                "enable_forget_tool": True,
                "forget_mode": "recoverable",
                "forget_max_per_request": 10,
                "forget_cooldown_seconds": 0,
            },
            "filtering_settings": {"use_persona_filtering": False},
        }
    )
    return MemoryForgetTool(
        context=Mock(), config_manager=config, memory_engine=service.engine
    )


async def restore(tool, memory_id, context=None):
    return json.loads(
        await tool.call(
            context or _run_context(sender_id="alice", origin=SCOPE),
            memory_id=memory_id,
            reason="用户要求恢复这条记忆",
            action="restore",
        )
    )


async def retained_delete(service, memory_id, *, actor=OWNER):
    result = await service.delete([memory_id], actor=actor)
    assert result["deleted_ids"] == [memory_id] and not result["failed"]
    return result["operation_id"]


@pytest.mark.asyncio
async def test_tool_recovers_a_rearchive_after_its_own_successful_recovery(
    administration,
):
    memory_id = await add(administration)
    before = await administration.snapshot(memory_id)
    tool, context = (
        tool_for(administration),
        _run_context(sender_id="alice", origin=SCOPE),
    )
    deleted = json.loads(
        await tool.call(context, memory_id=memory_id, reason="用户要求遗忘")
    )
    assert deleted["archived"] and deleted["recoverable"]
    operation_id = deleted["operation_id"]
    assert (await restore(tool, memory_id, context))["restored"]
    assert await administration.engine.archive_memories([memory_id]) == 1
    result = await restore(tool, memory_id, context)
    assert result == {"restored": True, "id": memory_id}
    current = await administration.snapshot(memory_id)
    assert current["metadata"]["status"] == "active"
    assert current["source_messages"] == before["source_messages"]
    assert current["metadata"]["recovery_operation_id"] == operation_id
    assert (await administration.store.items(operation_id))[0]["status"] == "restored"
    assert (await restore(tool, memory_id, context))["restored"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "history", ["restored", "purged", "removed", "foreign_owner", "expired"]
)
async def test_terminal_or_removed_journal_does_not_block_a_current_plain_archive(
    administration,
    monkeypatch,
    history,
):
    memory_id = await add(administration)
    actor = OTHER_OWNER if history == "foreign_owner" else OWNER
    operation_id = await retained_delete(administration, memory_id, actor=actor)
    await administration.restore(operation_id, actor=actor)
    if history == "purged":
        await administration.restore(operation_id, actor=actor, purge=True)
    elif history == "removed":
        await administration.remove_operation(operation_id, actor=actor)
        # Exercise the tool's own initialization path after log cleanup.
        administration.engine.memory_administration = None
    elif history == "expired":
        operation = await administration.store.get(operation_id)
        monkeypatch.setattr(time, "time", lambda: operation["expires_at"] + 1)
    assert await administration.engine.archive_memories([memory_id]) == 1
    result = await restore(tool_for(administration), memory_id)
    assert result == {"restored": True, "id": memory_id}
    assert (await administration.snapshot(memory_id))["metadata"]["status"] == "active"
    if history == "removed":
        assert await administration.store.get(operation_id) is None
    else:
        assert (await administration.store.items(operation_id))[0]["status"] == (
            "purged" if history == "purged" else "restored"
        )


@pytest.mark.asyncio
async def test_legacy_unlinked_audit_tag_uses_current_native_archive(administration):
    memory_id = await add(administration)
    assert await administration.engine.update_memory(
        memory_id,
        {
            "metadata": {"recovery_operation_id": "legacy-unlinked-audit"},
        },
    )
    assert await administration.engine.archive_memories([memory_id]) == 1
    assert (await restore(tool_for(administration), memory_id))["restored"]
    current = await administration.snapshot(memory_id)
    assert current["metadata"]["status"] == "active"
    assert current["metadata"]["recovery_operation_id"] == "legacy-unlinked-audit"


@pytest.mark.asyncio
async def test_tag_for_another_item_cannot_restore_that_unrelated_deletion(
    administration,
):
    memory_id, other_id = await add(administration), await add(administration, "喜欢书")
    operation_id = await retained_delete(administration, other_id)
    assert await administration.engine.update_memory(
        memory_id,
        {
            "metadata": {"recovery_operation_id": operation_id},
        },
    )
    assert await administration.engine.archive_memories([memory_id]) == 1
    assert (await restore(tool_for(administration), memory_id))["restored"]
    assert (await administration.snapshot(memory_id))["metadata"]["status"] == "active"
    assert (await administration.snapshot(other_id))["metadata"]["status"] == "archived"
    assert (await administration.store.items(operation_id))[0]["status"] == "deleted"


@pytest.mark.asyncio
async def test_matching_unfinished_delete_keeps_the_owner_journal_path(administration):
    memory_id = await add(administration)
    operation_id = await retained_delete(administration, memory_id)
    result = await restore(tool_for(administration), memory_id)
    assert result == {"restored": True, "id": memory_id}
    assert (await administration.snapshot(memory_id))["metadata"]["status"] == "active"
    assert (await administration.store.items(operation_id))[0]["status"] == "restored"
    assert (await administration.store.get(operation_id))["status"] == "restored"


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["owner", "expired", "fingerprint"])
async def test_unfinished_delete_never_falls_back_around_journal_safety(
    administration,
    monkeypatch,
    conflict,
):
    memory_id = await add(administration)
    operation_id = await retained_delete(
        administration,
        memory_id,
        actor=OTHER_OWNER if conflict == "owner" else OWNER,
    )
    if conflict == "expired":
        operation = await administration.store.get(operation_id)
        monkeypatch.setattr(time, "time", lambda: operation["expires_at"] + 1)
    elif conflict == "fingerprint":
        assert await administration.engine.update_memory(
            memory_id, {"metadata": {"independent_edit": True}}
        )
    original = await administration.snapshot(memory_id)
    native = AsyncMock(wraps=administration.engine.restore_memory)
    monkeypatch.setattr(administration.engine, "restore_memory", native)
    tool, context = (
        tool_for(administration),
        _run_context(sender_id="alice", origin=SCOPE),
    )
    result = await restore(tool, memory_id, context)
    assert not result.get("restored")
    if conflict == "owner":
        assert "不属于" in result["error"]
    elif conflict == "expired":
        assert "期限" in result["error"]
    else:
        assert result["error"] == "operation_failed"
    native.assert_not_awaited()
    assert await administration.snapshot(memory_id) == original
    assert (await administration.store.items(operation_id))[0]["status"] == "deleted"
    assert request_state(context.context.event).get("forget_count", 0) == 0


@pytest.mark.asyncio
async def test_reported_native_success_requires_an_actually_active_document(
    administration,
    monkeypatch,
):
    memory_id = await add(administration)
    assert await administration.engine.archive_memories([memory_id]) == 1
    original = await administration.snapshot(memory_id)
    monkeypatch.setattr(
        administration.engine, "restore_memory", AsyncMock(return_value=True)
    )
    tool, context = (
        tool_for(administration),
        _run_context(sender_id="alice", origin=SCOPE),
    )
    result = await restore(tool, memory_id, context)
    assert not result.get("restored") and result["error"] == "operation_failed"
    assert await administration.snapshot(memory_id) == original
    assert request_state(context.context.event).get("forget_count", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["new_foreign_delete", "restored_then_rearchived"])
async def test_restore_waiting_for_writer_routes_from_the_current_journal(
    administration,
    monkeypatch,
    winner,
):
    memory_id = await add(administration)
    operation_id = await retained_delete(administration, memory_id)
    if winner == "new_foreign_delete":
        await administration.restore(operation_id, actor=OWNER)
    locked, loaded = asyncio.Event(), asyncio.Event()
    read = administration.engine.get_memory

    async def observed_read(*args, **kwargs):
        result = await read(*args, **kwargs)
        if asyncio.current_task().get_name() == "waiting-restore":
            loaded.set()
        return result

    monkeypatch.setattr(administration.engine, "get_memory", observed_read)
    winning_operation = []

    async def writer():
        async with memory_mutation_guard(administration.engine):
            locked.set()
            await loaded.wait()
            if winner == "new_foreign_delete":
                winning_operation.append(
                    await retained_delete(administration, memory_id, actor=OTHER_OWNER)
                )
            else:
                await administration.restore(operation_id, actor=OWNER)
                assert await administration.engine.archive_memories([memory_id]) == 1

    writing = asyncio.create_task(writer())
    tasks = [writing]
    try:
        await asyncio.wait_for(locked.wait(), 5)
        restoring = asyncio.create_task(
            restore(tool_for(administration), memory_id), name="waiting-restore"
        )
        tasks.append(restoring)
        result = await asyncio.wait_for(restoring, 5)
        await asyncio.wait_for(writing, 5)
        current = await administration.snapshot(memory_id)
        if winner == "new_foreign_delete":
            assert not result.get("restored") and "不属于" in result["error"]
            assert current["metadata"]["status"] == "archived"
            assert current["metadata"]["recovery_operation_id"] == winning_operation[0]
            assert (await administration.store.items(winning_operation[0]))[0][
                "status"
            ] == "deleted"
        else:
            assert result == {"restored": True, "id": memory_id}
            assert current["metadata"]["status"] == "active"
            assert (await administration.store.items(operation_id))[0][
                "status"
            ] == "restored"
    finally:
        loaded.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
