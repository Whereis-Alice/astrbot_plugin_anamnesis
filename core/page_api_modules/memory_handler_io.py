"""
MemoryHandler 的 MemoryHandlerIoMixin 拆分模块
自动从 core/page_api_modules/memory_handler.py 拆分，保持行为不变
"""

import asyncio
import math
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from astrbot.api import logger
from quart import request

from ..managers.memory_mutation import (
    memory_import_guard,
    memory_mutation_guard,
    memory_mutation_task,
    settle_memory_mutation_task,
)
from ..memory_source import restore_source_messages
from ..memory_transfer import (
    MAX_IMPORT_ENTRIES,
    MAX_TRANSFER_CONTENT_BYTES,
    isolate_import_provenance,
    memory_import_key,
    parse_transfer_content,
    serialize_transfer_csv,
    serialize_transfer_json,
)


class MemoryHandlerIoMixin:
    """MemoryHandler 拆分模块：MemoryHandlerIoMixin"""

    async def export_memories(self, memory_engine) -> dict[str, Any]:
        """Export all or selected memories as portable JSON/CSV content."""
        payload = await request.get_json(silent=True)
        if payload is not None and not isinstance(payload, dict):
            return self.utils.error("请求参数必须是 JSON 对象")
        payload = payload or {}
        export_format = str(payload.get("format", "json")).strip().lower()
        if export_format not in {"json", "csv"}:
            return self.utils.error("导出格式仅支持 JSON 或 CSV")

        raw_ids = payload.get("memory_ids")
        memory_ids: list[int] | None = None
        if raw_ids is not None:
            if not isinstance(raw_ids, list):
                return self.utils.error("memory_ids 必须是整数列表")
            if len(raw_ids) > MAX_IMPORT_ENTRIES:
                return self.utils.error(f"单次最多导出 {MAX_IMPORT_ENTRIES} 条记忆")
            try:
                memory_ids = []
                for item in raw_ids:
                    if isinstance(item, bool) or not isinstance(item, (int, str)):
                        raise TypeError("记忆 ID 必须是正整数")
                    memory_id = int(item)
                    if not 0 < memory_id <= 2**63 - 1:
                        raise ValueError("记忆 ID 超出 SQLite 正整数范围")
                    memory_ids.append(memory_id)
            except (TypeError, ValueError):
                return self.utils.error("memory_ids 必须是 SQLite 范围内的正整数列表")

        records = await memory_engine.get_memory_transfer_records(memory_ids)
        if len(records) > MAX_IMPORT_ENTRIES:
            return self.utils.error(
                f"单次最多导出 {MAX_IMPORT_ENTRIES} 条记忆，请选择部分记忆"
            )
        exported_at = datetime.now(timezone.utc).isoformat()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # 序列化可能涉及上万条记录（含完整 source_messages），卸载到线程避免阻塞事件循环
        if export_format == "csv":
            content = await asyncio.to_thread(serialize_transfer_csv, records)
            mime_type = "text/csv;charset=utf-8"
        else:
            content = await asyncio.to_thread(
                serialize_transfer_json, records, exported_at
            )
            mime_type = "application/json;charset=utf-8"

        return self.utils.ok(
            {
                "filename": f"anamnesis-export-{stamp}.{export_format}",
                "mime_type": mime_type,
                "content": content,
                "memory_count": len(records),
            }
        )

    async def import_memories(self, memory_engine, memory_processor) -> dict[str, Any]:
        """Preview or import native and common external JSON/CSV memories."""
        payload = await request.get_json(silent=True)
        if payload is not None and not isinstance(payload, dict):
            return self.utils.error("请求参数必须是 JSON 对象")
        payload = payload or {}
        import_format = str(payload.get("format", "json")).strip().lower()
        content = payload.get("content")
        dry_run = bool(payload.get("dry_run", True))
        duplicate_strategy = (
            str(payload.get("duplicate_strategy", "skip")).strip().lower()
        )
        if duplicate_strategy not in {"skip", "allow"}:
            return self.utils.error("duplicate_strategy 仅支持 skip 或 allow")
        if not isinstance(content, str) or not content.strip():
            return self.utils.error("导入文件内容不能为空")
        if len(content.encode("utf-8")) > MAX_TRANSFER_CONTENT_BYTES:
            return self.utils.error("导入文件不能超过 50 MiB")

        try:
            # 最多 50 MiB 内容的解析，卸载到线程避免阻塞事件循环
            entries, parse_errors = await asyncio.to_thread(
                parse_transfer_content, content, import_format
            )
        except ValueError as exc:
            return self.utils.error(str(exc))

        if not dry_run:
            if not hasattr(memory_engine, "_memory_import_lock"):
                memory_engine._memory_import_lock = asyncio.Lock()
            async with memory_engine._memory_import_lock:
                return await self._import_entries(
                    memory_engine,
                    memory_processor,
                    entries,
                    parse_errors,
                    duplicate_strategy,
                )

        existing_keys = await memory_engine.get_memory_import_keys()
        detected_keys = set(existing_keys)
        duplicate_count = 0
        for entry in entries:
            if not entry.content:
                continue
            key = memory_import_key(entry.content, entry.session_id, entry.persona_id)
            if key in detected_keys:
                duplicate_count += 1
            detected_keys.add(key)

        summary_required = sum(entry.requires_summary for entry in entries)
        if dry_run:
            planned_count = len(entries)
            if duplicate_strategy == "skip":
                planned_count -= duplicate_count
            return self.utils.ok(
                {
                    "dry_run": True,
                    "total": len(entries) + len(parse_errors),
                    "valid_count": len(entries),
                    "invalid_count": len(parse_errors),
                    "duplicate_count": duplicate_count,
                    "summary_required_count": summary_required,
                    "planned_import_count": max(0, planned_count),
                    "errors": parse_errors[:50],
                }
            )

    async def _prepare_import_entry(self, memory_processor, entry):
        """Resolve an imported conversation to a stable draft before writing."""
        if not entry.requires_summary:
            return entry
        if memory_processor is None:
            raise RuntimeError("记忆处理器未初始化，无法总结仅含原始对话的导入项")
        messages = restore_source_messages(entry.source_messages)
        content, generated_metadata, importance = await memory_processor.process_conversation(
            messages=messages, is_group_chat=bool(messages[0].group_id), persona_id=entry.persona_id,
        )
        if not isinstance(content, str) or not content.strip():
            raise ValueError("导入总结必须是非空文本")
        if generated_metadata is not None and not isinstance(generated_metadata, dict):
            raise ValueError("导入总结的元数据必须是对象")
        metadata = {**entry.metadata, **(generated_metadata or {})}
        metadata.update(session_id=entry.session_id, persona_id=entry.persona_id,
                        status=entry.metadata.get("status", "active"))
        return replace(entry, content=content.strip(), metadata=isolate_import_provenance(metadata),
                       importance=self._normalize_importance_update(importance, "stored"))

    async def _import_entries(
        self, memory_engine, memory_processor, entries, parse_errors, duplicate_strategy,
        operation_receipt=None,
    ):
        imported_ids: list[int] = []
        skipped_duplicates = 0
        import_errors = list(parse_errors)
        for entry in entries:
            try:
                entry = await self._prepare_import_entry(memory_processor, entry)
                content_text = entry.content
                metadata = self._import_metadata(entry, operation_receipt)
                importance = metadata["importance"]
                atoms = None
                classify_atoms = getattr(
                    memory_processor, "classify_atoms_from_metadata", None
                )
                if entry.atoms is None and callable(classify_atoms):
                    atoms = classify_atoms(
                        metadata=metadata,
                        parent_importance=importance,
                        session_id=entry.session_id,
                        persona_id=entry.persona_id,
                    )
                memory_id = await self._finish_import_write(
                    memory_engine,
                    self._write_import_entry(
                        memory_engine, entry, content_text, importance, metadata, atoms,
                        skip_duplicates=duplicate_strategy == "skip",
                    ),
                )
                if memory_id is None:
                    skipped_duplicates += 1
                    continue
                imported_ids.append(memory_id)
            except Exception as exc:
                logger.error(
                    f"[PageAPI] 导入记忆失败 (index={entry.source_index}): {exc}",
                    exc_info=True,
                )
                import_errors.append({"index": entry.source_index, "error": str(exc)})

        return self.utils.ok(
            {
                "dry_run": False,
                "total": len(entries) + len(parse_errors),
                "imported_count": len(imported_ids),
                "skipped_duplicate_count": skipped_duplicates,
                "failed_count": len(import_errors),
                "imported_ids": imported_ids,
                "errors": import_errors[:50],
            }
        )

    def _import_metadata(self, entry, operation_receipt=None, *, imported_at=None):
        metadata = isolate_import_provenance(dict(entry.metadata))
        metadata.update(
            memory_origin="memory_import", imported_at=time.time() if imported_at is None else imported_at,
            import_source_index=entry.source_index,
            importance=self._normalize_importance_update(entry.importance, "stored"),
            session_id=entry.session_id, persona_id=entry.persona_id,
            status=entry.metadata.get("status", "active"),
        )
        if operation_receipt is not None:
            # Only the local job worker may bind a receipt; foreign fields
            # have already been moved into import provenance.
            metadata["import_operation_id"] = operation_receipt["operation_id"]
            metadata["import_item_key"] = operation_receipt["item_key"]
        return metadata

    async def _resume_import_entry_locked(self, engine, processor, entry, current, receipt):
        """Resume an unchanged stage owned by the persisted local item."""
        memory_id, actual = current["id"], dict(current["metadata"])
        expected = self._import_metadata(entry, receipt, imported_at=actual.get("imported_at"))
        expected["status"] = "importing"
        for field in ("create_time", "last_access_time", "imported_at"):
            value = actual.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"导入暂存记忆 {memory_id} 的时间字段不符，请人工核验")
            expected[field] = value
        if "create_time" in entry.metadata:
            try:
                expected["create_time"] = float(entry.metadata["create_time"])
            except (TypeError, ValueError):
                pass
        if entry.source_messages:
            expected.update(has_source=True, source_message_count=len(entry.source_messages))
        if entry.atoms:
            expected["atom_types"] = sorted({atom["atom_type"] for atom in entry.atoms})
        elif entry.atoms is None:
            classify = getattr(processor, "classify_atoms_from_metadata", None)
            atoms = classify(metadata=expected, parent_importance=expected["importance"],
                             session_id=entry.session_id, persona_id=entry.persona_id) if callable(classify) else []
            if atoms:
                expected["atom_types"] = sorted({atom.atom_type.value for atom in atoms})
        # Access accounting may legitimately change without editing a stage.
        actual.pop("access_count", None)
        expected.pop("access_count", None)
        if (
            actual != expected or current["text"] != entry.content
            or current["source_messages"] != entry.source_messages
        ):
            raise ValueError(f"导入暂存记忆 {memory_id} 已变化，保留记录等待人工核验")
        if await engine.has_memory_import_key(
            memory_import_key(entry.content, entry.session_id, entry.persona_id),
            exclude_memory_id=memory_id,
        ):
            raise ValueError(f"导入暂存记忆 {memory_id} 已有其他已完成副本，请核验后清理")
        # A retry never deletes an uncertain copy. Commit from the same saved
        # entry and retain its ID if storage fails again or its caller cancels.
        expected["status"] = entry.metadata.get("status", "active")
        return await self._commit_import_entry_locked(engine, memory_id, entry, entry.content, expected)

    async def _write_import_entry(
        self, engine, entry, content, importance, metadata, atoms, *, skip_duplicates=False
    ):
        async with memory_import_guard(engine, exclusive=skip_duplicates), memory_mutation_guard(engine):
            # Summarization and job preparation may await other mutations. The
            # preview's keys are never authoritative for the actual write.
            if skip_duplicates and await engine.has_memory_import_key(
                memory_import_key(content, entry.session_id, entry.persona_id)
            ):
                return None
            return await self._write_import_entry_locked(
                engine,
                entry,
                content,
                importance,
                metadata,
                atoms,
            )

    async def _write_import_entry_locked(
        self, engine, entry, content, importance, metadata, atoms
    ):
        memory_id = None
        original_status = metadata.get("status", "active")
        metadata = {**metadata, "status": "importing"}
        if entry.atoms:
            metadata["atom_types"] = sorted({row["atom_type"] for row in entry.atoms})
        add_task = memory_mutation_task(
            engine,
            engine.add_memory(
                content=content,
                session_id=entry.session_id,
                persona_id=entry.persona_id,
                importance=importance,
                metadata=metadata,
                atoms=atoms,
                preserve_create_time="create_time" in metadata,
                source_messages=entry.source_messages or None,
            ),
            track=False,
        )
        try:
            memory_id = await asyncio.shield(add_task)
            if (
                not isinstance(memory_id, int)
                or isinstance(memory_id, bool)
                or memory_id <= 0
            ):
                memory_id = None
                raise RuntimeError("导入写入未返回有效记忆 ID")
            return await self._commit_import_entry_locked(
                engine, memory_id, entry, content, {**metadata, "status": original_status}
            )
        except BaseException:
            if memory_id is None:
                try:
                    candidate_id = await settle_memory_mutation_task(add_task)
                    if (
                        isinstance(candidate_id, int)
                        and not isinstance(candidate_id, bool)
                        and candidate_id > 0
                    ):
                        memory_id = candidate_id
                except BaseException:
                    memory_id = None
            if memory_id is not None:
                try:
                    if not await engine.delete_memory(memory_id):
                        logger.error(
                            "导入回滚未删除暂存记忆 %s，请管理员检查", memory_id
                        )
                except Exception:
                    logger.error(
                        "导入回滚失败，暂存记忆 ID=%s", memory_id, exc_info=True
                    )
            raise

    async def _commit_import_entry_locked(self, engine, memory_id, entry, content, metadata):
        connection = getattr(engine, "db_connection", None)
        if connection is not None:
            async with connection.execute(
                "SELECT 1 FROM memory_write_ops WHERE memory_id=? "
                "AND status IN ('pending','needs_repair') LIMIT 1", (memory_id,),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    raise RuntimeError("导入记忆尚未完整写入，请修复索引后重试")
        if entry.atoms is not None:
            restored = await engine.restore_archived_atom_lifecycle(
                memory_id, entry.atoms, session_id=entry.session_id, persona_id=entry.persona_id,
            )
            if restored != len(entry.atoms):
                raise RuntimeError("导入原子生命周期未完整保存")
        commit_metadata = await self._prepare_import_commit(
            engine, memory_id, content, metadata, metadata.get("status", "active"),
        )
        if not await engine.update_memory(memory_id, {"metadata": commit_metadata}):
            raise RuntimeError("导入记忆状态未完成提交")
        return memory_id

    async def _prepare_import_commit(
        self, engine, memory_id, content, metadata, status
    ):
        """Finish lifecycle-sensitive indexes while the parent remains invisible."""
        commit_metadata = {"status": status}
        if status != "active":
            if status == "archived":
                snapshot = metadata.get("archived_atom_lifecycle")
                if not isinstance(snapshot, list):
                    snapshot = await engine.snapshot_archived_atom_lifecycle(memory_id)
                commit_metadata["archived_atom_lifecycle"] = snapshot
                commit_metadata["archived_at"] = metadata.get(
                    "archived_at", time.time()
                )
            await engine.db_connection.execute(
                "DELETE FROM livingmemory_memories_fts WHERE doc_id=?",
                (memory_id,),
            )
            await engine.db_connection.commit()
            embedding_delete = getattr(
                getattr(engine.faiss_db, "embedding_storage", None),
                "delete",
                None,
            )
            if callable(embedding_delete):
                await embedding_delete([memory_id])
            else:
                await engine._set_index_rebuild_required(True)
            await engine._delete_graph_and_atoms_for_batch([memory_id])
            return commit_metadata

        graph_manager = getattr(engine, "graph_memory_manager", None)
        if graph_manager is not None:
            atom_store = getattr(engine, "atom_store", None)
            atoms = await atom_store.get_by_parent(memory_id) if atom_store else []
            now = time.time()
            live_atoms = [
                atom
                for atom in atoms
                if atom.status.value == "active"
                and math.isfinite(atom.expires_at)
                and atom.expires_at > now
            ]
            if (atoms or metadata.get("atom_types")) and not live_atoms:
                await graph_manager.delete_memory(memory_id)
            else:
                await graph_manager.index_memory(
                    memory_id,
                    content,
                    {**metadata, "status": "active"},
                    live_atoms,
                )
        return commit_metadata

    @staticmethod
    async def _finish_import_write(engine, operation):
        task = memory_mutation_task(engine, operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise
