"""Explicit lifecycle operations and conservative write-time deduplication."""

import asyncio
import json
import re
import time
from difflib import SequenceMatcher


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

    async def delete_reverted_summaries(self, session_id, cutoff, revision):
        if self.db_connection is None:
            return []
        async with self.db_connection.execute(
            "SELECT id FROM documents WHERE json_extract(metadata,'$.source_session_id')=? "
            "AND json_extract(metadata,'$.source_revision')=? "
            "AND json_extract(metadata,'$.source_window.end_index')>?",
            (session_id, revision, cutoff),
        ) as cursor:
            ids = [row[0] for row in await cursor.fetchall()]
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
                    source = kwargs.get("source_messages") or []
                    combined = list(old_source)
                    for item in source:
                        if item not in combined:
                            combined.append(item)
                    if combined:
                        patch.update(has_source=True, source_message_count=len(combined))
                    if not await self.update_memory(candidate.doc_id, {"metadata": patch}):
                        break
                    try:
                        if source:
                            await self.save_memory_source(candidate.doc_id, combined)
                    except BaseException:
                        await self._restore_summary_snapshot(candidate.doc_id, {"metadata": old_meta, "source_messages": old_source})
                        raise
                    return candidate.doc_id, {"metadata": old_meta, "source_messages": old_source}
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
        await self.db_connection.execute("UPDATE documents SET metadata=? WHERE id=?",
                                         (json.dumps(previous["metadata"], ensure_ascii=False), memory_id))
        await self.db_connection.execute(
            "UPDATE memory_sources SET source_json=? WHERE memory_id=?",
            (json.dumps(previous["source_messages"], ensure_ascii=False), memory_id))
        await self.db_connection.commit()
        graph_manager = getattr(self, "graph_memory_manager", None)
        if graph_manager is not None:
            memory = await self.get_memory(memory_id)
            atom_store = getattr(self, "atom_store", None)
            atoms = await atom_store.get_by_parent(memory_id) if atom_store is not None else None
            await graph_manager.index_memory(memory_id, memory["text"], previous["metadata"], atoms)
        self._invalidate_search_cache()

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
