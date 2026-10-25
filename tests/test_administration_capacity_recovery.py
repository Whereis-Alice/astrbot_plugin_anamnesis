"""Public recovery remains truthful and retryable under real SQLite quotas."""

import json

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_administration import (
    MemoryAdministration,
)
from astrbot_plugin_anamnesis.storage.administration_store import (
    AdministrationCapacityError,
    AdministrationStore,
)

from tests.test_archive_restore_index_consistency import (
    SCOPE,
    make_service,
    request_admin,
)


async def reopen_service(engine, service):
    reopened = MemoryAdministration(engine, service.config)
    engine.memory_administration = reopened
    await reopened.initialize()
    return reopened


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["total", "record"])
@pytest.mark.parametrize("purge", [False, True])
async def test_summary_quota_preserves_actual_outcome_and_retries(
    tmp_path, monkeypatch, limit, purge
):
    engine, service = await make_service(tmp_path)
    try:
        source = [{"role": "user", "content": "exact source"}]
        memory_id = await engine.add_memory(
            "fact", session_id=SCOPE, source_messages=source
        )
        deleted = await service.delete([memory_id], actor="webui:a")
        operation_id = deleted["operation_id"]
        # A prior interrupted summary remains smaller than the final result.
        # It must not survive as misleading evidence of the current outcome.
        await service.store.update(operation_id, result={"deleted_ids": [memory_id]})
        item_before = (await service.store.items(operation_id))[0]
        service = await reopen_service(engine, service)
        original_limits = service.store.max_bytes, service.store.max_record_bytes
        if limit == "total":
            service.store.max_bytes = (
                1 if purge else (await service.store.storage_usage())["used_bytes"]
            )
        else:
            service.store.max_record_bytes = 64
        calls = []
        mutate = engine.delete_memory if purge else engine.restore_memory

        async def record_mutation(memory_id):
            calls.append(memory_id)
            return await mutate(memory_id)

        monkeypatch.setattr(
            engine, "delete_memory" if purge else "restore_memory", record_mutation
        )
        action = "purge" if purge else "restore"
        response = await request_admin(engine, action, {"operation_id": operation_id})
        assert response["status"] == "ok"
        result = response["data"]
        assert result == {
            "operation_id": operation_id,
            "ids": [memory_id],
            "conflicts": [],
            "purged": purge,
            "result_persisted": False,
        }
        operation = await service.store.get(operation_id)
        assert operation["status"] == "partial" and operation["result"] == {}
        item = (await service.store.items(operation_id))[0]
        assert item["status"] == ("purged" if purge else "restored")
        assert item["payload"] == ({} if purge else item_before["payload"])
        if purge:
            assert await service.snapshot(memory_id) is None
        else:
            assert (await service.snapshot(memory_id))["metadata"]["status"] == "active"
            assert await engine.get_memory_source(memory_id) == source
        # Reopening under the same bounds retains the durable item phase.
        limits = service.store.max_bytes, service.store.max_record_bytes
        service = await reopen_service(engine, service)
        service.store.max_bytes, service.store.max_record_bytes = limits
        still_full = await request_admin(engine, action, {"operation_id": operation_id})
        assert still_full["status"] == "ok"
        assert still_full["data"]["ids"] == ([] if purge else [memory_id])
        assert still_full["data"]["result_persisted"] is False
        service.store.max_bytes, service.store.max_record_bytes = original_limits
        retried = await request_admin(engine, action, {"operation_id": operation_id})
        assert retried["status"] == "ok"
        assert retried["data"]["ids"] == ([] if purge else [memory_id])
        operation = await service.store.get(operation_id)
        assert operation["status"] == ("purged" if purge else "restored")
        assert operation["result"] == retried["data"]
        assert calls == [memory_id]
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("purge", [False, True])
@pytest.mark.parametrize("limit", ["snapshot", "record"])
async def test_retained_item_phase_can_finish_after_limits_are_lowered(
    tmp_path, purge, limit
):
    engine, service = await make_service(tmp_path)
    try:
        source = [{"role": "user", "content": "large exact source" * 20}]
        memory_id = await engine.add_memory(
            "fact", session_id=SCOPE, source_messages=source
        )
        deleted = await service.delete([memory_id], actor="webui:a")
        operation_id = deleted["operation_id"]
        before = (await service.store.items(operation_id))[0]
        service = await reopen_service(engine, service)
        if limit == "snapshot":
            service.store.max_snapshot_bytes = 1
        else:
            service.store.max_record_bytes = 200
        response = await request_admin(
            engine, "purge" if purge else "restore", {"operation_id": operation_id}
        )
        assert response["status"] == "ok"
        assert response["data"]["ids"] == [memory_id]
        assert response["data"]["conflicts"] == []
        item = (await service.store.items(operation_id))[0]
        assert item["status"] == ("purged" if purge else "restored")
        assert item["payload"] == ({} if purge else before["payload"])
        if not purge:
            assert await engine.get_memory_source(memory_id) == source
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["payload", "result"])
@pytest.mark.parametrize("limit", ["total", "record", "snapshot"])
async def test_reopened_store_exempts_only_identical_item_content(
    tmp_path, change, limit
):
    store = AdministrationStore(str(tmp_path / "memory.db"))
    await store.initialize()
    await store.create("op", "delete", "webui:a", {})
    payload = {"before": {"text": "retained evidence" * 20}}
    result = {"after": {"text": "retained result" * 20}}
    await store.put_item("op", 1, payload, result=result, status="deleted")
    store = AdministrationStore(store.db_path)
    await store.initialize()
    original_used = (await store.storage_usage())["used_bytes"]
    if limit == "total":
        store.max_bytes = original_used
    elif limit == "record":
        store.max_record_bytes = 10
    else:
        store.max_snapshot_bytes = 1
    await store.put_item("op", 1, payload, result=result, status="restoring")
    assert (await store.storage_usage())["used_bytes"] == original_used
    changed_payload, changed_result = payload, result
    if change == "payload":
        changed_payload = {"before": {"text": payload["before"]["text"] + "new"}}
    else:
        changed_result = {"after": {"text": result["after"]["text"] + "new"}}
    with pytest.raises(AdministrationCapacityError):
        await store.put_item(
            "op", 1, changed_payload, result=changed_result, status="restored"
        )
    with pytest.raises(AdministrationCapacityError):
        await store.put_item("op", 2, payload, result=result, status="restored")
    item = (await store.items("op"))[0]
    assert item["status"] == "restoring"
    assert item["payload"] == payload and item["result"] == result
    assert len(await store.items("op")) == 1
    assert (await store.storage_usage())["used_bytes"] == original_used


