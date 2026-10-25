"""Durable, bounded administration records in the memory database.

Connections are short lived: no transaction is kept open across provider calls.
This is an incremental design informed by Memorix's operation/projection ledger.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import asynccontextmanager

import aiosqlite

from .sqlite_utils import configure_sqlite_connection, sqlite_connect_kwargs


class AdministrationStore:
    def __init__(self, db_path: str, config: dict | None = None):
        self.db_path, self.config = db_path, config or {}
        self.max_bytes = 128 * 1024 * 1024
        self.max_record_bytes = 16 * 1024 * 1024
        self.max_snapshot_bytes = 1024 * 1024
        self._budget_configured = False

    @asynccontextmanager
    async def connection(self, *, write=False):
        async with aiosqlite.connect(self.db_path, **sqlite_connect_kwargs(self.config)) as db:
            db.row_factory = aiosqlite.Row
            await configure_sqlite_connection(db, self.config)
            if write:
                await db.execute("BEGIN IMMEDIATE")
            try:
                if write and self._budget_configured:
                    await self._sync_limits(db)
                yield db
                if write:
                    await db.commit()
            except BaseException as exc:
                if write:
                    await db.rollback()
                if isinstance(exc, sqlite3.IntegrityError) and "anamnesis_admin_quota" in str(exc):
                    raise ValueError("管理记录、导入与恢复快照总容量已达上限，请先清理已完成记录") from exc
                raise

    async def initialize(self):
        async with self.connection(write=True) as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS memory_admin_operations(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, token_hash TEXT, token_expires REAL, token_actor TEXT)
            """)
            await db.execute("""CREATE TABLE IF NOT EXISTS memory_admin_items(
                operation_id TEXT NOT NULL, item_key TEXT NOT NULL, status TEXT NOT NULL,
                payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(operation_id,item_key))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS memory_episodes(
                id TEXT PRIMARY KEY, session_id TEXT, persona_id TEXT, source_session_id TEXT,
                title TEXT NOT NULL, summary TEXT NOT NULL, members TEXT NOT NULL,
                created_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1)""")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_admin_updated ON memory_admin_operations(updated_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_episode_scope ON memory_episodes(session_id,persona_id)")
            await self._create_budget(db)
        self._budget_configured = True

    async def _sync_limits(self, db):
        await db.execute("UPDATE memory_admin_quota SET max_bytes=?,max_record_bytes=? WHERE id=1",
                         (max(1, int(self.max_bytes)), max(1, int(self.max_record_bytes))))

    async def _create_budget(self, db):
        """Keep an O(1) UTF-8 byte ledger inside each SQLite write transaction.

        Episodes are often published through a direct connection; quota triggers
        cover those paths too. An aborted write also rolls back its byte delta.
        Payloads/results and generated episode content count toward the budget;
        status metadata does not make cancellation or cleanup consume capacity.
        """
        await db.execute("""CREATE TABLE IF NOT EXISTS memory_admin_quota(
            id INTEGER PRIMARY KEY CHECK(id=1), used_bytes INTEGER NOT NULL,
            max_bytes INTEGER NOT NULL, max_record_bytes INTEGER NOT NULL)""")
        tables = {
            "memory_admin_operations": ("payload", "result"),
            "memory_admin_items": ("payload", "result"),
            "memory_episodes": ("title", "summary", "members"),
        }

        def expression(columns, prefix=""):
            return "+".join(f"COALESCE(length(CAST({prefix}{column} AS BLOB)),0)" for column in columns)

        totals = []
        for table, columns in tables.items():
            totals.append(f"(SELECT COALESCE(SUM({expression(columns)}),0) FROM {table})")
        await db.execute("INSERT INTO memory_admin_quota(id,used_bytes,max_bytes,max_record_bytes) VALUES(1,0,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET max_bytes=excluded.max_bytes,max_record_bytes=excluded.max_record_bytes",
                         (max(1, int(self.max_bytes)), max(1, int(self.max_record_bytes))))
        await db.execute("UPDATE memory_admin_quota SET used_bytes=" + "+".join(totals) + " WHERE id=1")
        for table, columns in tables.items():
            new, old = expression(columns, "NEW."), expression(columns, "OLD.")
            for event in ("INSERT", "UPDATE", "DELETE"):
                name = f"anam_admin_quota_{table}_{event.lower()}"
                await db.execute(f"DROP TRIGGER IF EXISTS {name}")
                if event == "INSERT":
                    delta, growth = new, "1"
                elif event == "UPDATE":
                    delta, growth = f"({new})-({old})", f"({new})>({old})"
                else:
                    delta, growth = f"-({old})", "0"
                check = ""
                if event != "DELETE":
                    check = f"""SELECT CASE WHEN ({growth}) AND
                        ((SELECT used_bytes>max_bytes FROM memory_admin_quota WHERE id=1)
                         OR ({new})>(SELECT max_record_bytes FROM memory_admin_quota WHERE id=1))
                        THEN RAISE(ABORT,'anamnesis_admin_quota') END;"""
                await db.execute(f"""CREATE TRIGGER {name} AFTER {event} ON {table} BEGIN
                    UPDATE memory_admin_quota SET used_bytes=used_bytes+({delta}) WHERE id=1;
                    {check} END""")

    def _encode(self, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(raw.encode("utf-8")) > self.max_record_bytes:
            raise ValueError("单条管理记录超过容量上限，请减少输入或来源")
        # Recovery/correction snapshots must be bounded independently of their
        # short summary. Source messages can be much larger than summary text.
        if isinstance(value, dict):
            for key in ("before", "after", "tagged", "before_atom_lifecycle"):
                if key in value and len(json.dumps(value[key], ensure_ascii=False, allow_nan=False).encode("utf-8")) > self.max_snapshot_bytes:
                    raise ValueError("单条记忆及来源快照超过容量预算，请使用精确编辑或减少来源")
        return raw

    async def storage_usage(self):
        async with self.connection(write=True) as db:
            row = await (await db.execute("SELECT used_bytes,max_bytes,max_record_bytes FROM memory_admin_quota WHERE id=1")).fetchone()
            return dict(row)

    @staticmethod
    def decode(row):
        if row is None:
            return None
        data = dict(row)
        for key in ("payload", "result", "members"):
            if key in data:
                data[key] = json.loads(data[key])
        return data

    async def create(self, operation_id, kind, actor, payload, *, status="pending", keep_days=30, max_records=500):
        now = time.time()
        raw_payload = self._encode(payload)
        async with self.connection(write=True) as db:
            count = (await (await db.execute("SELECT COUNT(*) FROM memory_admin_operations")).fetchone())[0]
            if count >= max_records:
                # Never silently discard a recoverable deletion or an unfinished job.
                raise ValueError("任务与操作记录已达容量上限，请先清理已完成记录")
            await db.execute("INSERT INTO memory_admin_operations(id,kind,actor,status,payload,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                             (operation_id, kind, actor, status, raw_payload, now, now, now + keep_days*86400))

    async def get(self, operation_id):
        async with self.connection() as db:
            return self.decode(await (await db.execute("SELECT * FROM memory_admin_operations WHERE id=?", (operation_id,))).fetchone())

    async def update(self, operation_id, *, status=None, result=None, payload=None, expected_status=None):
        async with self.connection(write=True) as db:
            return await self.update_in_transaction(db, operation_id, status=status,
                result=result, payload=payload, expected_status=expected_status)

    async def update_in_transaction(self, db, operation_id, *, status=None, result=None, payload=None, expected_status=None):
        """Publish operation results together with their durable side effects."""
        fields, args = ["updated_at=?", "revision=revision+1"], [time.time()]
        for key, value in (("status", status), ("result", result), ("payload", payload)):
            if value is not None:
                fields.append(key + "=?")
                args.append(self._encode(value) if key != "status" else value)
        conditions = "id=?"
        args.append(operation_id)
        if expected_status is not None:
            if not expected_status:
                raise ValueError("需要提供允许的任务状态")
            conditions += " AND status IN (" + ",".join("?" for _ in expected_status) + ")"
            args.extend(expected_status)
        cursor = await db.execute("UPDATE memory_admin_operations SET " + ",".join(fields) + " WHERE " + conditions, args)
        return cursor.rowcount

    async def list(self, *, kind=None, limit=100):
        async with self.connection() as db:
            where, args = ("WHERE kind=?", [kind]) if kind else ("", [])
            rows = await (await db.execute(f"SELECT * FROM memory_admin_operations {where} ORDER BY created_at DESC LIMIT ?", (*args, max(1, min(500, limit))))).fetchall()
            return [self.decode(row) for row in rows]

    async def put_item(self, operation_id, item_key, payload, *, status="pending", result=None):
        raw_payload = self._encode(payload)
        raw_result = self._encode(result or {})
        async with self.connection(write=True) as db:
            await db.execute("INSERT INTO memory_admin_items VALUES(?,?,?,?,?) ON CONFLICT(operation_id,item_key) DO UPDATE SET status=excluded.status,payload=excluded.payload,result=excluded.result",
                             (operation_id, str(item_key), status, raw_payload, raw_result))

    async def items(self, operation_id):
        async with self.connection() as db:
            rows = await (await db.execute("SELECT * FROM memory_admin_items WHERE operation_id=? ORDER BY item_key", (operation_id,))).fetchall()
            return [self.decode(row) for row in rows]

    async def remove(self, operation_id):
        async with self.connection(write=True) as db:
            await db.execute("DELETE FROM memory_admin_items WHERE operation_id=?", (operation_id,))
            await db.execute("DELETE FROM memory_admin_operations WHERE id=?", (operation_id,))

    async def interrupt_jobs(self):
        async with self.connection(write=True) as db:
            await db.execute("UPDATE memory_admin_operations SET status='interrupted',updated_at=?,token_hash=NULL,token_expires=NULL WHERE status IN ('preparing','queued','running') AND kind IN ('import','evaluation','episode','projection')", (time.time(),))
