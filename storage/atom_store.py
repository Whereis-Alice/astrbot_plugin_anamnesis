"""SQLite-backed storage for time-aware memory atoms."""

from __future__ import annotations

import json
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from astrbot.api import logger

from ..core.models.memory_atom import (
    AtomStatus,
    AtomType,
    DecayType,
    MemoryAtom,
    compute_ttl,
)
from ..core.recall_persona import persona_filter_requested, persona_sql_filter
from .sqlite_utils import configure_sqlite_connection, sqlite_connect_kwargs


class AtomStore:
    """Persist memory atoms with FTS search support."""

    _SQLITE_BATCH_SIZE = 500

    def __init__(self, db_path: str, config: dict[str, Any] | None = None):
        self.db_path = db_path
        self.config = config or {}

    @asynccontextmanager
    async def _connect(self):
        # Do not change the database-wide journal mode on every connection;
        # that PRAGMA is itself a writer and was a major source of lock
        # contention during concurrent recall and graph/atom writes.
        db = await aiosqlite.connect(
            self.db_path,
            **sqlite_connect_kwargs(self.config),
        )
        try:
            await configure_sqlite_connection(db, self.config)
            yield db
        finally:
            await db.close()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _to_json(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        return json.dumps(payload if payload is not None else {}, ensure_ascii=False)

    @staticmethod
    def _from_json(payload: str | dict[str, Any] | None) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if not payload:
            return {}
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    async def initialize(self) -> None:
        """Create tables for memory atoms."""
        async with self._connect() as db:
            # Enable WAL once during store initialisation.  ``_connect`` has
            # already installed the busy timeout, so this setup operation also
            # waits correctly if another startup component is still writing.
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_atoms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_memory_id INTEGER NOT NULL,
                    atom_type TEXT NOT NULL DEFAULT 'unknown',
                    content TEXT NOT NULL,
                    entities TEXT DEFAULT '[]',
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.7,
                    created_at REAL NOT NULL,
                    last_accessed_at REAL NOT NULL,
                    last_reinforced_at REAL,
                    event_time REAL,
                    ttl_days REAL NOT NULL DEFAULT 30.0,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    reinforcement_count INTEGER NOT NULL DEFAULT 0,
                    decay_type TEXT NOT NULL DEFAULT 'exponential',
                    session_id TEXT,
                    persona_id TEXT,
                    metadata TEXT DEFAULT '{}'
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_atoms_parent ON memory_atoms(parent_memory_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_atoms_status ON memory_atoms(status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_atoms_expires ON memory_atoms(expires_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_atoms_session ON memory_atoms(session_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_atoms_persona ON memory_atoms(persona_id)"
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_atoms_scope_status
                ON memory_atoms(status, session_id, persona_id)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_atoms_status_expires
                ON memory_atoms(status, expires_at)
                """
            )
            await db.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_atoms_fts
                USING fts5(content, atom_id UNINDEXED, tokenize='unicode61')
                """
            )
            # Old releases did not index existing atoms. Repair identities and
            # content, including equal-count mismatches, without resurrecting
            # deliberately forgotten atoms on every restart.
            removed = await db.execute(
                "DELETE FROM memory_atoms_fts WHERE NOT EXISTS ("
                "SELECT 1 FROM memory_atoms ma WHERE ma.id=memory_atoms_fts.atom_id "
                "AND ma.status IN ('active','expired') "
                "AND ma.content=memory_atoms_fts.content)"
            )
            duplicates = await db.execute(
                "DELETE FROM memory_atoms_fts WHERE rowid NOT IN ("
                "SELECT MIN(rowid) FROM memory_atoms_fts GROUP BY CAST(atom_id AS INTEGER))"
            )
            inserted = await db.execute(
                "INSERT INTO memory_atoms_fts(atom_id, content) "
                "SELECT ma.id, ma.content FROM memory_atoms ma "
                "WHERE ma.status IN ('active','expired') AND ma.id NOT IN ("
                "SELECT CAST(atom_id AS INTEGER) FROM memory_atoms_fts)"
            )
            if removed.rowcount or duplicates.rowcount or inserted.rowcount:
                logger.info(
                    "[AtomStore] 修复原子 FTS: "
                    f"补入={inserted.rowcount}, 删除={removed.rowcount}, "
                    f"去重={duplicates.rowcount}"
                )
            await db.commit()

    async def insert(self, atom: MemoryAtom, *, preserve_lifecycle: bool = False) -> int:
        """Insert a new atom and return its id. Updates atom.atom_id in place."""
        if not (preserve_lifecycle and math.isfinite(atom.expires_at) and atom.expires_at > 0):
            self._prepare_atom_for_insert(atom)

        async with self._connect() as db:
            atom_id = await self._insert_atom(db, atom)
            await db.commit()
        return atom_id

    async def insert_many(self, atoms: list[MemoryAtom], *, preserve_lifecycle: bool = False) -> list[int]:
        """Insert in batches; interrupted-write replay can retain original times.

        Lifecycle preservation requires a finite positive stored expiry. Atoms
        that have never been prepared still receive their initial TTL normally.
        """
        if not atoms:
            return []

        atom_ids: list[int] = []
        async with self._connect() as db:
            for index in range(0, len(atoms), self._SQLITE_BATCH_SIZE):
                batch = atoms[index : index + self._SQLITE_BATCH_SIZE]
                batch_atom_ids: list[int] = []
                prepared_batch: list[MemoryAtom] = []
                try:
                    for atom in batch:
                        if not (preserve_lifecycle and math.isfinite(atom.expires_at) and atom.expires_at > 0):
                            self._prepare_atom_for_insert(atom)
                        prepared_batch.append(atom)
                        batch_atom_ids.append(await self._insert_atom(db, atom))
                    await db.commit()
                except Exception:
                    await db.rollback()
                    for atom in prepared_batch:
                        atom.atom_id = 0
                    raise
                atom_ids.extend(batch_atom_ids)
        return atom_ids

    def _prepare_atom_for_insert(self, atom: MemoryAtom) -> None:
        """Populate time-derived fields before persistence."""
        now = time.time()
        atom.created_at = now
        atom.last_accessed_at = now
        ttl, decay = compute_ttl(
            atom.atom_type, atom.importance, atom.reinforcement_count, atom.event_time
        )
        atom.ttl_days = ttl
        atom.decay_type = decay
        atom.expires_at = now + ttl * 86400.0

    async def _insert_atom(
        self,
        db: aiosqlite.Connection,
        atom: MemoryAtom,
    ) -> int:
        cursor = await db.execute(
            """
            INSERT INTO memory_atoms (
                parent_memory_id, atom_type, content, entities,
                importance, confidence, created_at, last_accessed_at,
                last_reinforced_at, event_time, ttl_days, expires_at,
                status, reinforcement_count, decay_type,
                session_id, persona_id, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                atom.parent_memory_id,
                atom.atom_type.value,
                atom.content,
                json.dumps(atom.entities, ensure_ascii=False),
                atom.importance,
                atom.confidence,
                atom.created_at,
                atom.last_accessed_at,
                atom.last_reinforced_at,
                atom.event_time,
                atom.ttl_days,
                atom.expires_at,
                atom.status.value,
                atom.reinforcement_count,
                atom.decay_type.value,
                atom.session_id,
                atom.persona_id,
                self._to_json(atom.metadata),
            ),
        )
        atom_id = int(cursor.lastrowid)
        atom.atom_id = atom_id

        await db.execute(
            "INSERT INTO memory_atoms_fts(atom_id, content) VALUES (?, ?)",
            (atom_id, atom.content),
        )
        return atom_id

    async def get(self, atom_id: int) -> MemoryAtom | None:
        """Retrieve a single atom by id."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM memory_atoms WHERE id = ?", (atom_id,)
            )
            row = await cursor.fetchone()
        return self._row_to_atom(row) if row else None

    async def get_by_parent(self, parent_memory_id: int) -> list[MemoryAtom]:
        """Retrieve all atoms belonging to a parent memory document."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM memory_atoms WHERE parent_memory_id = ? ORDER BY id ASC",
                (parent_memory_id,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_atom(row) for row in rows]

    async def search_fts(
        self,
        query: str,
        limit: int = 20,
        session_id: str | None = None,
        persona_id: str | None = None,
        include_expired: bool = False,
    ) -> list[MemoryAtom]:
        """Full-text search over atom content, returning time-scored results."""
        if not query or not query.strip():
            return []

        # Use bare tokens for CJK compatibility; quoted phrases only for multi-word
        tokens = [token for token in query.strip().split() if token]
        if not tokens:
            return []
        escaped = [token.replace('"', '""') for token in tokens]
        # Wrap tokens longer than 1 char (or containing spaces) in quotes
        fts_tokens = [
            f'"{token}"' if (" " in token or len(token) > 3) else token
            for token in escaped
        ]
        fts_query = " OR ".join(fts_tokens)

        filters = ["ma.status = 'active'"] if not include_expired else []
        params: list[Any] = [fts_query]
        cutoff = time.time()
        if not include_expired:
            filters.append("CAST(ma.expires_at AS REAL) > ?")
            params.append(cutoff)
        if session_id is not None:
            filters.append("ma.session_id = ?")
            params.append(session_id)
        if persona_filter_requested(persona_id):
            condition, values = persona_sql_filter("ma.persona_id", persona_id)
            filters.append(condition)
            params.extend(values)

        where_clause = f"AND {' AND '.join(filters)}" if filters else ""

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            # Try FTS first
            try:
                cursor = await db.execute(
                    f"""
                    SELECT ma.*, bm25(memory_atoms_fts) AS bm25_score
                    FROM memory_atoms_fts
                    JOIN memory_atoms ma ON ma.id = memory_atoms_fts.atom_id
                    WHERE memory_atoms_fts MATCH ? {where_clause}
                    ORDER BY bm25_score ASC
                    LIMIT ?
                    """,
                    (*params, limit),
                )
                rows = await cursor.fetchall()
            except Exception:
                rows = []

            # Fallback to LIKE when FTS returns nothing
            if not rows:
                like_clauses = " OR ".join(["ma.content LIKE ?" for _ in tokens])
                like_params_full: list[Any] = [f"%{t}%" for t in tokens]
                status_filter = (
                    "AND ma.status = 'active'" if not include_expired else ""
                )
                expiry_filter = "AND CAST(ma.expires_at AS REAL) > ?" if not include_expired else ""
                session_filter = (
                    "AND ma.session_id = ?" if session_id is not None else ""
                )
                persona_condition, persona_values = persona_sql_filter(
                    "ma.persona_id", persona_id
                )
                persona_filter = (
                    "AND " + persona_condition
                    if persona_filter_requested(persona_id)
                    else ""
                )
                if not include_expired:
                    like_params_full.append(cutoff)
                if session_id is not None:
                    like_params_full.append(session_id)
                if persona_filter_requested(persona_id):
                    like_params_full.extend(persona_values)
                cursor = await db.execute(
                    f"""
                    SELECT ma.*, 0.5 AS bm25_score
                    FROM memory_atoms ma
                    WHERE ({like_clauses}) {status_filter} {expiry_filter} {session_filter} {persona_filter}
                    ORDER BY ma.id DESC
                    LIMIT ?
                    """,
                    (*like_params_full, limit),
                )
                rows = await cursor.fetchall()

        if not rows:
            return []

        scores = [float(row["bm25_score"]) for row in rows]
        max_score = max(scores)
        min_score = min(scores)
        score_range = max_score - min_score

        atoms: list[MemoryAtom] = []
        now = time.time()
        for row in rows:
            atom = self._row_to_atom(row)
            if not include_expired and (
                not math.isfinite(atom.expires_at) or atom.is_expired(now)
            ):
                continue
            normalized = (
                1.0
                if score_range == 0
                else (max_score - float(row["bm25_score"])) / score_range
            )
            atom.metadata["bm25_score"] = normalized
            atom.metadata["temporal_score"] = atom.compute_temporal_score(now)
            atoms.append(atom)

        atoms.sort(
            key=lambda a: (
                float(a.metadata.get("bm25_score", 0))
                * float(a.metadata.get("temporal_score", 1))
            ),
            reverse=True,
        )
        return atoms

    async def update_status(self, atom_id: int, status: AtomStatus) -> bool:
        """Update the lifecycle status of one atom."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE memory_atoms SET status = ? WHERE id = ?",
                (status.value, atom_id),
            )
            await db.commit()
        return True

    async def touch(self, atom_id: int) -> None:
        """Update last_accessed_at for an atom."""
        now = time.time()
        async with self._connect() as db:
            await db.execute(
                "UPDATE memory_atoms SET last_accessed_at = ? WHERE id = ?",
                (now, atom_id),
            )
            await db.commit()

    async def reinforce(
        self, atom_id: int, new_confidence: float | None = None
    ) -> None:
        """Record a reinforcement event, extending TTL and optionally boosting confidence."""
        now = time.time()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            # 抢占写锁，避免并发 reinforce 对同一 atom 产生读-改-写丢失更新。
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT reinforcement_count, importance, confidence, atom_type, event_time FROM memory_atoms WHERE id = ?",
                (atom_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return

            new_count = int(row["reinforcement_count"]) + 1
            importance = float(row["importance"])
            atom_type = AtomType(row["atom_type"])
            event_time = float(row["event_time"]) if row["event_time"] else None
            new_ttl, decay = compute_ttl(atom_type, importance, new_count, event_time)

            confidence = (
                new_confidence
                if new_confidence is not None
                else float(row["confidence"])
            )
            # EMA update if new_confidence provided
            if new_confidence is not None:
                confidence = float(row["confidence"]) * 0.7 + new_confidence * 0.3

            await db.execute(
                """
                UPDATE memory_atoms
                SET reinforcement_count = ?, confidence = ?,
                    ttl_days = ?, expires_at = ?, decay_type = ?,
                    last_reinforced_at = ?
                WHERE id = ?
                """,
                (
                    new_count,
                    confidence,
                    new_ttl,
                    now + new_ttl * 86400.0,
                    decay.value,
                    now,
                    atom_id,
                ),
            )
            await db.commit()

    async def expire_stale_atoms(self) -> int:
        """Mark atoms whose expires_at has passed as EXPIRED. Returns count."""
        now = time.time()
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE memory_atoms SET status = ? WHERE status = 'active' AND expires_at < ?",
                (AtomStatus.EXPIRED.value, now),
            )
            await db.commit()
            return cursor.rowcount

    async def cleanup_forgotten(self, older_than_days: float = 7.0) -> int:
        """Purge old forgotten atoms and their FTS rows in one transaction."""
        cutoff = time.time() - older_than_days * 86400.0
        return await self._remove_atom_matches([("status = 'forgotten' AND expires_at < ?", [cutoff])])

    async def forget_expired_atoms(self, older_than_days: float = 7.0) -> int:
        """Soft-delete old expired atoms and remove them from the FTS index."""
        cutoff = time.time() - older_than_days * 86400.0
        return await self._remove_atom_matches(
            [("status = 'expired' AND expires_at < ?", [cutoff])], new_status=AtomStatus.FORGOTTEN,
        )

    async def delete_by_parent(self, parent_memory_id: int) -> int:
        """Delete all atoms belonging to a parent memory. Returns count."""
        return await self._remove_atom_matches([("parent_memory_id = ?", [parent_memory_id])])

    async def batch_delete_by_parent(self, parent_memory_ids: list[int]) -> int:
        """Delete atoms for multiple parent memories in bulk."""
        normalized_ids = sorted({int(item) for item in parent_memory_ids})
        if not normalized_ids:
            return 0

        matches = []
        for index in range(0, len(normalized_ids), self._SQLITE_BATCH_SIZE):
            batch = normalized_ids[index : index + self._SQLITE_BATCH_SIZE]
            matches.append(("parent_memory_id IN (" + ",".join("?" * len(batch)) + ")", batch))
        return await self._remove_atom_matches(matches)

    async def _remove_atom_matches(
        self, matches: list[tuple[str, list[Any]]], *, new_status: AtomStatus | None = None,
    ) -> int:
        """Apply internal predicates to atoms and FTS under the same writer lock.

        Subqueries avoid one parameter per atom when a parent has many facts.
        Predicates remain current until both updates commit, preventing cleanup
        from deleting a fact concurrently moved to another lifecycle state.
        """
        affected = 0
        async with self._connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                for where, parameters in matches:
                    await db.execute(
                        "DELETE FROM memory_atoms_fts WHERE CAST(atom_id AS INTEGER) IN "
                        f"(SELECT id FROM memory_atoms WHERE {where})", parameters,
                    )
                    if new_status is None:
                        cursor = await db.execute(f"DELETE FROM memory_atoms WHERE {where}", parameters)
                    else:
                        cursor = await db.execute(
                            f"UPDATE memory_atoms SET status=? WHERE {where}", [new_status.value, *parameters],
                        )
                    affected += max(0, cursor.rowcount)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return affected

    async def get_stats(self) -> dict[str, int]:
        """Return per-status atom counts."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT status, COUNT(*) AS cnt FROM memory_atoms GROUP BY status"
            )
            rows = await cursor.fetchall()
        stats: dict[str, int] = {s.value: 0 for s in AtomStatus}
        for row in rows:
            stats[row["status"]] = int(row["cnt"])
        return stats

    async def count_atoms(self) -> int:
        """Return the total number of atoms."""
        async with self._connect() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM memory_atoms")
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def count_by_type(self) -> dict[str, int]:
        """Return per-type atom counts for frontend display."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT atom_type, COUNT(*) AS cnt FROM memory_atoms GROUP BY atom_type"
            )
            rows = await cursor.fetchall()
        breakdown: dict[str, int] = {}
        for row in rows:
            breakdown[row["atom_type"]] = int(row["cnt"])
        return breakdown

    def _row_to_atom(self, row: aiosqlite.Row) -> MemoryAtom:
        """Map a database row to a MemoryAtom instance."""
        return MemoryAtom(
            atom_id=int(row["id"]),
            parent_memory_id=int(row["parent_memory_id"]),
            atom_type=AtomType(row["atom_type"]),
            content=row["content"],
            entities=json.loads(row["entities"]) if row["entities"] else [],
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            created_at=float(row["created_at"]),
            last_accessed_at=float(row["last_accessed_at"]),
            last_reinforced_at=float(row["last_reinforced_at"])
            if row["last_reinforced_at"]
            else None,
            event_time=float(row["event_time"]) if row["event_time"] else None,
            ttl_days=float(row["ttl_days"]),
            expires_at=float(row["expires_at"]),
            status=AtomStatus(row["status"]),
            reinforcement_count=int(row["reinforcement_count"]),
            decay_type=DecayType(row["decay_type"]),
            session_id=row["session_id"],
            persona_id=row["persona_id"],
            metadata=self._from_json(row["metadata"]),
        )


__all__ = ["AtomStore"]
