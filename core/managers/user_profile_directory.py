"""Administrator profile directory; display aggregation keeps storage scopes intact."""

from __future__ import annotations

import json
import time
from typing import Any


class UserProfileDirectoryMixin:
    @staticmethod
    def _directory_owner(scope: str, session_id: str | None) -> dict[str, str]:
        identity = scope
        if session_id:
            for marker in (":session:", ":isolated:"):
                suffix = marker + session_id
                if identity.endswith(suffix):
                    identity = identity[:-len(suffix)]
                    break
        prefix = "anamnesis:profile:"
        parts = identity[len(prefix):].split(":", 1) if identity.startswith(prefix) else []
        return {
            "identity": identity,
            "platform": parts[0] if len(parts) == 2 else "unknown",
            "sender_id": parts[1] if len(parts) == 2 else "",
        }

    async def _backfill_directory(self, connection: Any) -> None:
        async with connection.execute(
            "SELECT profile_scope, source_session_id FROM user_profiles "
            "WHERE identity IS NULL OR identity = '' GROUP BY profile_scope, source_session_id"
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            owner = self._directory_owner(row[0], row[1])
            await connection.execute(
                "UPDATE user_profiles SET identity=?, platform=?, sender_id=? "
                "WHERE profile_scope=? AND (identity IS NULL OR identity='')",
                (owner["identity"], owner["platform"], owner["sender_id"], row[0]),
            )
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_profiles_identity "
            "ON user_profiles(identity, updated_at DESC)"
        )

    async def list_users_for_web(
        self, *, query: str = "", source_session_id: str | None = None,
        limit: int = 30, offset: int = 0,
    ) -> dict[str, Any]:
        result = {"users": [], "total": 0, "limit": limit, "offset": offset,
                  "has_more": False, "profile_enabled": self.enabled,
                  "scope_mode": self.config_manager.get("user_profile.scope_mode", "session")}
        if not await self._ensure_schema() or self.store.connection is None:
            return result
        connection = self.store.connection
        clauses, params = [], []
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(identity LIKE ? ESCAPE '\\' OR sender_id LIKE ? ESCAPE '\\' "
                           "OR source_sender_name LIKE ? ESCAPE '\\' "
                           "OR source_group_name LIKE ? ESCAPE '\\' "
                           "OR context_sender_name LIKE ? ESCAPE '\\' "
                           "OR context_group_name LIKE ? ESCAPE '\\' "
                           "OR identity IN (SELECT identity FROM user_profile_aliases WHERE alias LIKE ? ESCAPE '\\'))")
            params.extend([f"%{escaped}%"] * 7)
        if source_session_id:
            clauses.append("source_session_id = ?")
            params.append(source_session_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        async with connection.execute(
            f"SELECT COUNT(DISTINCT identity) FROM user_profiles{where}", params
        ) as cursor:
            result["total"] = (await cursor.fetchone())[0]
        # Filter identifies users; their counts and details cover all of their scopes.
        async with connection.execute(
            f"""SELECT identity, MAX(platform) AS platform, MAX(sender_id) AS sender_id,
                MAX(updated_at) AS updated_at, COUNT(*) AS fact_count,
                SUM(suppressed=0 AND (expires_at IS NULL OR expires_at>?)) AS active_fact_count,
                SUM(expires_at IS NOT NULL AND expires_at<=?) AS expired_fact_count,
                SUM(suppressed<>0) AS suppressed_fact_count,
                COUNT(DISTINCT profile_scope) AS scope_count
                FROM user_profiles WHERE identity IN
                    (SELECT DISTINCT identity FROM user_profiles{where})
                GROUP BY identity ORDER BY updated_at DESC, identity LIMIT ? OFFSET ?""",
            [time.time(), time.time(), *params, limit, offset],
        ) as cursor:
            users = [dict(row) for row in await cursor.fetchall()]
        for user in users:
            async with connection.execute(
                "SELECT COALESCE(context_sender_name, source_sender_name), "
                "COALESCE(context_session_id, source_session_id) FROM user_profiles "
                "WHERE identity=? ORDER BY COALESCE(context_updated_at, updated_at) DESC, "
                "updated_at DESC LIMIT 1", (user["identity"],),
            ) as cursor:
                latest = await cursor.fetchone()
            async with connection.execute(
                "SELECT CASE WHEN context_group_id IS NOT NULL THEN context_group_name "
                "ELSE COALESCE(context_group_name, source_group_name) END, "
                "COALESCE(context_group_id, source_group_id) FROM user_profiles "
                "WHERE identity=? AND (COALESCE(context_group_name, source_group_name) IS NOT NULL "
                "OR COALESCE(context_group_id, source_group_id) IS NOT NULL) "
                "ORDER BY COALESCE(context_group_updated_at, context_updated_at, updated_at) DESC, "
                "updated_at DESC LIMIT 1",
                (user["identity"],),
            ) as cursor:
                group = await cursor.fetchone()
            user.update(sender_name=latest[0] if latest else None,
                        last_session_id=latest[1] if latest else None,
                        last_group_name=group[0] if group else None,
                        last_group_id=group[1] if group else None)
            async with connection.execute("SELECT alias FROM user_profile_aliases WHERE identity=?", (user["identity"],)) as cursor:
                alias = await cursor.fetchone()
            async with connection.execute(
                "SELECT COUNT(*) FROM user_profile_claims c JOIN user_profiles p "
                "ON p.profile_scope=c.profile_scope AND p.profile_key=c.profile_key "
                "WHERE p.identity=? AND c.state='pending'", (user["identity"],)) as cursor:
                pending = (await cursor.fetchone())[0]
            async with connection.execute(
                "SELECT DISTINCT e.source_context FROM user_profile_evidence e JOIN user_profile_claims c "
                "ON c.claim_id=e.claim_id JOIN user_profiles p ON p.profile_scope=c.profile_scope AND p.profile_key=c.profile_key "
                "WHERE p.identity=? ORDER BY e.created_at DESC LIMIT 30", (user["identity"],)) as cursor:
                contexts = await cursor.fetchall()
            names = {name for context in contexts if (name := json.loads(context[0]).get("source_sender_name"))}
            user.update(manual_alias=alias[0] if alias else None, pending_count=pending, observed_names=sorted(names))
        result.update(users=users, has_more=offset + len(users) < result["total"])
        return result

    async def set_source_suppressed(self, memory_ids: list[int], suppressed: bool) -> int:
        """Tombstone facts without overwriting later corrections or deleting history."""
        ids = sorted({int(value) for value in memory_ids if int(value) > 0})
        if not ids or not await self._ensure_schema() or self.store.connection is None:
            return 0
        connection = self.store.connection
        async with self.store._write_lock:
            try:
                count = await self._change_ledger_sources(connection, ids, suppressed=suppressed)
                await connection.commit()
                return count
            except BaseException:
                await connection.rollback()
                raise

    async def reassign_source_memory(self, old_id: int, new_id: int) -> None:
        if not await self._ensure_schema() or self.store.connection is None:
            return
        async with self.store._write_lock:
            try:
                await self._change_ledger_sources(self.store.connection, [old_id], new_id=new_id)
                await self.store.connection.commit()
            except BaseException:
                await self.store.connection.rollback()
                raise
