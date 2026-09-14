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
from .sqlite_utils import configure_sqlite_connection, sqlite_connect_kwargs

class GraphStore(GraphStoreWriteMixin, GraphStoreReadMixin, GraphStoreSnapshotMixin):
    """Persist graph nodes, edges, and searchable entries."""

    _SQLITE_BATCH_SIZE = 500
    _NODE_TOKEN_QUERY_BATCH_SIZE = 200
    _DEFAULT_PERSON_ALIAS_LIMIT = 40

    def __init__(self, db_path: str, config: dict[str, Any] | None = None):
        self.db_path = db_path
        options = config or {}
        # Keep the normalized options on the store so every short-lived
        # connection (including read-only graph queries) uses the same SQLite
        # busy-timeout policy.  Previously only ``person_alias_limit`` was
        # retained, which silently discarded any connection tuning options.
        self.config = options
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
        """创建新的SQLite连接并应用每连接设置。

        WAL 模式是数据库级持久设置，只在 ``initialize`` 中启用；每次
        打开连接都执行该 PRAGMA 会产生额外的写锁竞争。
        """
        db = await aiosqlite.connect(
            self.db_path,
            **sqlite_connect_kwargs(getattr(self, "config", None)),
        )
        try:
            await configure_sqlite_connection(
                db,
                getattr(self, "config", None),
                foreign_keys=True,
            )
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
