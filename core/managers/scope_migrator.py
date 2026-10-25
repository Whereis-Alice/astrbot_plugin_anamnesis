"""Explicit, previewed memory-scope migration with SQLite backups and CAS."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from ...storage.sqlite_utils import configure_sqlite_connection, sqlite_connect_kwargs


class ScopeMigrationError(RuntimeError):
    def __init__(self, message: str, backup_paths: list[str] | None = None):
        super().__init__(message)
        self.backup_paths = backup_paths or []


class ScopeMigrator:
    """Move memory scopes without moving personal profiles or source messages.

    A fingerprint binds the explicit source, target, persona restriction, and
    current document/atom/graph rows. apply rechecks it under SQLite write locks
    before backing up and modifying anything. Vector values and IDs stay intact;
    their metadata lives in AstrBot DocumentStorage.db_path and is updated in the
    same attached transaction. Backups cover both files for crash recovery too.
    """

    def __init__(self, memory_engine, backup_dir: str | Path | None = None):
        self.engine = memory_engine
        self.main_path = Path(memory_engine.db_path).resolve()
        if not self.main_path.is_file():
            raise ValueError("memory database does not exist")
        self.config = getattr(memory_engine, "config", {})
        self.backup_dir = Path(backup_dir) if backup_dir else self.main_path.parent / "scope_migration_backups"
        document_store = getattr(getattr(memory_engine, "faiss_db", None), "document_storage", None)
        document_path = getattr(document_store, "db_path", None)
        if document_path and Path(document_path).resolve() != self.main_path:
            raise ValueError("memory engine and document storage database paths differ")
        graph_db = getattr(memory_engine, "graph_vector_db", None)
        self.graph_path = None
        if graph_db is not None:
            storage = getattr(graph_db, "document_storage", None)
            graph_path = getattr(storage, "db_path", None) or getattr(graph_db, "doc_store_path", None)
            if not graph_path or not Path(graph_path).is_file():
                raise ValueError("graph vector document database path is unavailable")
            self.graph_path = Path(graph_path).resolve()
            if self.graph_path == self.main_path:
                raise ValueError("graph vectors must use a separate document database")
        self._lock = asyncio.Lock()

    @staticmethod
    def _validate_scopes(source_scope: str, target_scope: str):
        if not isinstance(source_scope, str) or not source_scope.strip():
            raise ValueError("source scope is empty")
        if not isinstance(target_scope, str) or not target_scope.strip():
            raise ValueError("target scope is empty")
        if source_scope == target_scope:
            raise ValueError("source and target scopes are identical")
        if source_scope != source_scope.strip() or target_scope != target_scope.strip():
            raise ValueError("scopes must not contain leading or trailing whitespace")

    @asynccontextmanager
    async def _connect(self):
        async with aiosqlite.connect(self.main_path, **sqlite_connect_kwargs(self.config)) as db:
            await configure_sqlite_connection(db, self.config)
            db.row_factory = aiosqlite.Row
            if self.graph_path is not None:
                await db.execute("ATTACH DATABASE ? AS graph_vectors", (str(self.graph_path),))
            yield db

    @staticmethod
    async def _table_exists(db, table: str, schema="main") -> bool:
        cursor = await db.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,))
        return await cursor.fetchone() is not None

    @staticmethod
    async def _rows(db, sql, parameters=()) -> list[dict]:
        cursor = await db.execute(sql, parameters)
        return [dict(row) for row in await cursor.fetchall()]

    async def _collect(self, db, source_scope, target_scope, persona_id):
        persona_sql = " AND json_extract(metadata,'$.persona_id')=?" if persona_id is not None else ""
        persona_parameters = (persona_id,) if persona_id is not None else ()
        docs = await self._rows(
            db, "SELECT * FROM documents WHERE json_valid(metadata) "
            "AND json_extract(metadata,'$.session_id')=?"+persona_sql+" ORDER BY id",
            (source_scope, *persona_parameters),
        )
        targets = await self._rows(
            db, "SELECT * FROM documents WHERE json_valid(metadata) "
            "AND json_extract(metadata,'$.session_id')=?"+persona_sql+" ORDER BY id",
            (target_scope, *persona_parameters),
        )
        ids = [int(row["id"]) for row in docs]
        rows = {"documents": docs, "target_documents": targets, "atoms": [], "graph_entries": [], "graph_vectors": []}
        for key, table, id_column in (("atoms", "memory_atoms", "parent_memory_id"), ("graph_entries", "graph_entries", "source_memory_id")):
            if not await self._table_exists(db, table):
                continue
            for start in range(0, len(ids), 500):
                batch = ids[start:start+500]
                rows[key].extend(await self._rows(db, f"SELECT * FROM {table} WHERE {id_column} IN ({','.join('?' for _ in batch)}) ORDER BY id", batch))
        if self.graph_path is not None:
            if not await self._table_exists(db, "documents", "graph_vectors"):
                raise ValueError("graph vector document table is unavailable")
            for start in range(0, len(ids), 500):
                batch = ids[start:start+500]
                rows["graph_vectors"].extend(await self._rows(
                    db, "SELECT * FROM graph_vectors.documents WHERE json_valid(metadata) "
                    "AND CAST(json_extract(metadata,'$.source_memory_id') AS INTEGER) IN "
                    f"({','.join('?' for _ in batch)}) ORDER BY id", batch,
                ))
        for key in rows:
            rows[key].sort(key=lambda row: int(row["id"]))
        return rows

    @staticmethod
    def _metadata(row) -> dict[str, Any]:
        try:
            raw = row.get("metadata") or "{}"
            metadata = raw if isinstance(raw, dict) else json.loads(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid metadata on row {row['id']}") from error
        if not isinstance(metadata, dict):
            raise ValueError(f"metadata on row {row['id']} is not an object")
        return dict(metadata)

    def _plan(self, source_scope, target_scope, persona_id, rows):
        persona_counts = {}
        for document in rows["documents"]:
            persona = str(self._metadata(document).get("persona_id") or "")
            persona_counts[persona] = persona_counts.get(persona, 0)+1
        # Include the exact source and target snapshots in the compare-and-swap
        # token; a new destination memory also changes what preview reviewed.
        payload = {"source_scope": source_scope, "target_scope": target_scope,
                   "persona_id": persona_id, "rows": rows}
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        return {
            "source_scope": source_scope, "target_scope": target_scope, "persona_id": persona_id,
            "document_count": len(rows["documents"]), "target_document_count": len(rows["target_documents"]),
            "atom_count": len(rows["atoms"]), "graph_entry_count": len(rows["graph_entries"]),
            "graph_vector_count": len(rows["graph_vectors"]), "persona_counts": persona_counts,
            "fingerprint": fingerprint, "profile_scopes_changed": False,
        }

    async def preview(self, source_scope: str, target_scope: str, persona_id: str | None = None) -> dict:
        self._validate_scopes(source_scope, target_scope)
        async with self._connect() as db:
            await db.execute("BEGIN")
            try:
                rows = await self._collect(db, source_scope, target_scope, persona_id)
                return self._plan(source_scope, target_scope, persona_id, rows)
            finally:
                await db.rollback()

    async def _backups(self) -> list[str]:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+uuid.uuid4().hex[:10]
        paths = []
        for label, path in (("main", self.main_path), ("graph", self.graph_path)):
            if path is None:
                continue
            target = self.backup_dir / f"scope-migration-{stamp}-{label}.sqlite3"
            try:
                async with aiosqlite.connect(path, **sqlite_connect_kwargs(self.config)) as source, aiosqlite.connect(target) as destination:
                    await source.backup(destination)
                    check = await (await destination.execute("PRAGMA quick_check")).fetchone()
                    if check[0] != "ok":
                        raise ScopeMigrationError("scope migration backup validation failed", paths)
            except Exception as error:
                raise ScopeMigrationError(f"scope migration backup failed: {error}", paths) from error
            paths.append(str(target.resolve()))
        return paths

    def _migrated_metadata(self, row, source_scope, target_scope):
        metadata = self._metadata(row)
        metadata.setdefault("source_session_id", metadata.get("session_id") or row.get("session_id") or source_scope)
        metadata.setdefault("source_persona_id", metadata.get("persona_id", row.get("persona_id")))
        metadata["session_id"] = target_scope
        # Archived atom snapshots are part of the same memory, rather than
        # personal profiles; preserving old snapshot scope would hide live facts
        # after a later restore. Original provenance stays in snapshot metadata.
        snapshots = metadata.get("archived_atom_lifecycle")
        if isinstance(snapshots, list):
            for snapshot in snapshots:
                if not isinstance(snapshot, dict):
                    raise ValueError("invalid archived atom lifecycle snapshot")
                snapshot_metadata = self._metadata({"id": snapshot.get("id", 0), "metadata": snapshot.get("metadata", "{}")})
                snapshot_metadata.setdefault("source_session_id", snapshot.get("session_id") or source_scope)
                snapshot_metadata.setdefault("source_persona_id", snapshot.get("persona_id"))
                snapshot["session_id"] = target_scope
                snapshot_metadata["session_id"] = target_scope
                snapshot["metadata"] = json.dumps(snapshot_metadata, ensure_ascii=False)
        return json.dumps(metadata, ensure_ascii=False)

    async def _update_rows(self, db, rows, source_scope, target_scope):
        for key, table, scoped_column in (
            ("documents", "documents", False), ("atoms", "memory_atoms", True),
            ("graph_entries", "graph_entries", True), ("graph_vectors", "graph_vectors.documents", False),
        ):
            for row in rows[key]:
                metadata = self._migrated_metadata(row, source_scope, target_scope)
                if scoped_column:
                    cursor = await db.execute(f"UPDATE {table} SET session_id=?,metadata=? WHERE id=? AND metadata IS ?", (target_scope, metadata, row["id"], row["metadata"]))
                else:
                    cursor = await db.execute(f"UPDATE {table} SET metadata=? WHERE id=? AND metadata IS ?", (metadata, row["id"], row["metadata"]))
                if cursor.rowcount != 1:
                    raise ScopeMigrationError("scope migration row changed after preview")

    async def _run(self, source_scope, target_scope, fingerprint, persona_id, apply):
        self._validate_scopes(source_scope, target_scope)
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("preview fingerprint is required")
        backups = []
        async with self._lock, self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if self.graph_path is not None:
                    # Acquire the attached database's writer lock before the
                    # fingerprint check, without changing a row.
                    await db.execute("UPDATE graph_vectors.documents SET metadata=metadata WHERE 0")
                rows = await self._collect(db, source_scope, target_scope, persona_id)
                plan = self._plan(source_scope, target_scope, persona_id, rows)
                if fingerprint != plan["fingerprint"]:
                    raise ValueError("preview is stale; run preview again")
                if not rows["documents"]:
                    raise ValueError("source scope has no matching memories")
                backups = await self._backups()
                if apply:
                    await self._update_rows(db, rows, source_scope, target_scope)
                    await db.commit()
                    invalidate = getattr(self.engine, "_invalidate_search_cache", None)
                    if callable(invalidate):
                        invalidate()
                else:
                    await db.rollback()
                return {**plan, "applied": apply, "backup_paths": backups}
            except BaseException as error:
                await db.rollback()
                if backups and isinstance(error, Exception):
                    raise ScopeMigrationError(str(error), backups) from error
                raise

    async def _finish(self, operation):
        task = asyncio.create_task(operation)
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

    async def backup(self, source_scope: str, target_scope: str, fingerprint: str, persona_id: str | None = None) -> dict:
        return await self._finish(self._run(source_scope, target_scope, fingerprint, persona_id, False))

    async def apply(self, source_scope: str, target_scope: str, fingerprint: str, persona_id: str | None = None) -> dict:
        return await self._finish(self._run(source_scope, target_scope, fingerprint, persona_id, True))