@pytest.mark.asyncio
@pytest.mark.parametrize("purge", [False, True])
@pytest.mark.parametrize("corrupt_source", ["{", "{}", "null", '["invalid"]'])
async def test_bad_source_is_local_and_retry_preserves_other_items(
    tmp_path, purge, corrupt_source
):
    engine, service = await make_service(tmp_path)
    try:
        ids, sources = [], {}
        for index in range(2):
            source = [{"role": "user", "content": f"source {index}"}]
            memory_id = await engine.add_memory(
                f"fact {index}", session_id=SCOPE, source_messages=source
            )
            ids.append(memory_id)
            sources[memory_id] = source
        deleted = await service.delete(ids, actor="tool:scope:owner")
        operation_id = deleted["operation_id"]
        await engine.db_connection.execute(
            "UPDATE memory_sources SET source_json=? WHERE memory_id=?",
            (corrupt_source, ids[0]),
        )
        await engine.db_connection.commit()
        service = await reopen_service(engine, service)
        with pytest.raises(ValueError, match="不属于"):
            await service.restore(operation_id, actor="tool:scope:other", purge=purge)
        response = await request_admin(
            engine, "purge" if purge else "restore", {"operation_id": operation_id}
        )
        assert response["status"] == "ok"
        assert response["data"]["ids"] == ids[1:]
        assert response["data"]["conflicts"] == ids[:1]
        items = await service.store.items(operation_id)
        assert items[0]["status"] == "deleted"
        assert items[0]["payload"]["before"]["source_messages"] == sources[ids[0]]
        assert items[1]["status"] == ("purged" if purge else "restored")
        assert (await service.store.get(operation_id))["status"] == "partial"
        await engine.db_connection.execute(
            "UPDATE memory_sources SET source_json=? WHERE memory_id=?",
            (json.dumps(sources[ids[0]]), ids[0]),
        )
        await engine.db_connection.commit()
        service = await reopen_service(engine, service)
        retried = await service.restore(operation_id, actor="webui:a", purge=purge)
        assert retried["ids"] == (ids[:1] if purge else ids)
        assert retried["conflicts"] == []
        for memory_id in ids:
            if purge:
                assert await service.snapshot(memory_id) is None
            else:
                assert (await service.snapshot(memory_id))["metadata"][
                    "status"
                ] == "active"
                assert await engine.get_memory_source(memory_id) == sources[memory_id]
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("purge", [False, True])
async def test_infrastructure_errors_are_not_reported_as_item_conflicts(
    tmp_path, monkeypatch, purge
):
    engine, service = await make_service(tmp_path)
    try:
        ids = [await engine.add_memory(f"fact {i}", session_id=SCOPE) for i in range(2)]
        deleted = await service.delete(ids, actor="webui:a")
        snapshot = service.snapshot

        async def unavailable(memory_id):
            if memory_id == ids[0]:
                raise OSError("snapshot storage unavailable")
            return await snapshot(memory_id)

        monkeypatch.setattr(service, "snapshot", unavailable)
        with pytest.raises(OSError, match="storage unavailable"):
            await service.restore(deleted["operation_id"], actor="webui:a", purge=purge)
        assert all(
            item["status"] == "deleted"
            for item in await service.store.items(deleted["operation_id"])
        )
        assert (await snapshot(ids[1]))["metadata"]["status"] == "archived"
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["total", "record", "snapshot"])
async def test_rollback_quota_keeps_corrected_source_and_retry_is_idempotent(
    tmp_path, limit
):
    engine, service = await make_service(tmp_path)
    try:
        source = [{"role": "user", "content": "original exact source"}]
        original_id = await engine.add_memory(
            "original fact", session_id=SCOPE, source_messages=source
        )
        preview = await service.preview_correction(
            original_id, "correct", actor="webui:a", new_content="corrected fact"
        )
        operation_id = preview["operation_id"]
        token = await service.confirm(operation_id, actor="webui:a")
        corrected = await service.execute_correction(
            operation_id, token["confirmation_token"], actor="webui:a"
        )
        corrected_id = corrected["new_memory_id"]
        corrected_snapshot = await service.snapshot(corrected_id)
        saved_operation = await service.store.get(operation_id)
        service = await reopen_service(engine, service)
        limits = (
            service.store.max_bytes,
            service.store.max_record_bytes,
            service.store.max_snapshot_bytes,
        )
        if limit == "total":
            service.store.max_bytes = (await service.store.storage_usage())[
                "used_bytes"
            ]
        elif limit == "record":
            service.store.max_record_bytes = len(
                json.dumps(saved_operation["result"], ensure_ascii=False).encode()
            )
        else:
            service.store.max_snapshot_bytes = 1
        response = await request_admin(
            engine, "rollback", {"operation_id": operation_id}
        )
        assert response["status"] == "error"
        assert await service.snapshot(corrected_id) == corrected_snapshot
        operation = await service.store.get(operation_id)
        assert operation["status"] == "completed"
        assert operation["payload"] == saved_operation["payload"]
        assert operation["result"] == saved_operation["result"]
        rows = await (
            await engine.db_connection.execute("SELECT id FROM documents")
        ).fetchall()
        assert [row[0] for row in rows] == [corrected_id]
        (
            service.store.max_bytes,
            service.store.max_record_bytes,
            service.store.max_snapshot_bytes,
        ) = limits
        retried = await service.rollback_correction(operation_id, actor="webui:a")
        assert (await service.snapshot(retried["memory_id"]))["text"] == "original fact"
        assert await engine.get_memory_source(retried["memory_id"]) == source
        repeated = await service.rollback_correction(operation_id, actor="webui:a")
        assert repeated == retried
        rows = await (
            await engine.db_connection.execute("SELECT id FROM documents")
        ).fetchall()
        assert [row[0] for row in rows] == [retried["memory_id"]]
    finally:
        await engine.close()
