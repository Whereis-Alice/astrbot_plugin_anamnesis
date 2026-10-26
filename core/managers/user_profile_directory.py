"""Administrator profile directory; display aggregation keeps storage scopes intact."""

from __future__ import annotations

import json
import time
from typing import Any


class UserProfileDirectoryMixin:
    @staticmethod
    def _profile_session_condition(session_id: str) -> tuple[str, list[str]]:
        """A scoped row keeps its chat; shared rows need current/pending evidence."""
        return (
            (
                "(p.profile_scope=p.identity || ':session:' || ? "
                "OR p.profile_scope=p.identity || ':isolated:' || ? "
                "OR (p.profile_scope=p.identity AND EXISTS(SELECT 1 FROM user_profile_claims c "
                "JOIN user_profile_evidence e ON e.claim_id=c.claim_id "
                "WHERE c.profile_scope=p.profile_scope AND c.profile_key=p.profile_key "
                "AND (c.claim_id=p.current_claim_id OR c.state='pending') AND e.suppressed=0 "
                "AND json_extract(e.source_context,'$.source_session_id')=?)))"
            ),
            [session_id, session_id, session_id],
        )

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
            condition, session_params = self._profile_session_condition(source_session_id)
            clauses.append(condition)
            params.extend(session_params)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        async with connection.execute(
            f"SELECT COUNT(DISTINCT identity) FROM user_profiles p{where}", params
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
                    (SELECT DISTINCT identity FROM user_profiles p{where})
                GROUP BY identity ORDER BY updated_at DESC, identity LIMIT ? OFFSET ?""",
            [time.time(), time.time(), *params, limit, offset],
        ) as cursor:
            users = [dict(row) for row in await cursor.fetchall()]
        if users:
            identities = [user["identity"] for user in users]
            async with connection.execute(
                "SELECT * FROM user_profiles WHERE identity IN ("
                + ",".join("?" for _ in identities) + ")",
                identities,
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
            facts = await self._enrich_profile_rows(rows, include_history=False)
            counts = {identity: {"active_fact_count": 0, "expired_fact_count": 0,
                                 "suppressed_fact_count": 0} for identity in identities}
            by_identity = {identity: [] for identity in identities}
            now = time.time()
            for fact in facts:
                by_identity[fact["identity"]].append(fact)
                summary = counts[fact["identity"]]
                expired = fact["expires_at"] is not None and fact["expires_at"] <= now
                summary["active_fact_count"] += int(not fact["suppressed"] and not expired)
                summary["expired_fact_count"] += int(expired)
                summary["suppressed_fact_count"] += int(bool(fact["suppressed"]))
            for user in users:
                user.update(counts[user["identity"]])
            placeholders = ",".join("?" for _ in identities)
            async with connection.execute(
                f"SELECT identity,alias FROM user_profile_aliases WHERE identity IN ({placeholders})", identities
            ) as cursor:
                aliases = dict(await cursor.fetchall())
            async with connection.execute(
                "SELECT p.identity,COUNT(*) FROM user_profile_claims c JOIN user_profiles p "
                "ON p.profile_scope=c.profile_scope AND p.profile_key=c.profile_key "
                f"WHERE p.identity IN ({placeholders}) AND c.state='pending' GROUP BY p.identity", identities,
            ) as cursor:
                pending_counts = dict(await cursor.fetchall())
            async with connection.execute(
                "SELECT identity,source_context FROM (SELECT p.identity,e.source_context,"
                "ROW_NUMBER() OVER(PARTITION BY p.identity ORDER BY MAX(e.created_at) DESC,e.source_context) AS rank "
                "FROM user_profile_evidence e JOIN user_profile_claims c ON c.claim_id=e.claim_id "
                "JOIN user_profiles p ON p.profile_scope=c.profile_scope AND p.profile_key=c.profile_key "
                f"WHERE p.identity IN ({placeholders}) GROUP BY p.identity,e.source_context) WHERE rank<=30", identities,
            ) as cursor:
                contexts = await cursor.fetchall()
            names = {identity: set() for identity in identities}
            for identity, context in contexts:
                name = json.loads(context).get("source_sender_name")
                if name:
                    names[identity].add(name)
            category_order = {name: index for index, name in enumerate(
                ("identity", "preference", "constraint", "task_preference", "status"))}
            for user in users:
                owned = by_identity[user["identity"]]
                latest = max(owned, key=lambda row: (row.get("context_updated_at") or row["updated_at"], row["updated_at"]))
                grouped = [row for row in owned if row.get("context_group_name") or row.get("source_group_name")
                           or row.get("context_group_id") or row.get("source_group_id")]
                group = max(grouped, key=lambda row: (row.get("context_group_updated_at") or row.get("context_updated_at")
                                                     or row["updated_at"], row["updated_at"])) if grouped else {}
                preview, seen = [], set()
                for row in sorted(owned, key=lambda row: (category_order.get(row["category"], 10), -row["updated_at"], row["profile_key"])):
                    if row["suppressed"] or row["expires_at"] is not None and row["expires_at"] <= now:
                        continue
                    signature = (row["category"], row["display_label"], row["value"])
                    if signature in seen:
                        continue
                    seen.add(signature)
                    preview.append({field: row[field] for field in ("profile_key", "display_label", "category", "value")})
                    if len(preview) == 3:
                        break
                user.update(
                    sender_name=latest.get("context_sender_name") or latest.get("source_sender_name"),
                    last_session_id=latest.get("context_session_id") or latest.get("source_session_id"),
                    last_seen_at=latest.get("context_updated_at") or latest["updated_at"],
                    last_group_name=group.get("context_group_name") if group.get("context_group_id") is not None
                    else group.get("context_group_name") or group.get("source_group_name"),
                    last_group_id=group.get("context_group_id") or group.get("source_group_id"),
                    manual_alias=aliases.get(user["identity"]), pending_count=pending_counts.get(user["identity"], 0),
                    observed_names=sorted(names[user["identity"]]), fact_preview=preview,
                )
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
                if suppressed:
                    await self._commit_profile_invalidation(source_ids=set(ids))
                else:
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
