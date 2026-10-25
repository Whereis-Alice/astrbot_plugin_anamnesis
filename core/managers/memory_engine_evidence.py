"""Shared live-fact policy for automatic recall and Agent evidence reads."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from dataclasses import is_dataclass, replace
from typing import Any

from astrbot.api import logger

from ..atom_evidence import atom_scope_matches_parent
from ..recall_persona import (
    is_null_persona_filter,
    persona_filter_requested,
    persona_matches,
)
from ..retrieval.hybrid_retriever import HybridResult


def safe_json_dict(value) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class MemoryEngineEvidenceMixin:
    async def merge_atom_recall_candidates(
        self, query: str, results: list, k: int,
        session_id: str | None = None, persona_id: str | None = None,
    ) -> list:
        """Bring independently matching atoms into the parent candidate pool."""
        retriever = getattr(self, "atom_retriever", None)
        if retriever is None or not getattr(self, "atom_enabled", True):
            return results
        try:
            atoms = await retriever.search(query, max(k*2, k), session_id, persona_id)
            existing = {int(result.doc_id) for result in results}
            scores = {}
            for atom in atoms:
                doc_id = int(atom.parent_memory_id)
                if doc_id not in existing and math.isfinite(float(atom.final_score)):
                    scores[doc_id] = max(scores.get(doc_id, 0.0), float(atom.final_score))
            if not scores:
                return results
            filters = {}
            if session_id is not None:
                filters["session_id"] = session_id
            if persona_filter_requested(persona_id):
                filters["persona_id"] = (
                    None if is_null_persona_filter(persona_id) else persona_id
                )
            storage_filters = {
                key: value
                for key, value in filters.items()
                if not (key == "persona_id" and value is None)
            }
            documents = await self.faiss_db.document_storage.get_documents(
                metadata_filters=storage_filters, ids=list(scores), limit=len(scores)
            )
            merged = list(results)
            for document in documents:
                metadata = safe_json_dict(document.get("metadata"))
                if str(metadata.get("status") or "active") != "active":
                    continue
                if any(
                    not persona_matches(metadata.get(key), value)
                    if key == "persona_id"
                    else metadata.get(key) != value
                    for key, value in filters.items()
                ):
                    continue
                doc_id = int(document["id"])
                if doc_id not in scores:
                    continue
                merged.append(HybridResult(
                    doc_id=doc_id, final_score=scores[doc_id], rrf_score=0.0,
                    bm25_score=None, vector_score=None,
                    content=str(document.get("text") or ""), metadata=metadata,
                    score_breakdown={"atom_score": scores[doc_id]},
                ))
            merged = await self.filter_recall_lifecycle(merged)
            return sorted(merged, key=lambda result: result.final_score, reverse=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The independent atom route may fail while document evidence is
            # still available; never add an unchecked atom candidate on failure.
            logger.warning("[MemoryEngine] 原子候选检索失败，保留其它路线", exc_info=True)
            return results

    async def _recall_has_atom_table(self) -> bool:
        cached = getattr(self, "_recall_atom_table_present", None)
        if cached is not None:
            return cached
        if self.db_connection is None:
            # Lightweight legacy embedders may only supply document/vector
            # storage. Do not cache absence before a real initialization.
            return False
        async with self.db_connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_atoms'"
        ) as cursor:
            present = await cursor.fetchone() is not None
        atom_type_column = "NULL"
        if present:
            async with self.db_connection.execute("PRAGMA table_info(memory_atoms)") as cursor:
                if any(row[1] == "atom_type" for row in await cursor.fetchall()):
                    atom_type_column = "atom_type"
        self._recall_atom_type_column = atom_type_column
        self._recall_atom_table_present = present
        return present

    async def _recall_atom_rows(self, memory_ids: list[int]) -> dict[int, list]:
        if not memory_ids or not await self._recall_has_atom_table():
            return {}
        grouped: dict[int, list] = {}
        ids = list(dict.fromkeys(int(value) for value in memory_ids))
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            async with self.db_connection.execute(
                "SELECT id, parent_memory_id, content, status, expires_at, "
                "session_id, persona_id, "
                f"{getattr(self, '_recall_atom_type_column', 'NULL')} "
                "FROM memory_atoms WHERE parent_memory_id IN "
                f"({','.join('?' for _ in batch)}) ORDER BY id",
                batch,
            ) as cursor:
                for row in await cursor.fetchall():
                    grouped.setdefault(int(row[1]), []).append(row)
        return grouped

    async def _recall_document_rows(self, memory_ids: list[int]) -> dict[int, tuple]:
        """Read current parent state even when the recall route came from cache."""
        documents = {}
        ids = list(dict.fromkeys(int(value) for value in memory_ids))
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            async with self.db_connection.execute(
                "SELECT id, text, metadata FROM documents WHERE id IN "
                f"({','.join('?' for _ in batch)})",
                batch,
            ) as cursor:
                for row in await cursor.fetchall():
                    documents[int(row[0])] = (str(row[1] or ""), safe_json_dict(row[2]))
        return documents

    @staticmethod
    def _recall_copy_result(result, content: str, metadata: dict[str, Any]):
        if is_dataclass(result):
            return replace(result, content=content, metadata=metadata)
        cloned = copy.copy(result)
        cloned.content, cloned.metadata = content, metadata
        return cloned

    @staticmethod
    def _recall_live_rows(rows: list, metadata: dict[str, Any]) -> list:
        now = time.time()
        live = []
        for row in rows:
            try:
                expiry = float(row[4])
                active = row[3] == "active" and math.isfinite(expiry) and expiry > now
            except (TypeError, ValueError, OverflowError):
                active = False
            if not active:
                continue
            # Legacy atoms may omit scope; a populated scope must agree with
            # the parent already authorized by the recall/read entry point.
            if not atom_scope_matches_parent(row[5], row[6], metadata):
                continue
            live.append(row)
        return live

    async def resolve_live_facts(
        self, memory_id: int, content: str, metadata: dict[str, Any]
    ) -> list[str] | None:
        """Return live facts, None for an expired/missing atom-backed parent."""
        metadata = safe_json_dict(metadata)
        if str(metadata.get("status") or "active") != "active":
            return None
        if not getattr(self, "atom_enabled", True) or self.db_connection is None:
            return [line for line in content.splitlines() if line.strip()]
        rows = (await self._recall_atom_rows([memory_id])).get(int(memory_id))
        if rows is None and not metadata.get("atom_types"):
            return [line for line in content.splitlines() if line.strip()]
        facts = list(dict.fromkeys(
            str(row[2]).strip()
            for row in self._recall_live_rows(rows or [], metadata)
            if str(row[2]).strip()
        ))
        return facts or None

    async def filter_recall_lifecycle(
        self, results: list, *, skip_doc_ids: set[int] | None = None
    ) -> list:
        """Recheck parent state/TTL after merging without mutating cached inputs.

        Rebuilding only content is insufficient: injection prefers persona_summary
        and also prints key_facts. Every fact-bearing display field is rebuilt
        from the same live rows so expired facts cannot return through metadata.
        """
        if not results:
            return []
        skip = skip_doc_ids or set()
        if self.db_connection is None:
            return [r for r in results if str(
                safe_json_dict(r.metadata).get("status") or "active"
            ) == "active"]
        documents = await self._recall_document_rows([int(r.doc_id) for r in results])
        atom_enabled = getattr(self, "atom_enabled", True)
        pending = [r for r in results if int(r.doc_id) not in skip]
        grouped = (
            await self._recall_atom_rows([int(r.doc_id) for r in pending])
            if atom_enabled else {}
        )
        filtered = []
        for result in results:
            cached_metadata = safe_json_dict(result.metadata)
            if str(cached_metadata.get("status") or "active") != "active":
                continue
            document = documents.get(int(result.doc_id))
            if document is None:
                continue
            source_content, metadata = document
            if str(metadata.get("status") or "active") != "active":
                continue
            # A cached result was authorized in its original scope. Scope
            # migration must not let it follow a moved parent across that
            # authorization boundary, even when no atoms exist.
            if any(
                metadata.get(key) != cached_metadata.get(key)
                for key in ("session_id", "persona_id")
            ):
                continue
            if int(result.doc_id) in skip:
                filtered.append(result)
                continue
            rows = grouped.get(int(result.doc_id))
            if not atom_enabled or rows is None and not metadata.get("atom_types"):
                filtered.append(self._recall_copy_result(result, source_content, metadata))
                continue
            live = self._recall_live_rows(rows or [], metadata)
            facts = list(dict.fromkeys(
                str(row[2]).strip() for row in live if str(row[2]).strip()
            ))
            if not facts:
                continue
            content = "\n".join(facts)
            display_metadata = {
                **metadata,
                "persona_summary": content,
                "canonical_summary": content,
                "key_facts": facts,
                "topics": [],
                "time_tags": [],
                "retrieved_atom_ids": [int(row[0]) for row in live],
            }
            if all(row[7] is not None for row in live):
                display_metadata["atom_types"] = list(dict.fromkeys(
                    str(row[7]).casefold() for row in live
                ))
            filtered.append(self._recall_copy_result(result, content, display_metadata))
        return filtered

    async def get_memory_atom_lifecycle(self, memory_id: int) -> dict | None:
        rows = (await self._recall_atom_rows([int(memory_id)])).get(int(memory_id))
        if not rows:
            return None
        document = (await self._recall_document_rows([int(memory_id)])).get(
            int(memory_id)
        )
        metadata = document[1] if document else {}
        active = len(self._recall_live_rows(rows, metadata)) if document else 0
        return {"total": len(rows), "active": active, "hidden": len(rows) - active}

    async def snapshot_archived_atom_lifecycle(self, memory_id: int) -> list[dict]:
        """Capture original TTL/times before archive removes retrieval rows."""
        if not await self._recall_has_atom_table():
            return []
        async with self.db_connection.execute(
            "SELECT * FROM memory_atoms WHERE parent_memory_id=? ORDER BY id",
            (int(memory_id),),
        ) as cursor:
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in await cursor.fetchall()]

    async def restore_archived_atom_lifecycle(
        self, memory_id: int, snapshots: list[dict],
        *, session_id: str | None = None, persona_id: str | None = None,
    ) -> int:
        """Replace reconstructed atoms with the archived original lifecycle.

        AtomStore.insert intentionally refreshes TTL for new memories. Restoring
        an archive must bypass that behavior, including when all facts expired
        while archived. An empty snapshot removes any newly reconstructed atoms.
        """
        if not await self._recall_has_atom_table():
            return 0
        if not isinstance(snapshots, list) or any(not isinstance(row, dict) for row in snapshots):
            raise ValueError("invalid archived atom lifecycle snapshot")
        columns = (
            "parent_memory_id", "atom_type", "content", "entities", "importance",
            "confidence", "created_at", "last_accessed_at", "last_reinforced_at",
            "event_time", "ttl_days", "expires_at", "status", "reinforcement_count",
            "decay_type", "session_id", "persona_id", "metadata",
        )
        defaults = {
            "atom_type": "unknown", "entities": "[]", "importance": 0.5,
            "confidence": 0.7, "created_at": 0.0, "last_accessed_at": 0.0,
            "ttl_days": 0.0, "expires_at": 0.0, "status": "active",
            "reinforcement_count": 0, "decay_type": "exponential", "metadata": "{}",
        }

        async def write(connection):
            await connection.execute("SAVEPOINT anam_restore_atom_lifecycle")
            try:
                await connection.execute(
                    "DELETE FROM memory_atoms_fts WHERE CAST(atom_id AS INTEGER) IN "
                    "(SELECT id FROM memory_atoms WHERE parent_memory_id=?)",
                    (int(memory_id),),
                )
                await connection.execute("DELETE FROM memory_atoms WHERE parent_memory_id=?", (int(memory_id),))
                inserted = 0
                for snapshot in snapshots:
                    row = {**defaults, **snapshot, "parent_memory_id": int(memory_id)}
                    if not isinstance(row.get("content"), str) or not row["content"].strip():
                        continue
                    if session_id is not None:
                        row["session_id"] = session_id
                    if persona_id is not None:
                        row["persona_id"] = persona_id
                    for key in ("entities", "metadata"):
                        if not isinstance(row.get(key), str):
                            row[key] = json.dumps(row.get(key) or ({} if key == "metadata" else []))
                    cursor = await connection.execute(
                        f"INSERT INTO memory_atoms({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                        [row.get(key) for key in columns],
                    )
                    if row["status"] in ("active", "expired"):
                        await connection.execute(
                            "INSERT INTO memory_atoms_fts(atom_id,content) VALUES(?,?)",
                            (int(cursor.lastrowid), row["content"]),
                        )
                    inserted += 1
                await connection.execute("RELEASE SAVEPOINT anam_restore_atom_lifecycle")
                return inserted
            except BaseException:
                await connection.execute("ROLLBACK TO SAVEPOINT anam_restore_atom_lifecycle")
                await connection.execute("RELEASE SAVEPOINT anam_restore_atom_lifecycle")
                raise

        async def transaction(connection, *, dedicated):
            if not dedicated:
                # A compatibility connection may already belong to a caller's
                # transaction. Keep the savepoint without committing that work.
                return await write(connection)
            try:
                # FTS deletion reads its parent subquery before writing. Obtain
                # the writer lock first so maintenance cannot invalidate this
                # transaction's read snapshot (SQLITE_BUSY_SNAPSHOT).
                await connection.execute("BEGIN IMMEDIATE")
                inserted = await write(connection)
                await connection.commit()
                return inserted
            except BaseException:
                await connection.rollback()
                raise

        async def finish(connection, *, dedicated=False):
            operation = asyncio.create_task(transaction(connection, dedicated=dedicated))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not operation.cancelled():
                    operation.exception()
                raise

        atom_store = getattr(self, "atom_store", None)
        if atom_store is not None:
            async with atom_store._connect() as connection:
                return await finish(connection, dedicated=True)
        return await finish(self.db_connection)
