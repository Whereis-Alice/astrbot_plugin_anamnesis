"""Explicit lifecycle operations and conservative write-time deduplication."""

import asyncio
import json
import re
import time
from difflib import SequenceMatcher

import aiosqlite

from ...storage.sqlite_utils import configure_sqlite_connection, sqlite_connect_kwargs


class MemoryEngineOperationsMixin:
    async def _set_index_rebuild_required(self, required):
        self.index_rebuild_required = bool(required)
        if self.db_connection is not None:
            await self.db_connection.execute(
                "INSERT OR REPLACE INTO migration_status(key,value,updated_at) "
                "VALUES ('document_index_rebuild_required', ?, datetime('now'))",
                ("true" if required else "false",),
            )
            await self.db_connection.commit()

    async def delete_reverted_summaries(self, session_id, cutoff, revision, removed_message_ids=None):
        if self.db_connection is None:
            return []
        removed = set(removed_message_ids or [])
        async with self.db_connection.execute(
            "SELECT id,metadata FROM documents WHERE json_valid(metadata) "
            "AND json_extract(metadata,'$.source_session_id')=?",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        ids = []
        for row in rows:
            metadata = json.loads(row[1])
            source_ids = metadata.get("source_message_ids")
            if removed_message_ids is not None and isinstance(source_ids, list):
                invalid = bool(removed.intersection(value for value in source_ids if isinstance(value, int)))
            else:
                window = metadata.get("source_window") or {}
                end = window.get("end_index", 0) if isinstance(window, dict) else 0
                invalid = (metadata.get("source_revision") == revision
                           and isinstance(end, (int, float)) and end > cutoff)
            if invalid:
                ids.append(row[0])
        deleted = []
        for memory_id in ids:
            if await self.delete_memory(memory_id):
                deleted.append(memory_id)
        return deleted

    @staticmethod
    def _dedup_facts(metadata):
        facts = metadata.get("key_facts")
        if not isinstance(facts, list) or not facts:
            return None
        return sorted(re.sub(r"\s+", " ", str(fact)).strip().casefold() for fact in facts)

    @staticmethod
    def _dedup_same_source_window(old_metadata, new_metadata):
        """Keep independently revertible summary windows in separate records."""
        if "source_message_ids" in old_metadata or "source_message_ids" in new_metadata:
            old_ids = old_metadata.get("source_message_ids")
            new_ids = new_metadata.get("source_message_ids")
            if any(
                not isinstance(ids, list) or not ids or any(
                    not isinstance(value, int) or isinstance(value, bool) or value <= 0
                    for value in ids
                )
                for ids in (old_ids, new_ids)
            ):
                return False
            return set(old_ids) == set(new_ids)
        if "source_revision" in old_metadata or "source_revision" in new_metadata:
            old_window = old_metadata.get("source_window")
            new_window = new_metadata.get("source_window")
            for window in (old_window, new_window):
                if not isinstance(window, dict):
                    return False
                start, end = window.get("start_index"), window.get("end_index")
                if not (
                    isinstance(start, int) and not isinstance(start, bool)
                    and isinstance(end, int) and not isinstance(end, bool)
                    and 0 <= start < end
                ):
                    return False
            return old_window == new_window
        return True

    async def add_memory_conservatively(self, *, threshold=0.97, candidate_count=10, **kwargs):
        """Only merge equivalent facts in the same scope, persona and source chat.

        Opposite preferences, changed numbers and new facts are always new records.
        A merge retains the existing ID/content and adds the new source provenance.
        """
        if not hasattr(self, "_dedup_write_lock"):
            self._dedup_write_lock = asyncio.Lock()
        async with self._dedup_write_lock:
            metadata = kwargs.get("metadata") or {}
            facts = self._dedup_facts(metadata)
            if facts is not None and metadata.get("source_session_id"):
                candidates = await self.search_memories(
                    kwargs["content"], k=candidate_count, session_id=kwargs.get("session_id"),
                    persona_id=kwargs.get("persona_id"),
                )
                for candidate in candidates:
                    old = await self.get_memory(candidate.doc_id)
                    old_meta = old.get("metadata", {}) if old else {}
                    if not isinstance(old_meta, dict) or old_meta.get("status", "active") != "active":
                        continue
                    if any(old_meta.get(key) != metadata.get(key) for key in ("source_session_id", "source_revision")):
                        continue
                    if not self._dedup_same_source_window(old_meta, metadata):
                        continue
                    if old_meta.get("session_id") != kwargs.get("session_id") or old_meta.get("persona_id") != kwargs.get("persona_id"):
                        continue
                    if facts != self._dedup_facts(old_meta):
                        continue
                    lifecycle = getattr(self, "get_memory_atom_lifecycle", None)
                    if callable(lifecycle):
                        state = await lifecycle(candidate.doc_id) or {}
                        if state.get("hidden", 0) or (old_meta.get("atom_types") and not state.get("total", 0)):
                            continue  # A newly restated expired fact needs new atoms.
                    old_owners = old_meta.get("participant_identities") or old_meta.get("participants") or []
                    new_owners = metadata.get("participant_identities") or metadata.get("participants") or []
                    if old_owners != new_owners:
                        continue
                    old_text, new_text = str(old.get("text", "")), str(kwargs["content"])
                    # Keep negation and numeric evidence exact even when an extractor
                    # omits those details from key_facts.
                    sensitive = r"\d+(?:\.\d+)?|不|没|无|未|别|not\b|never\b|no\b|without\b"
                    if re.findall(sensitive, old_text.casefold()) != re.findall(sensitive, new_text.casefold()):
                        continue
                    if SequenceMatcher(None, old_text.casefold(), new_text.casefold(), autojunk=False).ratio() < threshold:
                        continue
                    old_source = await self.get_memory_source(candidate.doc_id)
                    windows = list(old_meta.get("source_windows") or [])
                    for window in (old_meta.get("source_window"), metadata.get("source_window")):
                        if isinstance(window, dict) and window not in windows:
                            windows.append(window)
                    patch = {"source_windows": windows[-50:], "dedup_count": int(old_meta.get("dedup_count", 0)) + 1,
                             "last_reinforced_at": time.time(),
                             "importance": max(float(old_meta.get("importance", .5)), float(kwargs.get("importance", .5)))}
                    source_ids = list(old_meta.get("source_message_ids") or [])
                    for source_id in metadata.get("source_message_ids") or []:
                        if source_id not in source_ids:
                            source_ids.append(source_id)
                    if source_ids:
                        patch["source_message_ids"] = source_ids
                    source = kwargs.get("source_messages") or []
                    combined = list(old_source)
                    for item in source:
                        if item not in combined:
                            combined.append(item)
                    if combined:
                        patch.update(has_source=True, source_message_count=len(combined))
                    previous = {"metadata": old_meta, "source_messages": old_source,
                                "metadata_patch": patch, "written_source_messages": combined}
                    if not await self.update_memory(candidate.doc_id, {"metadata": patch}):
                        break
                    try:
                        if source:
                            await self.save_memory_source(candidate.doc_id, combined)
                    except BaseException:
                        await self._restore_summary_snapshot(candidate.doc_id, previous)
                        raise
                    return candidate.doc_id, previous
            return await self.add_memory(**kwargs), None

    async def undo_summary_write(self, memory_id, previous=None):
        if previous is None:
            await self.delete_memory(memory_id)
            manager = getattr(self, "user_profile_manager", None)
            if manager:
                await manager.delete_by_source_memory(memory_id)
        else:
            await self._restore_summary_snapshot(memory_id, previous)

    async def _restore_summary_snapshot(self, memory_id, previous):
        """Undo only this merge's fields, preserving later independent writes."""
        async def restore():
            async with aiosqlite.connect(self.db_path, **sqlite_connect_kwargs(self.config)) as connection:
                await configure_sqlite_connection(connection, self.config)
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    row = await (await connection.execute(
                        "SELECT metadata FROM documents WHERE id=?", (memory_id,),
                    )).fetchone()
                    if row is None:
                        await connection.rollback()
                        return
                    current = self._safe_json_dict(row[0])
                    source_row = await (await connection.execute(
                        "SELECT source_json FROM memory_sources WHERE memory_id=?", (memory_id,),
                    )).fetchone()
                    current_source = json.loads(source_row[0]) if source_row else []
                    old_source = previous["source_messages"]
                    source_unchanged = current_source in (old_source, previous.get("written_source_messages", old_source))
                    # Only fields owned by the merge are eligible for reversal.
                    # A later value for the same field also takes precedence.
                    for key, expected in previous.get("metadata_patch", {}).items():
                        if key in ("has_source", "source_message_count") and not source_unchanged:
                            continue
                        if current.get(key) != expected:
                            continue
                        if key in previous["metadata"]:
                            current[key] = previous["metadata"][key]
                        else:
                            current.pop(key, None)
                    await connection.execute("UPDATE documents SET metadata=? WHERE id=?",
                                             (json.dumps(current, ensure_ascii=False), memory_id))
                    if source_unchanged:
                        await connection.execute("UPDATE memory_sources SET source_json=? WHERE memory_id=?",
                                                 (json.dumps(old_source, ensure_ascii=False), memory_id))
                    await connection.commit()
                except BaseException:
                    await connection.rollback()
                    raise

        task = asyncio.create_task(restore())
        try:
            await asyncio.shield(task)
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
        self._invalidate_search_cache()
        graph_manager = getattr(self, "graph_memory_manager", None)
        if graph_manager is not None:
            memory = await self.get_memory(memory_id)
            metadata = self._safe_json_dict(memory.get("metadata")) if memory else {}
            if not memory or metadata.get("status", "active") != "active":
                return
            atom_store = getattr(self, "atom_store", None)
            atoms = await atom_store.get_by_parent(memory_id) if atom_store is not None else None
            await graph_manager.index_memory(memory_id, memory["text"], metadata, atoms)

    async def record_forget_audit(self, *, memory_id, scope, mode, reason, success):
        if self.db_connection is None:
            return
        await self.db_connection.execute(
            "CREATE TABLE IF NOT EXISTS memory_forget_audit "
            "(id INTEGER PRIMARY KEY, memory_id INTEGER, scope TEXT, mode TEXT, "
            "reason TEXT, success INTEGER, created_at REAL)"
        )
        await self.db_connection.execute(
            "INSERT INTO memory_forget_audit(memory_id,scope,mode,reason,success,created_at) "
            "VALUES (?,?,?,?,?,?)", (memory_id, scope, mode, reason[:300], int(success), time.time()),
        )
        await self.db_connection.execute(
            "DELETE FROM memory_forget_audit WHERE id NOT IN "
            "(SELECT id FROM memory_forget_audit ORDER BY id DESC LIMIT 10000)"
        )
        await self.db_connection.commit()
