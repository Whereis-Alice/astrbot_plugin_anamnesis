"""Run WebUI deletions in bounded batches and persist per-memory receipts."""

from __future__ import annotations

import asyncio
import json
import time

from astrbot.api import logger

from ...storage.administration_store import AdministrationCapacityError
from .memory_mutation import (
    memory_import_guard,
    memory_mutation_guard,
    memory_mutation_task,
    settle_memory_mutation_task,
)

MAX_DELETION_IDS = 2_000
RECOVERABLE_BATCH_SIZE = 32
PERMANENT_BATCH_SIZE = 200
_DELETED_STATES = {"deleted", "restoring", "restored", "purging", "purged"}
_RECOVERABLE_STATES = {"completed", "partial", "interrupted"}


class DeletionJobsMixin:
    async def start_deletion(
        self, memory_ids: list[int], *, mode: str = "recoverable", actor: str, reason: str = "",
    ) -> dict:
        return await self._track_job_preparation(
            self._start_deletion, memory_ids, mode=mode, actor=actor, reason=reason,
        )

    async def _start_deletion(self, memory_ids, *, mode, actor, reason):
        if mode not in ("recoverable", "permanent"):
            raise ValueError("删除模式无效")
        if mode == "recoverable":
            self.require("recovery")
        ids = self.ids(memory_ids, MAX_DELETION_IDS)
        if any(memory_id > 2**63 - 1 for memory_id in ids):
            raise ValueError("记忆 ID 超出 SQLite 整数范围")

        # SQLite can recycle an integer ID. Bind this plan to the document
        # UUID before queuing it, then recheck each batch under the write guard.
        document_ids = await self._deletion_document_ids(ids)
        kind = "delete" if mode == "recoverable" else "permanent_delete"
        payload = {
            "mode": mode, "memory_ids": ids, "document_ids": document_ids,
            "reason": str(reason)[:500],
        }
        creating = asyncio.create_task(self._create(kind, actor, payload, "preparing"))
        operation_id = None
        try:
            try:
                operation_id = await asyncio.shield(creating)
            except asyncio.CancelledError:
                operation_id = await settle_memory_mutation_task(creating)
                raise
            await self.store.reserve_deletion_items(operation_id, ids)
            self._assert_jobs_open()
            await self.store.update(operation_id, status="queued")
            self._launch_job(operation_id, runner=self._run_deletion_job)
        except BaseException:
            if operation_id is not None:
                await settle_memory_mutation_task(asyncio.create_task(self.store.remove(operation_id)))
            raise
        return self._deletion_progress(operation_id, mode, "queued", len(ids), {})

    async def deletion_status(self, operation_id: str, *, actor: str) -> dict:
        operation = await self._owned(operation_id, actor, allow_expired=True)
        payload = operation["payload"]
        mode = payload.get("mode")
        if (operation["kind"], mode) not in {("delete", "recoverable"), ("permanent_delete", "permanent")}:
            raise ValueError("删除任务不存在")
        return self._deletion_progress(
            operation_id, mode, operation["status"], len(payload["memory_ids"]),
            await self.store.deletion_item_states(operation_id),
            error=operation["result"].get("error"),
            expired=operation["expires_at"] <= time.time(),
        )

    async def _reconcile_interrupted_deletions(self) -> None:
        for operation in await self.store.interrupted_permanent_deletions():
            document_ids = operation["payload"].get("document_ids")
            if operation.get("corrupt_fields") or not isinstance(document_ids, dict):
                continue
            states = await self.store.deletion_item_states(operation["id"])
            in_flight = [
                memory_id for memory_id, state in states.items()
                if state == "deleting" and document_ids.get(str(memory_id))
            ]
            current = await self._deletion_document_ids(in_flight)
            removed = [
                memory_id for memory_id in in_flight
                if current.get(str(memory_id)) != document_ids[str(memory_id)]
            ]
            # Only reconcile the receipt. A replacement document under the
            # same integer ID never becomes eligible for this deletion.
            await self.store.mark_deletion_items(operation["id"], removed, "deleted")
            await self.store.mark_deletion_items(operation["id"], in_flight, "failed")

    @staticmethod
    def _deletion_progress(operation_id, mode, status, total, states, *, error=None, expired=False):
        deleted_count = sum(state in _DELETED_STATES for state in states.values())
        failed_ids = [memory_id for memory_id, state in states.items() if state == "failed"]
        return {
            "operation_id": operation_id, "status": status, "mode": mode, "total": total,
            "processed_count": deleted_count + len(failed_ids), "deleted_count": deleted_count,
            "failed_count": len(failed_ids), "failed_ids": failed_ids,
            "can_restore": (
                mode == "recoverable" and not expired and status in _RECOVERABLE_STATES
                and any(state in {"deleted", "restoring"} for state in states.values())
            ),
            **({"error": error} if error else {}),
        }

    async def _deletion_document_ids(self, memory_ids: list[int]) -> dict[str, str]:
        db = self.engine.db_connection
        if db is None:
            raise RuntimeError("记忆数据库未初始化")
        result = {}
        for start in range(0, len(memory_ids), PERMANENT_BATCH_SIZE):
            batch = memory_ids[start:start + PERMANENT_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            async with db.execute(
                f"SELECT id,doc_id FROM documents WHERE id IN ({placeholders})", batch,
            ) as cursor:
                result.update((str(row[0]), row[1]) for row in await cursor.fetchall() if row[1])
        return result

    async def _run_deletion_job(self, operation_id: str) -> None:
        try:
            if not hasattr(self, "_deletion_semaphore"):
                self._deletion_semaphore = asyncio.Semaphore(1)
            async with self._deletion_semaphore:
                self._assert_jobs_open()
                operation = await self.store.get(operation_id)
                if operation["status"] != "queued":
                    return
                await self.store.update(operation_id, status="running")
                plan = operation["payload"]
                memory_ids = plan["memory_ids"]
                recoverable = plan["mode"] == "recoverable"
                chunk_size = RECOVERABLE_BATCH_SIZE if recoverable else PERMANENT_BATCH_SIZE
                for start in range(0, len(memory_ids), chunk_size):
                    self._assert_jobs_open()
                    # Permanent removal briefly excludes adds while the vector
                    # adapter and primary store delete the same integer IDs.
                    async with memory_import_guard(self.engine, exclusive=not recoverable), memory_mutation_guard(self.engine):
                        self._assert_jobs_open()
                        task = memory_mutation_task(
                            self.engine,
                            self._delete_job_chunk(operation_id, plan, memory_ids[start:start + chunk_size]),
                            track=False,
                        )
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            await settle_memory_mutation_task(task)
                            raise
                await self._finish_deletion(operation_id)
        except asyncio.CancelledError:
            await settle_memory_mutation_task(asyncio.create_task(
                self.store.update(operation_id, status="interrupted", expected_status=("queued", "running")),
            ))
            raise
        except Exception:
            logger.exception("删除任务 %s 未完整执行", operation_id)
            await self._finish_deletion(operation_id, error="删除任务未完整执行，请查看插件日志")

    async def _finish_deletion(self, operation_id: str, *, error: str | None = None) -> None:
        states = await self.store.deletion_item_states(operation_id)
        deleted = sum(state in _DELETED_STATES for state in states.values())
        failed = sum(state == "failed" for state in states.values())
        status = ("partial" if deleted else "failed") if error or failed else "completed"
        result = {"deleted_count": deleted, "failed_count": failed}
        if error:
            result["error"] = error
        try:
            await self.store.update(operation_id, status=status, result=result)
        except AdministrationCapacityError:
            # The per-memory receipts already account for every committed change.
            await self.store.update(operation_id, status=status)

    async def _delete_job_chunk(self, operation_id, plan, memory_ids):
        current = await self._deletion_document_ids(memory_ids)
        matched = [
            memory_id for memory_id in memory_ids
            if str(memory_id) in current and current[str(memory_id)] == plan["document_ids"].get(str(memory_id))
        ]
        try:
            if plan["mode"] == "recoverable":
                self.require("recovery")
                await self._archive_delete_chunk(operation_id, matched)
            else:
                await self._permanent_delete_chunk(operation_id, matched)
        finally:
            # Entries already marked deleted by the batch are left intact.
            await self.store.mark_deletion_items(operation_id, memory_ids, "failed")

    async def _archive_delete_chunk(self, operation_id, memory_ids):
        from .memory_administration import fingerprint, valid_snapshot

        snapshots = {}
        for memory_id in memory_ids:
            try:
                record = await self.snapshot(memory_id)
            except (TypeError, ValueError):
                continue
            if valid_snapshot(record) and (record["metadata"].get("status") or "active") == "active":
                snapshots[memory_id] = record
        if not snapshots:
            return
        reserved = await self.store.prepare_deletion_snapshots(operation_id, snapshots)
        if not reserved:
            return

        async def record_recovery(connection, ids):
            placeholders = ",".join("?" for _ in ids)
            async with connection.execute(
                f"SELECT d.id,d.text,d.metadata,s.source_json FROM documents d "
                f"LEFT JOIN memory_sources s ON s.memory_id=d.id WHERE d.id IN ({placeholders})", ids,
            ) as cursor:
                rows = await cursor.fetchall()
            fingerprints = {
                row[0]: fingerprint({
                    "text": row[1], "metadata": json.loads(row[2]),
                    "source_messages": json.loads(row[3]) if row[3] else [],
                }) for row in rows
            }
            if len(fingerprints) != len(ids):
                raise RuntimeError("归档记录已变化")
            await self.store.seal_deletion_snapshots(connection, operation_id, fingerprints)

        await self.engine.archive_memories(
            reserved, recovery_operation_id=operation_id, record_recovery=record_recovery,
        )

    async def _permanent_delete_chunk(self, operation_id, memory_ids):
        if not memory_ids:
            return
        await self.store.mark_deletion_items(operation_id, memory_ids, "deleting")
        try:
            await self.engine.batch_delete_memories(memory_ids)
        finally:
            # A derived index may fail after the primary deletion commits.
            # Keep those exact successes visible even when repair is pending.
            remaining = await self._deletion_document_ids(memory_ids)
            removed = [memory_id for memory_id in memory_ids if str(memory_id) not in remaining]
            await self.store.mark_deletion_items(operation_id, removed, "deleted")
            profile_manager = getattr(self.engine, "user_profile_manager", None)
            if profile_manager is not None and removed:
                await profile_manager.delete_by_source_memories(removed)
