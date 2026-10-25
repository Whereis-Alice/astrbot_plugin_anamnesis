"""Webchat history reconciliation and revision guards for asynchronous summaries."""

import asyncio
import json
from typing import Any

from ..utils.history_alignment import compute_revert_cutoff


class ConversationHistoryMixin:
    def _history_lock(self, session_id: str) -> asyncio.Lock:
        if not hasattr(self, "_history_locks"):
            self._history_locks = {}
        return self._history_locks.setdefault(session_id, asyncio.Lock())

    async def get_session_revision(self, session_id: str) -> int:
        return int(await self.get_session_metadata(session_id, "history_revision", 0) or 0)

    async def bump_session_revision(self, session_id: str) -> int:
        async with self._history_lock(session_id):
            revision = await self.get_session_revision(session_id) + 1
            await self._update_metadata_batch_unlocked(session_id, {"history_revision": revision}, revision - 1)
            return revision

    async def update_session_metadata_if_revision(self, session_id: str, key: str, value: Any, expected_revision: int) -> bool:
        return await self.update_session_metadata_batch_if_revision(
            session_id, {key: value}, expected_revision)

    async def update_session_metadata_batch_if_revision(self, session_id: str, updates: dict[str, Any], expected_revision: int) -> bool:
        """Atomically commit progress and pending state for the same history version."""
        async with self._history_lock(session_id):
            return await self._update_metadata_batch_unlocked(session_id, updates, expected_revision)

    async def _update_metadata_batch_unlocked(self, session_id, updates, expected_revision):
        connection = self.store.connection
        if connection is None or not updates:
            return False
        arguments = []
        expressions = []
        for key, value in updates.items():
            expressions.append("?,json(?)")
            arguments.extend(("$." + json.dumps(key), json.dumps(value, ensure_ascii=False)))
        arguments.append(session_id)
        revision_clause = ""
        if expected_revision is not None:
            revision_clause = " AND COALESCE(CAST(json_extract(metadata,'$.history_revision') AS INTEGER),0)=?"
            arguments.append(expected_revision)
        async with self.store._write_lock:
            try:
                cursor = await connection.execute(
                    "UPDATE sessions SET metadata=json_set(COALESCE(metadata,'{}')," +
                    ",".join(expressions) + ") WHERE session_id=?" + revision_clause,
                    arguments,
                )
                changed = cursor.rowcount == 1
                await cursor.close()
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        if changed:
            await self.invalidate_cache(session_id)
        return changed

    async def _clear_session_history(self, session_id):
        """Clear messages and advance revision in a single transaction."""
        async with self._history_lock(session_id):
            connection = self.store.connection
            if connection is None:
                return
            async with self.store._write_lock:
                try:
                    async with connection.execute(
                        "SELECT COALESCE(CAST(json_extract(metadata,'$.history_revision') AS INTEGER),0) "
                        "FROM sessions WHERE session_id=?", (session_id,)
                    ) as cursor:
                        row = await cursor.fetchone()
                    revision = int(row[0]) + 1 if row else 1
                    await connection.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
                    await connection.execute(
                        "UPDATE sessions SET message_count=0,metadata=? WHERE session_id=?",
                        (json.dumps({"history_revision": revision}), session_id),
                    )
                    await connection.commit()
                except BaseException:
                    await connection.rollback()
                    raise
            await self.invalidate_cache(session_id)

    async def align_webchat_history(self, event: Any, contexts: list, conversation_id: str | None = None) -> dict[str, Any] | None:
        platform = self._call_event_getter(event, "get_platform_name")
        checkpoint = None
        getter = getattr(event, "get_extra", None)
        if callable(getter):
            checkpoint = getter("llm_checkpoint_id")
        if str(platform).casefold() != "webchat" or not isinstance(checkpoint, str) or not checkpoint:
            return None
        session_id = event.unified_msg_origin
        if "GroupMessage" in session_id or not isinstance(contexts, list):
            return None
        checkpoints = []
        for context in contexts:
            if isinstance(context, dict) and context.get("role") == "_checkpoint":
                content = context.get("content")
                value = content.get("id") if isinstance(content, dict) else None
                if isinstance(value, str) and value:
                    checkpoints.append(value)
        async with self._history_lock(session_id):
            connection = self.store.connection
            if connection is None:
                return None
            async with self.store._write_lock:
                try:
                    async with connection.execute(
                        "SELECT id,role,metadata FROM messages WHERE session_id=? ORDER BY timestamp,id", (session_id,)
                    ) as cursor:
                        rows = [dict(row) for row in await cursor.fetchall()]
                    for row in rows:
                        try:
                            metadata = json.loads(row["metadata"] or "{}")
                        except (ValueError, TypeError):
                            return None  # Unknown legacy provenance cannot justify deletion.
                        if not isinstance(metadata, dict):
                            return None
                        row["metadata"] = metadata
                    latest_cid = (max(rows, key=lambda row: row["id"])["metadata"].get("anamnesis_conversation_id")
                                  if rows else None)
                    known_cid = conversation_id if isinstance(conversation_id, str) and conversation_id else None
                    changed_conversation = known_cid is not None and isinstance(latest_cid, str) and latest_cid != known_cid
                    ambiguous_empty_history = not checkpoints and (known_cid is None or latest_cid != known_cid)
                    if rows and (changed_conversation or ambiguous_empty_history):
                        # /new can retain the same UMO while changing AstrBot's
                        # conversation ID. An empty history without an ID is
                        # also insufficient evidence to delete durable memories.
                        previous_revision = await self.get_session_revision(session_id)
                        await connection.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
                        await connection.execute(
                            "UPDATE sessions SET message_count=0,metadata=json_set(metadata,"
                            "'$.history_revision',?,'$.last_summarized_index',0,'$.pending_summary',NULL) WHERE session_id=?",
                            (previous_revision+1,session_id),
                        )
                        await connection.commit()
                        await self.invalidate_cache(session_id)
                        return {"cutoff":0,"revision":previous_revision,"preserve_long_term":True}
                    cutoff = compute_revert_cutoff(rows, checkpoints)
                    if cutoff is None:
                        return None
                    previous_revision = await self.get_session_revision(session_id)
                    removed_ids = [row["id"] for row in rows[cutoff:]]
                    await connection.execute(
                        "DELETE FROM messages WHERE session_id=? AND id IN (" + ",".join("?" for _ in removed_ids) + ")",
                        (session_id, *removed_ids),
                    )
                    await connection.execute(
                        "UPDATE sessions SET message_count=?, metadata=json_set(metadata, "
                        "'$.history_revision', ?, '$.last_summarized_index', "
                        "MIN(COALESCE(json_extract(metadata,'$.last_summarized_index'),0),?), "
                        "'$.pending_summary', NULL) WHERE session_id=?",
                        (cutoff, previous_revision + 1, cutoff, session_id),
                    )
                    await connection.commit()
                except BaseException:
                    await connection.rollback()
                    raise
            await self.invalidate_cache(session_id)
            return {"cutoff": cutoff, "revision": previous_revision}

    async def response_checkpoint_valid(self, event: Any) -> bool:
        getter = getattr(event, "get_extra", None)
        checkpoint = getter("llm_checkpoint_id") if callable(getter) else None
        if not checkpoint or str(self._call_event_getter(event, "get_platform_name")).casefold() != "webchat":
            return True
        connection = self.store.connection
        if connection is None:
            return False
        async with connection.execute(
            "SELECT role,metadata FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (event.unified_msg_origin,),
        ) as cursor:
            latest = await cursor.fetchone()
        if latest is None:
            return await self.get_session_revision(event.unified_msg_origin) == 0
        try:
            metadata = json.loads(latest["metadata"] or "{}")
        except (ValueError, TypeError):
            return False
        if not isinstance(metadata, dict):
            return False
        stored = metadata.get("llm_checkpoint_id")
        current_cid = getter("anamnesis_conversation_id") if callable(getter) else None
        stored_cid = metadata.get("anamnesis_conversation_id")
        if isinstance(current_cid, str) and isinstance(stored_cid, str) and current_cid != stored_cid:
            return False
        return not stored or (latest["role"] == "user" and stored == checkpoint)
