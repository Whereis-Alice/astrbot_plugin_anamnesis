"""
GraphStore 的 GraphStoreWriteMixin 拆分模块
自动从 storage/graph_store.py 拆分，保持行为不变
"""
from __future__ import annotations

import asyncio

import aiosqlite
from typing import Any
from ..core.models.graph_models import GraphEdge, GraphEntry, GraphNode


class GraphStoreWriteMixin:
    """图写入；连接工厂与批量常量由 GraphStore 提供。"""

    # 仅作类型声明；本分支没有上游后续引入的 _node_fts_available。
    _SQLITE_BATCH_SIZE: int
    person_alias_limit: int

    async def initialize(self) -> None:
        """Create tables used by the graph-memory layer."""
        async with self._connect() as db:
            # WAL is persistent and database-wide.  Enable it once at startup
            # (after busy_timeout has been configured by ``_connect``), rather
            # than on every graph read/write connection.
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_key TEXT NOT NULL UNIQUE,
                    node_type TEXT NOT NULL,
                    node_value TEXT NOT NULL,
                    canonical_value TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    edge_key TEXT NOT NULL UNIQUE,
                    source_node_id INTEGER NOT NULL,
                    target_node_id INTEGER NOT NULL,
                    relation_type TEXT NOT NULL,
                    source_memory_id INTEGER NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    confidence REAL NOT NULL DEFAULT 0.8,
                    status TEXT NOT NULL DEFAULT 'active',
                    metadata TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
                    FOREIGN KEY(target_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_key TEXT NOT NULL UNIQUE,
                    source_memory_id INTEGER NOT NULL,
                    session_id TEXT,
                    persona_id TEXT,
                    entry_type TEXT NOT NULL,
                    relation_type TEXT,
                    content TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    edge_id INTEGER,
                    vector_doc_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(edge_id) REFERENCES graph_edges(id) ON DELETE CASCADE
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_entry_nodes (
                    entry_id INTEGER NOT NULL,
                    node_id INTEGER NOT NULL,
                    PRIMARY KEY(entry_id, node_id),
                    FOREIGN KEY(entry_id) REFERENCES graph_entries(id) ON DELETE CASCADE,
                    FOREIGN KEY(node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
                )
                """
            )
            await db.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS livingmemory_graph_entries_fts
                USING fts5(content, entry_id UNINDEXED, tokenize='unicode61')
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_nodes_canonical ON graph_nodes(canonical_value)"
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_graph_edges_semantic
                ON graph_edges(source_node_id, target_node_id, relation_type)
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_edges_memory_id ON graph_edges(source_memory_id)"
            )
            # 孤儿节点判定需要「没有任何边指向该节点」。缺少 target_node_id 单列索引时
            # idx_graph_edges_semantic 的前导列用不上，判定会退化成全表扫描（线上 85k 行）。
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_node_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_entries_memory_id ON graph_entries(source_memory_id)"
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_graph_entries_scope_latest
                ON graph_entries(session_id, persona_id, source_memory_id, id DESC)
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_entries_session_id ON graph_entries(session_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_entries_persona_id ON graph_entries(persona_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_graph_entry_nodes_node ON graph_entry_nodes(node_id)"
            )
            await db.commit()

    async def upsert_node(self, node: GraphNode) -> int:
        """Insert or update one graph node and return its identifier."""
        now = self._now_iso()
        async with self._connect() as db:
            node_id = await self._upsert_node(db, node, now)
            await db.commit()
            return node_id

    async def upsert_nodes(self, nodes: list[GraphNode]) -> dict[str, int]:
        """Insert or update nodes in one transaction."""
        if not nodes:
            return {}

        now = self._now_iso()
        node_key_to_id: dict[str, int] = {}
        async with self._connect() as db:
            for node in nodes:
                node_key_to_id[node.node_key] = await self._upsert_node(db, node, now)
            await db.commit()
        return node_key_to_id

    async def _merge_person_metadata(
        self,
        db: aiosqlite.Connection,
        node: GraphNode,
    ) -> dict[str, Any]:
        """Accumulate person aliases instead of overwriting them.

        A plain ``metadata = excluded.metadata`` upsert throws away every former
        nickname, so a person node ends up remembering only the name used in the
        most recent memory. People rename themselves constantly; losing the
        history breaks recall for anyone who searches by an older name.
        """
        incoming = dict(node.metadata or {})
        cursor = await db.execute(
            "SELECT metadata FROM graph_nodes WHERE node_key = ?",
            (node.node_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return incoming

        existing = self._from_json(row[0])
        merged = {**existing, **incoming}

        aliases: list[str] = []
        seen: set[str] = set()
        for source in (incoming.get("aliases"), existing.get("aliases")):
            for alias in source or []:
                text = str(alias).strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                aliases.append(text)
                if len(aliases) >= self.person_alias_limit:
                    break
            if len(aliases) >= self.person_alias_limit:
                break
        merged["aliases"] = aliases
        # Once an identity is known to be a bot it stays a bot; a single message
        # missing the flag must not silently demote it back to a human.
        merged["is_bot"] = bool(existing.get("is_bot") or incoming.get("is_bot"))
        return merged

    async def _upsert_node(
        self,
        db: aiosqlite.Connection,
        node: GraphNode,
        now: str,
    ) -> int:
        metadata = node.metadata
        if node.node_type == "person":
            metadata = await self._merge_person_metadata(db, node)
        cursor = await db.execute(
            """
            INSERT INTO graph_nodes(
                node_key, node_type, node_value, canonical_value,
                metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_key) DO UPDATE SET
                node_value = excluded.node_value,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                node.node_key,
                node.node_type,
                node.value,
                node.canonical_value,
                self._to_json(metadata),
                now,
                now,
            ),
        )
        cursor = await db.execute(
            "SELECT id FROM graph_nodes WHERE node_key = ?",
            (node.node_key,),
        )
        row = await cursor.fetchone()
        return int(row[0])

    async def add_edge(
        self,
        edge: GraphEdge,
        node_key_to_id: dict[str, int],
    ) -> int:
        """Insert or update one graph edge and return its identifier.

        Uses semantic_edge_key for cross-memory merging:
        when the same semantic edge already exists (from a different memory),
        confidence is updated via EMA and weight accumulates evidence.
        """
        source_node_id = node_key_to_id[edge.source_key]
        target_node_id = node_key_to_id[edge.target_key]
        now = self._now_iso()
        async with self._connect() as db:
            edge_id = await self._add_edge(
                db,
                edge,
                source_node_id,
                target_node_id,
                now,
            )
            await db.commit()
            return edge_id

    async def add_edges(
        self,
        edges: list[GraphEdge],
        node_key_to_id: dict[str, int],
    ) -> dict[str, int]:
        """Insert or update edges in one transaction."""
        if not edges:
            return {}

        now = self._now_iso()
        edge_key_to_id: dict[str, int] = {}
        async with self._connect() as db:
            for edge in edges:
                source_node_id = node_key_to_id.get(edge.source_key)
                target_node_id = node_key_to_id.get(edge.target_key)
                if source_node_id is None or target_node_id is None:
                    continue
                edge_key_to_id[edge.edge_key] = await self._add_edge(
                    db,
                    edge,
                    source_node_id,
                    target_node_id,
                    now,
                )
            await db.commit()
        return edge_key_to_id

    async def _add_edge(
        self,
        db: aiosqlite.Connection,
        edge: GraphEdge,
        source_node_id: int,
        target_node_id: int,
        now: str,
    ) -> int:
        # Exact key match first (same memory, same edge)
        cursor = await db.execute(
            "SELECT id FROM graph_edges WHERE edge_key = ?",
            (edge.edge_key,),
        )
        row = await cursor.fetchone()
        if row:
            await db.execute(
                """
                UPDATE graph_edges
                SET weight = ?, confidence = ?, status = ?, metadata = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    edge.weight,
                    edge.confidence,
                    edge.status,
                    self._to_json(edge.metadata),
                    now,
                    row[0],
                ),
            )
            return int(row[0])

        # Cross-memory semantic merge: find same relation between same nodes.
        semantic_cursor = await db.execute(
            """
            SELECT id, confidence, weight FROM graph_edges
            WHERE source_node_id = ? AND target_node_id = ?
              AND relation_type = ?
            ORDER BY id ASC LIMIT 1
            """,
            (source_node_id, target_node_id, edge.relation_type),
        )
        semantic_row = await semantic_cursor.fetchone()

        if semantic_row:
            existing_id = int(semantic_row[0])
            old_conf = float(semantic_row[1] or 0.8)
            old_weight = float(semantic_row[2] or 1.0)
            merged_confidence = old_conf * 0.7 + edge.confidence * 0.3
            merged_weight = old_weight + edge.weight * 0.15
            await db.execute(
                """
                UPDATE graph_edges
                SET confidence = ?, weight = ?, updated_at = ?
                WHERE id = ?
                """,
                (merged_confidence, merged_weight, now, existing_id),
            )
            return existing_id

        cursor = await db.execute(
            """
            INSERT INTO graph_edges(
                edge_key, source_node_id, target_node_id, relation_type,
                source_memory_id, weight, confidence, status,
                metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(edge_key) DO UPDATE SET
                weight = excluded.weight,
                confidence = excluded.confidence,
                status = excluded.status,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                edge.edge_key,
                source_node_id,
                target_node_id,
                edge.relation_type,
                edge.source_memory_id,
                edge.weight,
                edge.confidence,
                edge.status,
                self._to_json(edge.metadata),
                now,
                now,
            ),
        )
        cursor = await db.execute(
            "SELECT id FROM graph_edges WHERE edge_key = ?",
            (edge.edge_key,),
        )
        row = await cursor.fetchone()
        return int(row[0])

    async def add_entry(
        self,
        entry: GraphEntry,
        node_key_to_id: dict[str, int],
        edge_id: int | None = None,
    ) -> int:
        """Insert or update a searchable graph entry."""
        now = self._now_iso()
        async with self._connect() as db:
            entry_id = await self._add_entry(db, entry, node_key_to_id, edge_id, now)
            await db.commit()
            return entry_id

    async def add_entries(
        self,
        entries: list[GraphEntry],
        node_key_to_id: dict[str, int],
        edge_key_to_id: dict[str, int],
    ) -> list[int]:
        """Insert or update searchable graph entries in one transaction."""
        if not entries:
            return []

        now = self._now_iso()
        entry_ids: list[int] = []
        async with self._connect() as db:
            for entry in entries:
                edge_id = None
                if entry.relation_type and len(entry.node_keys) >= 2:
                    edge_key = (
                        f"{entry.node_keys[0]}|{entry.relation_type}|"
                        f"{entry.node_keys[1]}|{entry.source_memory_id}"
                    )
                    edge_id = edge_key_to_id.get(edge_key)
                entry_ids.append(
                    await self._add_entry(db, entry, node_key_to_id, edge_id, now)
                )
            await db.commit()
        return entry_ids

    async def _add_entry(
        self,
        db: aiosqlite.Connection,
        entry: GraphEntry,
        node_key_to_id: dict[str, int],
        edge_id: int | None,
        now: str,
    ) -> int:
        cursor = await db.execute(
            "SELECT id FROM graph_entries WHERE entry_key = ?",
            (entry.entry_key,),
        )
        row = await cursor.fetchone()

        if row:
            entry_id = int(row[0])
            await db.execute(
                """
                UPDATE graph_entries
                SET session_id = ?, persona_id = ?, entry_type = ?, relation_type = ?,
                    content = ?, metadata = ?, edge_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    entry.session_id,
                    entry.persona_id,
                    entry.entry_type,
                    entry.relation_type,
                    entry.content,
                    self._to_json(entry.metadata),
                    edge_id,
                    now,
                    entry_id,
                ),
            )
            await db.execute(
                "DELETE FROM livingmemory_graph_entries_fts WHERE entry_id = ?",
                (entry_id,),
            )
            await db.execute(
                "DELETE FROM graph_entry_nodes WHERE entry_id = ?",
                (entry_id,),
            )
        else:
            cursor = await db.execute(
                """
                INSERT INTO graph_entries(
                    entry_key, source_memory_id, session_id, persona_id,
                    entry_type, relation_type, content, metadata,
                    edge_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.entry_key,
                    entry.source_memory_id,
                    entry.session_id,
                    entry.persona_id,
                    entry.entry_type,
                    entry.relation_type,
                    entry.content,
                    self._to_json(entry.metadata),
                    edge_id,
                    now,
                    now,
                ),
            )
            entry_id = int(cursor.lastrowid)

        await db.execute(
            "INSERT INTO livingmemory_graph_entries_fts(entry_id, content) VALUES (?, ?)",
            (entry_id, entry.content),
        )
        entry_node_rows = [
            (entry_id, node_id)
            for node_id in (
                node_key_to_id.get(node_key) for node_key in entry.node_keys
            )
            if node_id is not None
        ]
        if entry_node_rows:
            await db.executemany(
                "INSERT OR IGNORE INTO graph_entry_nodes(entry_id, node_id) VALUES (?, ?)",
                entry_node_rows,
            )
        return entry_id

    async def update_entry_vector_doc_id(
        self, entry_id: int, vector_doc_id: int
    ) -> None:
        """Persist the vector-store identifier for one graph entry."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE graph_entries SET vector_doc_id = ?, updated_at = ? WHERE id = ?",
                (vector_doc_id, self._now_iso(), entry_id),
            )
            await db.commit()

    async def update_entry_vector_doc_ids(
        self,
        entry_vector_doc_ids: dict[int, int],
    ) -> None:
        """Persist vector-store identifiers for graph entries in one transaction."""
        if not entry_vector_doc_ids:
            return

        now = self._now_iso()
        async with self._connect() as db:
            await db.executemany(
                "UPDATE graph_entries SET vector_doc_id = ?, updated_at = ? WHERE id = ?",
                [
                    (vector_doc_id, now, entry_id)
                    for entry_id, vector_doc_id in entry_vector_doc_ids.items()
                ],
            )
            await db.commit()

    async def clear_all(self) -> list[int]:
        """Clear every graph artifact and return referenced vector document IDs."""
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT DISTINCT vector_doc_id FROM graph_entries "
                "WHERE vector_doc_id IS NOT NULL"
            )
            vector_doc_ids = [int(row[0]) for row in await cursor.fetchall()]
            await db.execute("DELETE FROM livingmemory_graph_entries_fts")
            await db.execute("DELETE FROM graph_entry_nodes")
            await db.execute("DELETE FROM graph_entries")
            await db.execute("DELETE FROM graph_edges")
            await db.execute("DELETE FROM graph_nodes")
            await db.commit()
        return vector_doc_ids

    async def replace_all_from(self, shadow_db_path: str) -> None:
        """Atomically replace live graph tables from a fully built shadow DB."""
        async with self._connect() as db:
            await db.execute("PRAGMA foreign_keys = OFF")
            await db.execute("ATTACH DATABASE ? AS shadow_graph", (shadow_db_path,))
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("DELETE FROM livingmemory_graph_entries_fts")
                await db.execute("DELETE FROM graph_entry_nodes")
                await db.execute("DELETE FROM graph_entries")
                await db.execute("DELETE FROM graph_edges")
                await db.execute("DELETE FROM graph_nodes")

                await db.execute(
                    """
                    INSERT INTO graph_nodes(
                        id, node_key, node_type, node_value, canonical_value,
                        metadata, created_at, updated_at
                    )
                    SELECT id, node_key, node_type, node_value, canonical_value,
                           metadata, created_at, updated_at
                    FROM shadow_graph.graph_nodes
                    """
                )
                await db.execute(
                    """
                    INSERT INTO graph_edges(
                        id, edge_key, source_node_id, target_node_id,
                        relation_type, source_memory_id, weight, confidence,
                        status, metadata, created_at, updated_at
                    )
                    SELECT id, edge_key, source_node_id, target_node_id,
                           relation_type, source_memory_id, weight, confidence,
                           status, metadata, created_at, updated_at
                    FROM shadow_graph.graph_edges
                    """
                )
                await db.execute(
                    """
                    INSERT INTO graph_entries(
                        id, entry_key, source_memory_id, session_id, persona_id,
                        entry_type, relation_type, content, metadata, edge_id,
                        vector_doc_id, created_at, updated_at
                    )
                    SELECT id, entry_key, source_memory_id, session_id, persona_id,
                           entry_type, relation_type, content, metadata, edge_id,
                           vector_doc_id, created_at, updated_at
                    FROM shadow_graph.graph_entries
                    """
                )
                await db.execute(
                    """
                    INSERT INTO graph_entry_nodes(entry_id, node_id)
                    SELECT entry_id, node_id
                    FROM shadow_graph.graph_entry_nodes
                    """
                )
                await db.execute(
                    """
                    INSERT INTO livingmemory_graph_entries_fts(content, entry_id)
                    SELECT content, entry_id
                    FROM shadow_graph.livingmemory_graph_entries_fts
                    """
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            finally:
                await db.execute("DETACH DATABASE shadow_graph")

    async def delete_memory(self, source_memory_id: int) -> list[int]:
        """Delete graph artifacts belonging to one source memory."""
        vector_doc_ids: list[int] = []
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT id, vector_doc_id FROM graph_entries WHERE source_memory_id = ?",
                (source_memory_id,),
            )
            rows = await cursor.fetchall()
            entry_ids = [int(row[0]) for row in rows]
            vector_doc_ids = [int(row[1]) for row in rows if row[1] is not None]

            # 只有本次删除移除了引用的节点才可能变成孤儿，因此先按索引收集候选集，
            # 避免旧实现每删一条记忆都对 graph_edges / graph_entry_nodes 做全表扫描。
            candidate_node_ids = await self._collect_node_refs(
                db, entry_ids, [int(source_memory_id)]
            )

            for entry_batch in self._chunked(entry_ids, self._SQLITE_BATCH_SIZE):
                placeholders = ",".join("?" * len(entry_batch))
                await db.execute(
                    f"DELETE FROM livingmemory_graph_entries_fts WHERE entry_id IN ({placeholders})",
                    entry_batch,
                )
                await db.execute(
                    f"DELETE FROM graph_entry_nodes WHERE entry_id IN ({placeholders})",
                    entry_batch,
                )
                await db.execute(
                    f"DELETE FROM graph_entries WHERE id IN ({placeholders})",
                    entry_batch,
                )

            # 语义相同的边会跨记忆合并（_add_edge），此时其它记忆的条目可能通过
            # graph_entries.edge_id 指向这条边。graph_entries.edge_id 是 ON DELETE
            # CASCADE，直接按 source_memory_id 删边会连带删掉别人的条目（静默丢数据），
            # 所以只删除已经没有任何条目引用的边；本记忆的条目上面已经删完了。
            await db.execute(
                """
                DELETE FROM graph_edges
                WHERE source_memory_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM graph_entries ge WHERE ge.edge_id = graph_edges.id
                  )
                """,
                (source_memory_id,),
            )
            await self._delete_orphan_nodes(db, candidate_node_ids)
            await db.commit()
        return vector_doc_ids

    async def batch_delete_memories(
        self, source_memory_ids: list[int]
    ) -> dict[int, list[int]]:
        """Batch delete graph artifacts for multiple source memories."""
        result: dict[int, list[int]] = {}
        if not source_memory_ids:
            return result

        normalized_ids = sorted({int(item) for item in source_memory_ids})
        async with self._connect() as db:
            candidate_node_ids: set[int] = set()
            for batch in self._chunked(normalized_ids, self._SQLITE_BATCH_SIZE):
                memory_placeholders = ",".join("?" * len(batch))

                cursor = await db.execute(
                    f"""
                    SELECT id, source_memory_id, vector_doc_id
                    FROM graph_entries
                    WHERE source_memory_id IN ({memory_placeholders})
                    """,
                    batch,
                )
                rows = await cursor.fetchall()
                entry_ids: list[int] = []
                for row in rows:
                    entry_id = int(row[0])
                    memory_id = int(row[1])
                    vector_doc_id = row[2]
                    entry_ids.append(entry_id)
                    if vector_doc_id is not None:
                        result.setdefault(memory_id, []).append(int(vector_doc_id))

                candidate_node_ids.update(
                    await self._collect_node_refs(db, entry_ids, batch)
                )

                if entry_ids:
                    for entry_batch in self._chunked(
                        entry_ids,
                        self._SQLITE_BATCH_SIZE,
                    ):
                        entry_placeholders = ",".join("?" * len(entry_batch))
                        await db.execute(
                            f"DELETE FROM livingmemory_graph_entries_fts WHERE entry_id IN ({entry_placeholders})",
                            entry_batch,
                        )
                        await db.execute(
                            f"DELETE FROM graph_entry_nodes WHERE entry_id IN ({entry_placeholders})",
                            entry_batch,
                        )
                        await db.execute(
                            f"DELETE FROM graph_entries WHERE id IN ({entry_placeholders})",
                            entry_batch,
                        )

                # 同 delete_memory：跨记忆合并的边可能仍被其它记忆的条目引用，
                # 而 graph_entries.edge_id 是 ON DELETE CASCADE，必须先排除。
                await db.execute(
                    f"""
                    DELETE FROM graph_edges
                    WHERE source_memory_id IN ({memory_placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM graph_entries ge
                          WHERE ge.edge_id = graph_edges.id
                      )
                    """,
                    batch,
                )

            await self._delete_orphan_nodes(db, candidate_node_ids)
            await db.commit()
        return result

    # 单次 prune_orphans 的总行数预算，避免低配机器上出现长事务与内存峰值；
    # 没清完时返回 truncated=True，下一次维护会继续。
    _PRUNE_MAX_ROWS = 200_000

    async def _collect_node_refs(
        self,
        db: aiosqlite.Connection,
        entry_ids: list[int],
        source_memory_ids: list[int],
    ) -> set[int]:
        """收集即将被删除的条目与边所引用的节点 id，作为孤儿判定候选集。"""
        node_ids: set[int] = set()
        for entry_batch in self._chunked(list(entry_ids), self._SQLITE_BATCH_SIZE):
            placeholders = ",".join("?" * len(entry_batch))
            cursor = await db.execute(
                f"""
                SELECT DISTINCT node_id FROM graph_entry_nodes
                WHERE entry_id IN ({placeholders})
                """,
                entry_batch,
            )
            node_ids.update(int(row[0]) for row in await cursor.fetchall())
        for memory_batch in self._chunked(
            list(source_memory_ids), self._SQLITE_BATCH_SIZE
        ):
            placeholders = ",".join("?" * len(memory_batch))
            cursor = await db.execute(
                f"""
                SELECT source_node_id, target_node_id FROM graph_edges
                WHERE source_memory_id IN ({placeholders})
                """,
                memory_batch,
            )
            for row in await cursor.fetchall():
                node_ids.add(int(row[0]))
                node_ids.add(int(row[1]))
        return node_ids

    async def _delete_orphan_nodes(
        self,
        db: aiosqlite.Connection,
        candidate_node_ids: set[int] | None,
    ) -> int:
        """删除候选集中已经没有任何引用的节点，返回删除行数。

        节点只可能因为本次操作移除了它的引用才变成孤儿，所以「候选集 + NOT EXISTS」
        与旧实现的全表 ``NOT IN (UNION ...)`` 语义等价，但可以走索引，
        不再每删一条记忆就全表扫描 graph_edges（85k 行）与 graph_entry_nodes（178k 行）。
        """
        if not candidate_node_ids:
            return 0
        removed = 0
        for batch in self._chunked(
            sorted(candidate_node_ids), self._SQLITE_BATCH_SIZE
        ):
            placeholders = ",".join("?" * len(batch))
            cursor = await db.execute(
                f"""
                DELETE FROM graph_nodes
                WHERE id IN ({placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM graph_entry_nodes en
                      WHERE en.node_id = graph_nodes.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM graph_edges e
                      WHERE e.source_node_id = graph_nodes.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM graph_edges e
                      WHERE e.target_node_id = graph_nodes.id
                  )
                """,
                batch,
            )
            removed += max(0, int(cursor.rowcount or 0))
        return removed

    async def prune_orphans(
        self,
        *,
        batch_size: int = 2000,
        dry_run: bool = False,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        """清理图子系统中不再被引用的残留行。

        图子系统是主库体积的绝对主因（线上实测 233MB / 289MB 活跃数据）。写入图结构时
        节点、边、条目分属三个独立事务，中途失败会留下互不引用的残留行；FTS5 影子表也
        可能残留已删条目。这里按引用关系分五步分批清理：

        1. ``edges_without_entries``：所属记忆已经没有任何条目、且没有任何条目指向它的边
        2. ``edges_without_nodes``：源或目标节点已消失、且没有任何条目指向它的边
        3. ``entry_nodes``：指向已消失条目或节点的 graph_entry_nodes 关联行
        4. ``fts_rows``：livingmemory_graph_entries_fts 中条目已删除的影子行
        5. ``nodes``：既无条目关联又无边引用的孤儿节点

        两条删边规则都带「没有任何 graph_entries.edge_id 指向它」的前置条件，因为该外键是
        ON DELETE CASCADE，而语义相同的边会跨记忆合并；不加这个条件就可能连带删掉其它
        记忆的条目。有了它，清理过程可以证明不会触发任何级联删除。

        每批都先把 rowid 取到内存再按 rowid 删除（FTS5 虚拟表不能边扫边删），批间提交并
        让出事件循环，因此峰值内存只有一批 rowid。不执行 VACUUM——磁盘回收统一交给
        ``MemoryEngine.maintain_storage``。

        Args:
            batch_size: 每批处理的行数，钳制到 [100, 20000]。
            dry_run: 只统计不删除；此时统计值不含级联删除产生的连带行数。
            max_rows: 单次调用的总行数预算，None 时使用 ``_PRUNE_MAX_ROWS``。

        Returns:
            ``{success, dry_run, deleted, total, truncated, summary}``。
            ``truncated=True`` 表示预算用尽、仍可能有残留，下次维护会继续。
        """
        batch_size = max(100, min(int(batch_size), 20_000))
        budget = self._PRUNE_MAX_ROWS if max_rows is None else max(0, int(max_rows))
        steps: tuple[tuple[str, str, str], ...] = (
            (
                "edges_without_entries",
                "graph_edges",
                """
                SELECT rowid FROM graph_edges
                WHERE NOT EXISTS (
                    SELECT 1 FROM graph_entries ge
                    WHERE ge.source_memory_id = graph_edges.source_memory_id
                ) AND NOT EXISTS (
                    SELECT 1 FROM graph_entries ge2 WHERE ge2.edge_id = graph_edges.id
                )
                """,
            ),
            (
                "edges_without_nodes",
                "graph_edges",
                """
                SELECT rowid FROM graph_edges
                WHERE (
                    NOT EXISTS (
                        SELECT 1 FROM graph_nodes n
                        WHERE n.id = graph_edges.source_node_id
                    ) OR NOT EXISTS (
                        SELECT 1 FROM graph_nodes n
                        WHERE n.id = graph_edges.target_node_id
                    )
                ) AND NOT EXISTS (
                    SELECT 1 FROM graph_entries ge WHERE ge.edge_id = graph_edges.id
                )
                """,
            ),
            (
                "entry_nodes",
                "graph_entry_nodes",
                """
                SELECT rowid FROM graph_entry_nodes
                WHERE NOT EXISTS (
                    SELECT 1 FROM graph_entries ge
                    WHERE ge.id = graph_entry_nodes.entry_id
                ) OR NOT EXISTS (
                    SELECT 1 FROM graph_nodes n
                    WHERE n.id = graph_entry_nodes.node_id
                )
                """,
            ),
            (
                "fts_rows",
                "livingmemory_graph_entries_fts",
                """
                SELECT rowid FROM livingmemory_graph_entries_fts
                WHERE entry_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM graph_entries ge
                    WHERE ge.id = livingmemory_graph_entries_fts.entry_id
                )
                """,
            ),
            (
                "nodes",
                "graph_nodes",
                """
                SELECT rowid FROM graph_nodes
                WHERE NOT EXISTS (
                    SELECT 1 FROM graph_entry_nodes en WHERE en.node_id = graph_nodes.id
                ) AND NOT EXISTS (
                    SELECT 1 FROM graph_edges e WHERE e.source_node_id = graph_nodes.id
                ) AND NOT EXISTS (
                    SELECT 1 FROM graph_edges e WHERE e.target_node_id = graph_nodes.id
                )
                """,
            ),
        )

        report: dict[str, int] = {}
        truncated = False
        total = 0
        try:
            async with self._connect() as db:
                for name, table, select_sql in steps:
                    remaining = budget - total
                    if remaining <= 0:
                        truncated = True
                        break
                    if dry_run:
                        cursor = await db.execute(
                            f"SELECT COUNT(*) FROM ({select_sql} LIMIT ?)",
                            (remaining + 1,),
                        )
                        row = await cursor.fetchone()
                        found = int(row[0]) if row else 0
                        if found > remaining:
                            truncated = True
                            found = remaining
                        report[name] = found
                        total += found
                        continue
                    removed = 0
                    while True:
                        take = min(batch_size, budget - total - removed)
                        if take <= 0:
                            truncated = True
                            break
                        cursor = await db.execute(
                            f"{select_sql} LIMIT ?", (take,)
                        )
                        rowids = [int(row[0]) for row in await cursor.fetchall()]
                        if not rowids:
                            break
                        for chunk in self._chunked(rowids, self._SQLITE_BATCH_SIZE):
                            placeholders = ",".join("?" * len(chunk))
                            await db.execute(
                                f"DELETE FROM {table} WHERE rowid IN ({placeholders})",
                                chunk,
                            )
                        removed += len(rowids)
                        await db.commit()
                        # 让出事件循环，避免长时间阻塞消息处理。
                        await asyncio.sleep(0)
                        if len(rowids) < take:
                            break
                    report[name] = removed
                    total += removed
                if not dry_run:
                    await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "dry_run": bool(dry_run),
                "deleted": report,
                "total": total,
            }

        details = [f"{key}={value}" for key, value in report.items() if value]
        summary = ", ".join(details) if details else "无残留"
        if truncated:
            summary = f"{summary}（未清完，下次维护继续）"
        return {
            "success": True,
            "dry_run": bool(dry_run),
            "deleted": report,
            "total": total,
            "truncated": truncated,
            "summary": summary,
        }

    async def get_recent_memory_ids(
        self,
        limit: int = 12,
        session_id: str | None = None,
        persona_id: str | None = None,
    ) -> list[int]:
        """Return recently updated memory identifiers represented in the graph."""
        limit = max(1, min(limit, 200))
        filters: list[str] = []
        params: list[Any] = []

        if session_id is not None:
            filters.append("session_id = ?")
            params.append(session_id)
        if persona_id is not None:
            filters.append("persona_id = ?")
            params.append(persona_id)

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT source_memory_id, MAX(id) AS latest_entry_id
                FROM graph_entries
                {where_clause}
                GROUP BY source_memory_id
                ORDER BY latest_entry_id DESC
                LIMIT ?
                """,
                (*params, limit),
            )
            rows = await cursor.fetchall()

        return [int(row["source_memory_id"]) for row in rows]
