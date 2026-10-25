"""Versioned profile claims and evidence, stored with their current projection.

Design inspired by Memorix's claim/evidence/transition separation (AGPL-3.0).
The implementation uses Anamnesis' asynchronous connection, identity boundaries
and original-quote extraction; it does not import the upstream runtime.
"""

from __future__ import annotations

import json
import time
from typing import Any

_PROFILE_SOURCE_KEYS = (
    "source_memory_id", "source_session_id", "source_group_id",
    "source_group_name", "source_sender_name",
)


class UserProfileLedgerMixin:
    def _ledger_limit(self, name: str, default: int, maximum: int) -> int:
        try:
            return max(1, min(maximum, int(self.config_manager.get(f"user_profile.{name}", default))))
        except (TypeError, ValueError):
            return default

    async def _create_ledger(self, connection: Any) -> None:
        for sql in (
            """CREATE TABLE IF NOT EXISTS user_profile_claims (
                claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_scope TEXT NOT NULL, profile_key TEXT NOT NULL,
                value TEXT NOT NULL, authority TEXT NOT NULL, state TEXT NOT NULL,
                snapshot TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS user_profile_evidence (
                evidence_id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id INTEGER NOT NULL,
                source_memory_id INTEGER, quote TEXT NOT NULL DEFAULT '',
                source_context TEXT NOT NULL DEFAULT '{}', suppressed INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                FOREIGN KEY(claim_id) REFERENCES user_profile_claims(claim_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS user_profile_transitions (
                transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_scope TEXT NOT NULL, profile_key TEXT NOT NULL,
                action TEXT NOT NULL, old_claim_id INTEGER, new_claim_id INTEGER,
                actor TEXT NOT NULL, created_at REAL NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_profile_claim_owner ON user_profile_claims(profile_scope,profile_key,state)",
            "CREATE INDEX IF NOT EXISTS idx_profile_evidence_source ON user_profile_evidence(source_memory_id,claim_id)",
            "CREATE INDEX IF NOT EXISTS idx_profile_evidence_claim ON user_profile_evidence(claim_id,evidence_id)",
            """CREATE TABLE IF NOT EXISTS user_profile_aliases (
                identity TEXT PRIMARY KEY, alias TEXT NOT NULL, updated_at REAL NOT NULL)""",
        ):
            await connection.execute(sql)
        async with connection.execute("PRAGMA table_info(user_profiles)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        for column, definition in (("current_claim_id", "INTEGER"),
                                   ("manual_override", "INTEGER NOT NULL DEFAULT 0"),
                                   ("revision", "INTEGER NOT NULL DEFAULT 1"),
                                   ("authority", "TEXT NOT NULL DEFAULT 'legacy'")):
            if column not in columns:
                await connection.execute(f"ALTER TABLE user_profiles ADD COLUMN {column} {definition}")
        # Incremental backfill also handles a legacy scope migration performed
        # by another manager. Old rows retain 'legacy', never 'manual'.
        async with connection.execute("SELECT * FROM user_profiles WHERE current_claim_id IS NULL") as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            snapshot = dict(row)
            claim = await self._insert_claim(connection, snapshot, "legacy", "current")
            await self._add_evidence(connection, claim, snapshot, "")
            await connection.execute("UPDATE user_profiles SET current_claim_id=? WHERE profile_scope=? AND profile_key=?",
                                     (claim, row["profile_scope"], row["profile_key"]))

    async def _insert_claim(self, connection: Any, snapshot: dict[str, Any], authority: str, state: str) -> int:
        now = time.time()
        cursor = await connection.execute(
            "INSERT INTO user_profile_claims(profile_scope,profile_key,value,authority,state,snapshot,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (snapshot["profile_scope"], snapshot["profile_key"], snapshot["value"], authority, state,
             json.dumps(snapshot, ensure_ascii=False), now, now))
        return int(cursor.lastrowid)

    async def _add_evidence(self, connection: Any, claim: int, snapshot: dict[str, Any], quote: str) -> bool:
        source_id = snapshot.get("source_memory_id")
        context = {key: snapshot.get(key) for key in ("source_session_id", "source_group_id", "source_group_name", "source_sender_name")}
        async with connection.execute(
            "SELECT 1 FROM user_profile_evidence WHERE claim_id=? AND source_memory_id IS ? AND quote=? "
            "AND (? IS NOT NULL OR json_extract(source_context,'$.source_session_id') IS ?) LIMIT 1",
            (claim, source_id, quote, source_id, context.get("source_session_id"))) as cursor:
            if await cursor.fetchone():
                return False
        # Once the source budget is exhausted keep older support. Dropping it
        # would make a later source deletion incorrectly retract the fact.
        async with connection.execute("SELECT COUNT(*) FROM user_profile_evidence WHERE claim_id=?", (claim,)) as cursor:
            if (await cursor.fetchone())[0] >= self._ledger_limit("evidence_max_sources", 20, 100):
                return False
        await connection.execute(
            "INSERT INTO user_profile_evidence(claim_id,source_memory_id,quote,source_context,suppressed,created_at) VALUES(?,?,?,?,?,?)",
            (claim, source_id, quote, json.dumps(context, ensure_ascii=False), int(snapshot.get("suppressed", 0)), time.time()))
        return True

    async def _transition(self, connection: Any, scope: str, key: str, action: str,
                          old_claim: int | None, new_claim: int | None, actor: str) -> None:
        await connection.execute(
            "INSERT INTO user_profile_transitions(profile_scope,profile_key,action,old_claim_id,new_claim_id,actor,created_at) VALUES(?,?,?,?,?,?,?)",
            (scope, key, action, old_claim, new_claim, actor, time.time()))

    async def _prune_ledger(self, connection: Any, scope: str, key: str) -> None:
        limit = self._ledger_limit("history_max_versions", 20, 100)
        async with connection.execute(
            "SELECT claim_id FROM user_profile_claims WHERE profile_scope=? AND profile_key=? "
            "AND state NOT IN ('current','pending') "
            "AND claim_id NOT IN (SELECT current_claim_id FROM user_profiles WHERE current_claim_id IS NOT NULL) "
            "ORDER BY claim_id DESC LIMIT -1 OFFSET ?",
            (scope, key, limit)) as cursor:
            expired = [row[0] for row in await cursor.fetchall()]
        # Keep at most three alternatives per attribute; repeated extraction
        # of one proposal adds evidence rather than a new alternative.
        async with connection.execute(
            "SELECT claim_id FROM user_profile_claims WHERE profile_scope=? AND profile_key=? "
            "AND state='pending' "
            "AND claim_id NOT IN (SELECT current_claim_id FROM user_profiles WHERE current_claim_id IS NOT NULL) "
            "ORDER BY claim_id DESC LIMIT -1 OFFSET 3", (scope, key)) as cursor:
            expired += [row[0] for row in await cursor.fetchall()]
        for claim_id in expired:
            await connection.execute("DELETE FROM user_profile_evidence WHERE claim_id=?", (claim_id,))
            await connection.execute("DELETE FROM user_profile_claims WHERE claim_id=?", (claim_id,))
        await connection.execute(
            "DELETE FROM user_profile_transitions WHERE profile_scope=? AND profile_key=? AND transition_id NOT IN "
            "(SELECT transition_id FROM user_profile_transitions WHERE profile_scope=? AND profile_key=? ORDER BY transition_id DESC LIMIT ?)",
            (scope, key, scope, key, limit * 3))

    async def _project_claim(self, connection: Any, snapshot: dict[str, Any], claim: int,
                             authority: str, *, revision: int = 1, manual: bool = False) -> None:
        row = {**snapshot, "current_claim_id": claim, "authority": authority,
               "manual_override": int(manual), "revision": revision}
        # snapshot keys originate in our persisted row or normalized writer.
        async with connection.execute("PRAGMA table_info(user_profiles)") as cursor:
            columns = {record[1] for record in await cursor.fetchall()}
        row = {key: value for key, value in row.items() if key in columns}
        keys = list(row)
        assignments = ",".join(f"{key}=excluded.{key}" for key in keys if key not in ("profile_scope", "profile_key"))
        await connection.execute(
            f"INSERT INTO user_profiles({','.join(keys)}) VALUES({','.join('?' for _ in keys)}) "
            f"ON CONFLICT(profile_scope,profile_key) DO UPDATE SET {assignments}", list(row.values()))

    async def _upsert_ledger_fact(self, connection: Any, snapshot: dict[str, Any], quote: str) -> None:
        scope, key = snapshot["profile_scope"], snapshot["profile_key"]
        async with connection.execute("SELECT * FROM user_profiles WHERE profile_scope=? AND profile_key=?", (scope, key)) as cursor:
            record = await cursor.fetchone()
        current = dict(record) if record else None
        if current and current["value"] == snapshot["value"]:
            claim = current["current_claim_id"]
            changed = await self._add_evidence(connection, claim, snapshot, quote)
            if not changed:
                return  # A replay must not renew TTL or invalidate review tokens.
            if not current["manual_override"]:
                snapshot["confidence"] = max(snapshot["confidence"], current["confidence"])
                await self._project_claim(connection, snapshot, claim, current["authority"], revision=current["revision"] + 1)
                await connection.execute("UPDATE user_profile_claims SET snapshot=?,updated_at=? WHERE claim_id=?",
                                         (json.dumps(snapshot, ensure_ascii=False), time.time(), claim))
            else:
                await connection.execute("UPDATE user_profiles SET revision=revision+1 WHERE profile_scope=? AND profile_key=?", (scope, key))
            return
        review = current and (current["manual_override"] or self.config_manager.get("user_profile.review_changes", True))
        if review:
            async with connection.execute(
                "SELECT 1 FROM user_profile_claims c JOIN user_profile_evidence e ON e.claim_id=c.claim_id "
                "WHERE c.profile_scope=? AND c.profile_key=? AND c.value=? AND c.state='rejected' "
                "AND e.source_memory_id IS ? AND e.quote=? "
                "AND (? IS NOT NULL OR json_extract(e.source_context,'$.source_session_id') IS ?) LIMIT 1",
                (scope, key, snapshot["value"], snapshot.get("source_memory_id"), quote,
                 snapshot.get("source_memory_id"), snapshot.get("source_session_id"))) as cursor:
                if await cursor.fetchone():
                    return  # Do not repeatedly ask about the exact rejected evidence.
            async with connection.execute(
                "SELECT claim_id,authority FROM user_profile_claims WHERE profile_scope=? AND profile_key=? AND value=? AND state='pending' ORDER BY claim_id DESC LIMIT 1",
                (scope, key, snapshot["value"])) as cursor:
                candidate = await cursor.fetchone()
            claim = candidate[0] if candidate else await self._insert_claim(connection, snapshot, "direct_user", "pending")
            changed = await self._add_evidence(connection, claim, snapshot, quote)
            if candidate and changed and candidate["authority"] != "manual":
                # A new observation may renew a volatile proposal. Replaying
                # the same source/quote leaves both expiry and revision intact.
                # Independent administrator choices keep their saved facts
                # and expiry; new automatic support only joins their evidence.
                await connection.execute("UPDATE user_profile_claims SET snapshot=?,updated_at=? WHERE claim_id=?",
                                         (json.dumps(snapshot, ensure_ascii=False), time.time(), claim))
            if not candidate or changed:
                await connection.execute("UPDATE user_profiles SET revision=revision+1 WHERE profile_scope=? AND profile_key=?", (scope, key))
            if not candidate:
                await self._transition(connection, scope, key, "proposed", current["current_claim_id"], claim, "automatic")
        else:
            claim = await self._insert_claim(connection, snapshot, "direct_user", "current")
            await self._add_evidence(connection, claim, snapshot, quote)
            if current:
                await connection.execute("UPDATE user_profile_claims SET state='superseded',updated_at=? WHERE claim_id=?",
                                         (time.time(), current["current_claim_id"]))
            await self._project_claim(connection, snapshot, claim, "direct_user", revision=current["revision"] + 1 if current else 1)
            await self._transition(connection, scope, key, "updated" if current else "created",
                                   current["current_claim_id"] if current else None, claim, "automatic")
        await self._prune_ledger(connection, scope, key)

    async def _manual_ledger_update(self, connection: Any, before: dict[str, Any], after: dict[str, Any],
                                    supporting_evidence: list[dict[str, Any]] | None = None) -> None:
        scope, key = before["profile_scope"], before["profile_key"]
        manual = bool(self.config_manager.get("user_profile.manual_override_enabled", True)) or bool(before["manual_override"])
        inherit_evidence = not manual and supporting_evidence is None and before["value"] == after["value"]
        if inherit_evidence:
            supporting_evidence = await self._claim_evidence(connection, before["current_claim_id"])
            live = await self._live_evidence(supporting_evidence)
            after.update(self._profile_source_projection(live))
            after["suppressed"] = int(not live)
        elif supporting_evidence is not None:
            # Account/context columns stay current, but provenance follows the
            # accepted statement rather than the statement it replaces.
            after.update(self._profile_source_projection(supporting_evidence))
            after["suppressed"] = int(not supporting_evidence)
        claim = await self._insert_claim(connection, after, "manual", "current")
        # Administrator confirmation is independent support. Historical chat
        # evidence remains with the previous claim, rather than being relabelled.
        if manual:
            independent = {**after, "source_memory_id": None, "suppressed": 0}
            await self._add_evidence(connection, claim, independent, "")
            after["suppressed"] = 0
        elif inherit_evidence:
            # A label/category edit keeps existing support, including its age,
            # ordering and suppression, even after the source budget is lowered.
            await connection.execute(
                "INSERT INTO user_profile_evidence(claim_id,source_memory_id,quote,source_context,suppressed,created_at) "
                "SELECT ?,source_memory_id,quote,source_context,suppressed,created_at "
                "FROM user_profile_evidence WHERE claim_id=? ORDER BY evidence_id",
                (claim, before["current_claim_id"]))
        elif supporting_evidence is not None:
            # These are existing, verified sources of the accepted proposal,
            # not new observations subject to a newly lowered source budget.
            await connection.executemany(
                "INSERT INTO user_profile_evidence(claim_id,source_memory_id,quote,source_context,suppressed,created_at) "
                "VALUES(?,?,?,?,?,?)",
                [(claim, evidence["source_memory_id"], evidence["quote"],
                  json.dumps({key: evidence.get(key) for key in _PROFILE_SOURCE_KEYS if key != "source_memory_id"}, ensure_ascii=False),
                  evidence["suppressed"], evidence["created_at"]) for evidence in reversed(supporting_evidence)])
        else:
            await self._add_evidence(connection, claim, after, "")
        await connection.execute("UPDATE user_profile_claims SET state='superseded',updated_at=? WHERE claim_id=?",
                                 (time.time(), before["current_claim_id"]))
        await self._project_claim(connection, after, claim, "manual", revision=before["revision"] + 1, manual=manual)
        await self._transition(connection, scope, key, "manual_edit", before["current_claim_id"], claim, "administrator")
        await self._prune_ledger(connection, scope, key)

    @staticmethod
    def _profile_source_projection(evidence: list[dict[str, Any]]) -> dict[str, Any]:
        source = next((item for item in evidence if item["source_memory_id"] is not None),
                      evidence[0] if evidence else {})
        return {key: source.get(key) for key in _PROFILE_SOURCE_KEYS}

    async def _project_profile_sources(self, connection: Any, scope: str, key: str,
                                       live: list[dict[str, Any]]) -> None:
        source = self._profile_source_projection(live)
        await connection.execute(
            "UPDATE user_profiles SET suppressed=?,source_memory_id=?,source_session_id=?,source_group_id=?,"
            "source_group_name=?,source_sender_name=?,revision=revision+1 WHERE profile_scope=? AND profile_key=?",
            (int(not live), *(source[name] for name in _PROFILE_SOURCE_KEYS), scope, key))

    async def _live_evidence(self, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = [item for item in evidence if not item["suppressed"]]
        connection = getattr(getattr(self, "memory_engine", None), "db_connection", None)
        source_ids = {item["source_memory_id"] for item in candidates if item["source_memory_id"] is not None}
        if connection is None or not source_ids:
            return candidates
        active = set()
        source_ids = list(source_ids)
        for start in range(0, len(source_ids), 500):
            batch = source_ids[start:start + 500]
            async with connection.execute(
                "SELECT id FROM documents WHERE id IN (" + ",".join("?" for _ in batch) + ") "
                "AND json_valid(metadata) AND COALESCE(json_extract(metadata,'$.status'),'active')='active'", batch) as cursor:
                active.update(row[0] for row in await cursor.fetchall())
        return [item for item in candidates if item["source_memory_id"] is None or item["source_memory_id"] in active]

    async def _claim_evidence(self, connection: Any, claim: int) -> list[dict[str, Any]]:
        async with connection.execute("SELECT * FROM user_profile_evidence WHERE claim_id=? ORDER BY evidence_id DESC", (claim,)) as cursor:
            result = [dict(row) for row in await cursor.fetchall()]
        for item in result:
            item.update(json.loads(item.pop("source_context")))
        return result

    async def _enrich_profile_rows(self, rows: list[dict[str, Any]], *, include_history: bool = True) -> list[dict[str, Any]]:
        connection = self.store.connection
        current_evidence = {}
        claim_ids = [row["current_claim_id"] for row in rows if row.get("current_claim_id")]
        all_evidence = []
        for start in range(0, len(claim_ids), 500):
            batch = claim_ids[start:start + 500]
            async with connection.execute("SELECT * FROM user_profile_evidence WHERE claim_id IN (" + ",".join("?" for _ in batch) + ") ORDER BY evidence_id DESC", batch) as cursor:
                all_evidence.extend(dict(record) for record in await cursor.fetchall())
        live_ids = {item["evidence_id"] for item in await self._live_evidence(all_evidence)}
        for item in all_evidence:
            item.update(json.loads(item.pop("source_context")))
            item["active"] = item["evidence_id"] in live_ids
            current_evidence.setdefault(item["claim_id"], []).append(item)
        for row in rows:
            claim = row.get("current_claim_id")
            if not claim:
                row.update(evidence=[], pending=[], history=[])
                continue
            row["evidence"] = current_evidence.get(claim, [])
            live = [item for item in row["evidence"] if item["active"]]
            # Source replacement/deletion can bypass the profile projection
            # hook. Display only provenance that this read verified as live.
            row.update(self._profile_source_projection(live))
            row["live_evidence_count"] = len(live)
            row["suppressed"] = int(not live)
            row["pending"], row["history"] = [], []
            if not include_history:
                continue
            async with connection.execute(
                "SELECT claim_id,value,authority,state,created_at,updated_at,snapshot FROM user_profile_claims "
                "WHERE profile_scope=? AND profile_key=? AND claim_id<>? ORDER BY claim_id DESC LIMIT ?",
                (row["profile_scope"], row["profile_key"], claim, self._ledger_limit("history_max_versions", 20, 100) + 3)) as cursor:
                claims = [dict(record) for record in await cursor.fetchall()]
            for item in claims:
                snapshot = json.loads(item.pop("snapshot"))
                item["expires_at"] = snapshot.get("expires_at")
                item["evidence"] = await self._claim_evidence(connection, item["claim_id"])
                supported_ids = {record["evidence_id"] for record in await self._live_evidence(item["evidence"])}
                for evidence in item["evidence"]:
                    evidence["active"] = evidence["evidence_id"] in supported_ids
                if item["state"] == "pending":
                    item["can_accept"] = bool(supported_ids) and (not item["expires_at"] or item["expires_at"] > time.time())
                    row["pending"].append(item)
                else:
                    row["history"].append(item)
        return rows

    async def review_for_web(self, profile_scope: str, profile_key: str, *, claim_id: int | None = None,
                             action: str, expected_revision: int) -> dict[str, Any] | None:
        """Accept/reject a proposal or unlock an exact administrator-selected row.

        An obsolete revision refuses the action. A Boolean supplied by the LLM
        never enters this API; authorization is the administrator Web handler.
        """
        if action not in {"accept", "reject", "unlock"} or isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            return None
        if not await self._ensure_schema() or self.store.connection is None:
            return None
        connection = self.store.connection
        async with self.store._write_lock:
            try:
                async with connection.execute("SELECT * FROM user_profiles WHERE profile_scope=? AND profile_key=?", (profile_scope, profile_key)) as cursor:
                    record = await cursor.fetchone()
                if not record or record["revision"] != expected_revision:
                    return None
                current = dict(record)
                if action == "unlock":
                    await connection.execute("UPDATE user_profiles SET manual_override=0,revision=revision+1 WHERE profile_scope=? AND profile_key=?", (profile_scope, profile_key))
                    await self._transition(connection, profile_scope, profile_key, "unlocked", current["current_claim_id"], current["current_claim_id"], "administrator")
                else:
                    if isinstance(claim_id, bool) or not isinstance(claim_id, int):
                        return None
                    async with connection.execute("SELECT * FROM user_profile_claims WHERE claim_id=? AND profile_scope=? AND profile_key=? AND state='pending'", (claim_id, profile_scope, profile_key)) as cursor:
                        candidate = await cursor.fetchone()
                    if not candidate:
                        return None
                    if action == "reject":
                        await connection.execute("UPDATE user_profile_claims SET state='rejected',updated_at=? WHERE claim_id=?", (time.time(), claim_id))
                        await connection.execute("UPDATE user_profiles SET revision=revision+1 WHERE profile_scope=? AND profile_key=?", (profile_scope, profile_key))
                    else:
                        snapshot = json.loads(candidate["snapshot"])
                        evidence = await self._live_evidence(await self._claim_evidence(connection, claim_id))
                        if (snapshot.get("expires_at") and snapshot["expires_at"] <= time.time()) or not evidence:
                            return None
                        # Keep current account directory metadata; a proposal
                        # from old traffic must not replace the recent nickname.
                        after = {**current, **{key: snapshot[key] for key in ("value", "category", "confidence", "expires_at", "display_label") if key in snapshot}, "updated_at": time.time()}
                        await self._manual_ledger_update(connection, current, after, evidence)
                        await connection.execute("UPDATE user_profile_claims SET state='accepted',updated_at=? WHERE claim_id=?", (time.time(), claim_id))
                    await self._transition(connection, profile_scope, profile_key, action, current["current_claim_id"], claim_id, "administrator")
                    # Accepted/rejected proposals enter history only after
                    # their final state is saved; enforce its budget here.
                    await self._prune_ledger(connection, profile_scope, profile_key)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        async with connection.execute("SELECT * FROM user_profiles WHERE profile_scope=? AND profile_key=?", (profile_scope, profile_key)) as cursor:
            row = await cursor.fetchone()
        return (await self._enrich_profile_rows([dict(row)]))[0] if row else None

    async def _delete_ledger_keys(self, connection: Any, scope: str, key: str | None = None) -> None:
        where, parameters = "profile_scope=?", [scope]
        if key:
            where += " AND profile_key=?"
            parameters.append(key)
        await connection.execute(f"DELETE FROM user_profile_evidence WHERE claim_id IN (SELECT claim_id FROM user_profile_claims WHERE {where})", parameters)
        await connection.execute(f"DELETE FROM user_profile_claims WHERE {where}", parameters)
        await connection.execute(f"DELETE FROM user_profile_transitions WHERE {where}", parameters)

    async def _change_ledger_sources(self, connection: Any, ids: list[int], *, suppressed: bool | None = None,
                                     delete: bool = False, new_id: int | None = None) -> int:
        affected = set()
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            marks = ",".join("?" for _ in batch)
            async with connection.execute(
                "SELECT DISTINCT c.profile_scope,c.profile_key FROM user_profile_claims c JOIN user_profile_evidence e ON c.claim_id=e.claim_id "
                f"WHERE e.source_memory_id IN ({marks})", batch) as cursor:
                affected.update(tuple(row) for row in await cursor.fetchall())
            if delete:
                await connection.execute(f"DELETE FROM user_profile_evidence WHERE source_memory_id IN ({marks})", batch)
            elif new_id is not None:
                await connection.execute(f"UPDATE user_profile_evidence SET source_memory_id=? WHERE source_memory_id IN ({marks})", [new_id, *batch])
            else:
                await connection.execute(f"UPDATE user_profile_evidence SET suppressed=? WHERE source_memory_id IN ({marks})", [int(bool(suppressed)), *batch])
        removed = 0
        for scope, key in affected:
            async with connection.execute("SELECT * FROM user_profiles WHERE profile_scope=? AND profile_key=?", (scope, key)) as cursor:
                current = await cursor.fetchone()
            if not current:
                continue
            evidence = await self._claim_evidence(connection, current["current_claim_id"])
            live = [item for item in evidence if not item["suppressed"]]
            if not evidence and delete:
                async with connection.execute(
                    "SELECT 1 FROM user_profile_claims c WHERE c.profile_scope=? AND c.profile_key=? AND c.state='pending' "
                    "AND EXISTS(SELECT 1 FROM user_profile_evidence e WHERE e.claim_id=c.claim_id) LIMIT 1", (scope, key)) as cursor:
                    has_proposal = await cursor.fetchone()
                if has_proposal:
                    # Keep the review entry reachable without keeping the
                    # deleted fact's content or exposing it to injection.
                    await connection.execute("UPDATE user_profiles SET value='[来源已删除]' WHERE profile_scope=? AND profile_key=?", (scope, key))
                    await self._project_profile_sources(connection, scope, key, [])
                else:
                    await connection.execute("DELETE FROM user_profiles WHERE profile_scope=? AND profile_key=?", (scope, key))
                removed += 1
            else:
                await self._project_profile_sources(connection, scope, key, live)
            if delete:
                # An explicit source deletion removes unsupported derived
                # content from history too; manual assertions have independent
                # support and retain their administrator-entered value.
                await connection.execute(
                    "UPDATE user_profile_claims SET value='[来源已删除]',snapshot='{}',state='withdrawn' "
                    "WHERE profile_scope=? AND profile_key=? AND NOT EXISTS "
                    "(SELECT 1 FROM user_profile_evidence e WHERE e.claim_id=user_profile_claims.claim_id)", (scope, key))
            await self._prune_ledger(connection, scope, key)
        return removed if delete else len(affected)

    async def set_alias_for_web(self, identity: str, alias: str) -> dict[str, Any] | None:
        """Give a known account an administrator label without identity merging."""
        if not isinstance(identity, str) or not isinstance(alias, str) or len(alias.strip()) > 80:
            return None
        clean = self._clean_context_value(alias, max_length=80) or ""
        if not await self._ensure_schema() or self.store.connection is None:
            return None
        connection = self.store.connection
        async with self.store._write_lock:
            try:
                async with connection.execute("SELECT 1 FROM user_profiles WHERE identity=? LIMIT 1", (identity,)) as cursor:
                    if not await cursor.fetchone():
                        return None
                if clean:
                    await connection.execute("INSERT INTO user_profile_aliases(identity,alias,updated_at) VALUES(?,?,?) ON CONFLICT(identity) DO UPDATE SET alias=excluded.alias,updated_at=excluded.updated_at",
                                             (identity, clean, time.time()))
                else:
                    await connection.execute("DELETE FROM user_profile_aliases WHERE identity=?", (identity,))
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return {"identity": identity, "alias": clean}
