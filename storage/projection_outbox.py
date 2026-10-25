"""Versioned index synchronization intents, committed with document changes.

Inspired by Memorix's projection jobs. SQLite triggers bind the intent to the
primary transaction; completion is conditional on the version read by a worker.
"""

from __future__ import annotations

import asyncio
import math
import time

from astrbot.api import logger

from ..core.managers.memory_mutation import (
    index_projection_guard,
    memory_mutation_guard,
)
from .administration_store import AdministrationStore


class ProjectionOutbox:
    def __init__(self, engine):
        self.engine = engine
        self.store = AdministrationStore(engine.db_path, getattr(engine, "config", {}))

    async def initialize(self):
        async with self.store.connection(write=True) as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS memory_projection_jobs(
                memory_id INTEGER PRIMARY KEY, action TEXT NOT NULL,
                revision INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL, error TEXT)""")
            await db.execute("CREATE TABLE IF NOT EXISTS memory_projection_versions(memory_id INTEGER PRIMARY KEY,revision INTEGER NOT NULL)")
            await db.execute("INSERT INTO memory_projection_versions SELECT memory_id,revision FROM memory_projection_jobs WHERE 1 ON CONFLICT(memory_id) DO UPDATE SET revision=MAX(revision,excluded.revision)")
            enqueue = """INSERT INTO memory_projection_versions(memory_id,revision) VALUES({row}.id,1)
                ON CONFLICT(memory_id) DO UPDATE SET revision=revision+1;
                INSERT INTO memory_projection_jobs(memory_id,action,revision,updated_at)
                VALUES({row}.id,'{action}',(SELECT revision FROM memory_projection_versions WHERE memory_id={row}.id),unixepoch())
                ON CONFLICT(memory_id) DO UPDATE SET action=excluded.action,
                revision=excluded.revision,attempts=0,
                updated_at=excluded.updated_at,error=NULL;"""
            for event, row, action in (("INSERT", "NEW", "upsert"), ("DELETE", "OLD", "delete")):
                await db.execute(f"DROP TRIGGER IF EXISTS anam_projection_{event.lower()}")
                await db.execute(f"CREATE TRIGGER IF NOT EXISTS anam_projection_{event.lower()} AFTER {event} ON documents BEGIN {enqueue.format(row=row, action=action)} END")
            # Access statistics alone do not require a new embedding/graph.
            ignored = "'$.last_access_time','$.access_count'"
            old_meta = "CASE WHEN json_valid(OLD.metadata) THEN OLD.metadata ELSE '{}' END"
            new_meta = "CASE WHEN json_valid(NEW.metadata) THEN NEW.metadata ELSE '{}' END"
            await db.execute("DROP TRIGGER IF EXISTS anam_projection_update")
            await db.execute(f"""CREATE TRIGGER IF NOT EXISTS anam_projection_update AFTER UPDATE ON documents
                WHEN OLD.text IS NOT NEW.text OR json_remove({old_meta},{ignored}) IS NOT json_remove({new_meta},{ignored})
                BEGIN {enqueue.format(row='NEW', action='upsert')} END""")

    async def pending(self, limit=25):
        async with self.store.connection() as db:
            rows = await (await db.execute("SELECT * FROM memory_projection_jobs ORDER BY updated_at LIMIT ?", (max(1, min(100, limit)),))).fetchall()
            return [dict(row) for row in rows]

    async def complete(self, memory_id, revision):
        async with self.store.connection(write=True) as db:
            cursor = await db.execute("DELETE FROM memory_projection_jobs WHERE memory_id=? AND revision=?", (memory_id, revision))
            return cursor.rowcount == 1

    @staticmethod
    def _live_atoms(atoms, metadata):
        """Use the same TTL and parent-scope policy as live fact retrieval."""
        now, live = time.time(), []
        for atom in atoms:
            try:
                expiry = float(atom.expires_at)
            except (TypeError, ValueError, OverflowError):
                continue
            if (
                getattr(atom.status, "value", atom.status) != "active"
                or not math.isfinite(expiry)
                or expiry <= now
            ):
                continue
            if any(
                getattr(atom, key, None) is not None
                and metadata.get(key) is not None
                and getattr(atom, key) != metadata[key]
                for key in ("session_id", "persona_id")
            ):
                continue
            live.append(atom)
        return live

    async def repair(self, limit=10):
        engine = self.engine
        repaired, failed = 0, []
        for job in await self.pending(limit):
            try:
                async with index_projection_guard(engine), memory_mutation_guard(engine):
                    graph = engine.graph_memory_manager
                    if graph is not None and getattr(graph, "_rebuild_active", False):
                        continue
                    async with self.store.connection() as db:
                        pending = await (await db.execute("SELECT 1 FROM memory_write_ops WHERE memory_id=? AND status IN ('pending','needs_repair') LIMIT 1", (job["memory_id"],))).fetchone()
                    if pending:
                        continue  # Existing staged-write repair has priority.
                    snapshot = await engine._replacement_snapshot(job["memory_id"])
                    memory_id = job["memory_id"]
                    embedding = getattr(engine.faiss_db, "embedding_storage", None)
                    if snapshot is None or snapshot["metadata"].get("status", "active") != "active":
                        delete = getattr(embedding, "delete", None)
                        if callable(delete):
                            await delete([memory_id])
                        else:
                            await engine._set_index_rebuild_required(True)
                        if engine.bm25_retriever:
                            await engine.bm25_retriever.delete_document(memory_id)
                        if engine.graph_memory_manager:
                            await engine.graph_memory_manager.delete_memory(memory_id)
                    else:
                        import numpy as np
                        provider = getattr(engine.faiss_db, "embedding_provider", None)
                        get = getattr(provider, "get_embedding_with_retry", None) or getattr(provider, "get_embedding", None)
                        if not callable(get) or not callable(getattr(embedding, "delete", None)) or not callable(getattr(embedding, "insert", None)):
                            raise ValueError("宿主缺少索引同步接口，请执行完整索引重建")
                        vector = np.asarray(await get(snapshot["text"]), dtype=np.float32)
                        if (
                            vector.ndim != 1
                            or not vector.size
                            or not np.isfinite(vector).all()
                        ):
                            raise ValueError(
                                "Embedding 返回无效向量，保留旧索引并等待重试"
                            )
                        dimensions = (
                            getattr(embedding, "dimension", None),
                            getattr(getattr(embedding, "index", None), "d", None),
                        )
                        if any(
                            isinstance(dimension, int) and dimension > 0 and vector.size != dimension
                            for dimension in dimensions
                        ):
                            raise ValueError(
                                "Embedding 维度与现有索引不一致，请执行完整索引重建"
                            )
                        await embedding.delete([memory_id])
                        await embedding.insert(vector, memory_id)
                        await engine.bm25_retriever.add_document(memory_id, snapshot["text"], snapshot["metadata"])
                        if engine.graph_memory_manager:
                            atoms = await engine.atom_store.get_by_parent(memory_id) if engine.atom_store else []
                            live_atoms = self._live_atoms(atoms, snapshot["metadata"])
                            atom_backed = engine.atom_enabled and (atoms or snapshot["metadata"].get("atom_types"))
                            if atom_backed and not live_atoms:
                                await engine.graph_memory_manager.delete_memory(memory_id)
                            else:
                                content, metadata = snapshot["text"], dict(snapshot["metadata"])
                                if atom_backed:
                                    content = "\n".join(atom.content for atom in live_atoms)
                                    metadata.update(key_facts=[atom.content for atom in live_atoms], canonical_summary=content, persona_summary=content)
                                await engine.graph_memory_manager.index_memory(memory_id, content, metadata, live_atoms if atom_backed else None)
                        if not engine._replacement_snapshot_matches(await engine._replacement_snapshot(memory_id), snapshot):
                            continue
                    if graph is not None and getattr(graph, "_rebuild_active", False):
                        continue
                    repaired += int(await self.complete(memory_id, job["revision"]))
                    engine._invalidate_search_cache()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- projection adapters may fail independently; retain intent
                failed.append({"memory_id": job["memory_id"], "error": str(exc)[:300]})
                async with self.store.connection(write=True) as db:
                    await db.execute("UPDATE memory_projection_jobs SET attempts=attempts+1,error=? WHERE memory_id=? AND revision=?", (str(exc)[:300], job["memory_id"], job["revision"]))
                logger.warning("[Anamnesis] 版本化索引同步失败，保留待办", exc_info=True)
        return {"repaired": repaired, "failed": failed, "failed_count": len(failed)}
