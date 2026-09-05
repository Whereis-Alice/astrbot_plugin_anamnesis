"""SQLite-backed graph-memory storage."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .graph_store_write import GraphStoreWriteMixin
from .graph_store_read import GraphStoreReadMixin
from .graph_store_snapshot import GraphStoreSnapshotMixin

class GraphStore(GraphStoreWriteMixin, GraphStoreReadMixin, GraphStoreSnapshotMixin):
    """Persist graph nodes, edges, and searchable entries."""

    _SQLITE_BATCH_SIZE = 500
    _NODE_TOKEN_QUERY_BATCH_SIZE = 200
    _DEFAULT_PERSON_ALIAS_LIMIT = 40

    def __init__(self, db_path: str, config: dict[str, Any] | None = None):
        self.db_path = db_path
        options = config or {}
        try:
            limit = int(
                options.get("alias_max_per_identity", self._DEFAULT_PERSON_ALIAS_LIMIT)
            )
        except (TypeError, ValueError):
            limit = self._DEFAULT_PERSON_ALIAS_LIMIT
        #: person 节点 metadata 里缓存的昵称上限。完整改名史由 person_aliases 表
        #: 负责，这里只是给图查询用的就近缓存，必须有界以免 metadata 无限膨胀。
        self.person_alias_limit = max(1, limit)

    @asynccontextmanager
    async def _connect(self):
        """创建新的SQLite连接并启用WAL模式和busy_timeout。"""
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            await db.close()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _to_json(payload: dict[str, Any] | None) -> str:
        return json.dumps(payload or {}, ensure_ascii=False)

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

    @staticmethod
    def _chunked(items: list[int], size: int) -> list[list[int]]:
        return [items[index : index + size] for index in range(0, len(items), size)]

__all__ = ["GraphStore"]
