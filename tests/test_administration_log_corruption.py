"""Malformed durable journals never authorize writes or hide unrelated records."""

import json
import sqlite3

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_administration import (
    MemoryAdministration,
)
from astrbot_plugin_anamnesis.storage.administration_store import (
    AdministrationRecordError,
)
from quart import g

from tests.test_archive_restore_index_consistency import (
    APP,
    SCOPE,
    make_service,
    request_admin,
)
from tests.test_memory_transfer_runtime_safety import _api


async def set_log_field(service, table, field, raw, operation_id, item_key=None):
    async with service.store.connection(write=True) as db:
        where = "id=?" if item_key is None else "operation_id=? AND item_key=?"
        args = (
            (raw, operation_id)
            if item_key is None
            else (raw, operation_id, str(item_key))
        )
        await db.execute(f"UPDATE {table} SET {field}=? WHERE {where}", args)


async def raw_log(service, table, operation_id, item_key=None):
    async with service.store.connection() as db:
        where = "id=?" if item_key is None else "operation_id=? AND item_key=?"
        args = (operation_id,) if item_key is None else (operation_id, str(item_key))
        return dict(
            await (
                await db.execute(f"SELECT * FROM {table} WHERE {where}", args)
            ).fetchone()
        )


async def list_public(engine):
    async with APP.test_request_context("/operations", method="GET"):
        g.username = "a"
        return await _api(engine).administration_request("list")


async def preview(service, content="original"):
    memory_id = await service.engine.add_memory(
        content,
        session_id=SCOPE,
        source_messages=[{"role": "user", "content": content}],
    )
    plan = await service.preview_correction(
        memory_id, "correct", actor="webui:a", new_content="corrected"
    )
    return memory_id, plan["operation_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("purge", [False, True])
@pytest.mark.parametrize(
    "damage",
    [
        "json",
        "payload_list",
        "result_json",
        "before",
        "before_metadata",
        "after",
        "tagged",
    ],
)
async def test_bad_recovery_item_is_local_and_original_log_survives_retry(
    tmp_path, damage, purge
):
    engine, service = await make_service(tmp_path)
    try:
        ids = [
            await engine.add_memory(f"fact {index}", session_id=SCOPE)
            for index in range(2)
        ]
        deleted = await service.delete(ids, actor="tool:scope:owner")
        operation_id = deleted["operation_id"]
        original = await raw_log(service, "memory_admin_items", operation_id, ids[0])
        payload = json.loads(original["payload"])
        field = "payload"
        if damage == "json":
            raw = "{"
        elif damage == "payload_list":
            raw = "[]"
        elif damage == "result_json":
            field, raw = "result", "{"
        else:
            if damage == "before_metadata":
                payload["before"] = {**payload["before"], "metadata": []}
            else:
                payload[damage] = None if damage == "before" else "invalid snapshot"
            raw = json.dumps(payload)
        await set_log_field(
            service, "memory_admin_items", field, raw, operation_id, ids[0]
        )
        damaged = await raw_log(service, "memory_admin_items", operation_id, ids[0])
        with pytest.raises(ValueError, match="不属于"):
            await service.restore(operation_id, actor="tool:scope:other", purge=purge)
        result = await request_admin(
            engine, "purge" if purge else "restore", {"operation_id": operation_id}
        )
        assert result["status"] == "ok"
        assert result["data"]["ids"] == ids[1:]
        assert result["data"]["conflicts"] == ids[:1]
        assert (
            await raw_log(service, "memory_admin_items", operation_id, ids[0])
            == damaged
        )
        assert (await service.snapshot(ids[0]))["metadata"]["status"] == "archived"
        await set_log_field(
            service, "memory_admin_items", field, original[field], operation_id, ids[0]
        )
        reopened = MemoryAdministration(engine, service.config)
        retried = await reopened.restore(operation_id, actor="webui:a", purge=purge)
        assert retried["ids"] == (ids[:1] if purge else ids)
        assert retried["conflicts"] == []
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["payload", "result"])
@pytest.mark.parametrize("raw", ["{", "[]", "null", '"text"'])
async def test_corrupt_operation_is_visible_but_not_authorized(tmp_path, field, raw):
    engine, service = await make_service(tmp_path)
    try:
        operation_id = await service._create(
            "delete", "tool:scope:owner", {"memory_ids": [1]}, "partial"
        )
        valid_id = await service._create(
            "delete", "webui:a", {"memory_ids": [2]}, "partial"
        )
        await set_log_field(
            service, "memory_admin_operations", field, raw, operation_id
        )
        before = await raw_log(service, "memory_admin_operations", operation_id)
        result = await list_public(engine)
        assert result["status"] == "ok"
        listed = {row["id"]: row for row in result["data"]}
        assert listed[operation_id]["corrupt_fields"] == [field]
        assert "corrupt_fields" not in listed[valid_id]
        with pytest.raises(AdministrationRecordError):
            await service.store.get(operation_id)
        with pytest.raises(ValueError, match="不属于"):
            await service.restore(operation_id, actor="tool:scope:other")
        with pytest.raises(AdministrationRecordError):
            await service.restore(operation_id, actor="tool:scope:owner")
        assert await raw_log(service, "memory_admin_operations", operation_id) == before
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "execute"])
@pytest.mark.parametrize(
    "damage", ["before", "metadata", "source", "fingerprint", "memory_id"]
)
async def test_invalid_correction_snapshot_cannot_issue_or_consume_token(
    tmp_path, action, damage
):
    engine, service = await make_service(tmp_path)
    try:
        memory_id, operation_id = await preview(service)
        token = None
        if action == "execute":
            token = (await service.confirm(operation_id, actor="webui:a"))[
                "confirmation_token"
            ]
        operation = await service.store.get(operation_id)
        payload = operation["payload"]
        if damage == "before":
            payload["before"] = None
        elif damage in {"metadata", "source"}:
            field = "metadata" if damage == "metadata" else "source_messages"
            payload["before"][field] = (
                [] if damage == "metadata" else ["invalid source"]
            )
        elif damage == "fingerprint":
            payload["before"]["text"] = "untrusted changed snapshot"
        else:
            payload["memory_id"] = True
        await set_log_field(
            service,
            "memory_admin_operations",
            "payload",
            json.dumps(payload),
            operation_id,
        )
        before = await raw_log(service, "memory_admin_operations", operation_id)
        response = await request_admin(
            engine, action, {"operation_id": operation_id, "confirmation_token": token}
        )
        assert response["status"] == "error"
        assert "confirmation_token" not in response.get("data", {})
        assert await raw_log(service, "memory_admin_operations", operation_id) == before
        assert (await service.snapshot(memory_id))["text"] == "original"
        rows = await (
            await engine.db_connection.execute("SELECT id FROM documents")
        ).fetchall()
        assert [row[0] for row in rows] == [memory_id]
        listed = {row["id"]: row for row in (await list_public(engine))["data"]}
        assert listed[operation_id]["corrupt_fields"] == ["correction_log"]
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["after", "after_metadata", "after_fingerprint", "new_id"]
)
async def test_invalid_committed_snapshot_cannot_authorize_rollback(tmp_path, damage):
    engine, service = await make_service(tmp_path)
    try:
        _, operation_id = await preview(service)
        token = await service.confirm(operation_id, actor="webui:a")
        corrected = await service.execute_correction(
            operation_id, token["confirmation_token"], actor="webui:a"
        )
        corrected_id = corrected["new_memory_id"]
        snapshot = await service.snapshot(corrected_id)
        result = (await service.store.get(operation_id))["result"]
        if damage == "after":
            result["after"] = "invalid snapshot"
        elif damage == "after_metadata":
            result["after"]["metadata"] = []
        elif damage == "after_fingerprint":
            result["after"]["text"] = "changed journal"
        else:
            result["new_memory_id"] = True
        await set_log_field(
            service,
            "memory_admin_operations",
            "result",
            json.dumps(result),
            operation_id,
        )
        raw = await raw_log(service, "memory_admin_operations", operation_id)
        response = await request_admin(
            engine, "rollback", {"operation_id": operation_id}
        )
        assert response["status"] == "error"
        assert await service.snapshot(corrected_id) == snapshot
        assert await raw_log(service, "memory_admin_operations", operation_id) == raw
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["result_json", "result_list", "missing_commit", "before"]
)
async def test_startup_quarantines_only_bad_journal_and_retains_bytes_across_restarts(
    tmp_path, damage
):
    engine, service = await make_service(tmp_path)
    try:
        memory_id, bad_id = await preview(service, "bad journal original")
        await service.store.update(bad_id, status="prepared_commit")
        if damage == "result_json":
            await set_log_field(
                service, "memory_admin_operations", "result", "{", bad_id
            )
        elif damage == "result_list":
            await set_log_field(
                service, "memory_admin_operations", "result", "[]", bad_id
            )
        elif damage == "before":
            payload = (await service.store.get(bad_id))["payload"]
            payload["before"] = None
            await set_log_field(
                service,
                "memory_admin_operations",
                "payload",
                json.dumps(payload),
                bad_id,
            )
        raw_before = await raw_log(service, "memory_admin_operations", bad_id)
        original = await service.snapshot(memory_id)
        _, valid_id = await preview(service, "valid journal original")
        token = await service.confirm(valid_id, actor="webui:a")
        await service.execute_correction(
            valid_id, token["confirmation_token"], actor="webui:a"
        )
        await service.store.update(valid_id, status="prepared_commit")
        for _ in range(3):
            service = MemoryAdministration(engine, service.config)
            engine.memory_administration = service
            service.store.max_bytes = await raw_log_usage(service)
            service.store.max_snapshot_bytes = 1
            await service.initialize()
            raw_after = await raw_log(service, "memory_admin_operations", bad_id)
            assert raw_after["status"] == "needs_review"
            assert raw_after["payload"] == raw_before["payload"]
            assert raw_after["result"] == raw_before["result"]
            assert await service.snapshot(memory_id) == original
            assert (await service.store.get(valid_id))["status"] == "completed"
            response = await list_public(engine)
            assert response["status"] == "ok"
            bad = next(row for row in response["data"] if row["id"] == bad_id)
            assert bad["corrupt_fields"]
    finally:
        await engine.close()


async def raw_log_usage(service):
    async with service.store.connection() as db:
        return (
            await (
                await db.execute("SELECT used_bytes FROM memory_admin_quota WHERE id=1")
            ).fetchone()
        )[0]


@pytest.mark.asyncio
async def test_store_read_infrastructure_failures_still_propagate(tmp_path):
    engine, service = await make_service(tmp_path)
    try:
        async with service.store.connection(write=True) as db:
            await db.execute("DROP TABLE memory_admin_items")
        with pytest.raises(sqlite3.OperationalError):
            await service.store.items("unknown")
    finally:
        await engine.close()
