"""Split legacy user profiles by the chat attached to each piece of evidence."""

from __future__ import annotations

import json
from typing import Any


class UserProfileMigrationMixin:
    async def _migrate_legacy_scope_for_event(self, event: Any, scope: str) -> None:
        session = str(getattr(event, "unified_msg_origin", "") or "")
        suffix = f":session:{session}"
        if not session or not scope.endswith(suffix) or self.store.connection is None:
            return
        legacy = scope[: -len(suffix)]
        connection = self.store.connection
        previous_scopes = (legacy, f"{legacy}:isolated:{session}")
        # Include pending/history-only chat support even when the current
        # statement belongs elsewhere or its source projection is cleared.
        async with connection.execute(
            "SELECT 1 FROM user_profiles p JOIN user_profile_claims c "
            "ON c.profile_scope=p.profile_scope AND c.profile_key=p.profile_key "
            "JOIN user_profile_evidence e ON e.claim_id=c.claim_id "
            "WHERE p.profile_scope IN (?,?) AND json_extract(e.source_context,'$.source_session_id')=? LIMIT 1",
            (*previous_scopes, session),
        ) as cursor:
            if not await cursor.fetchone():
                return
        async with self.store._write_lock:
            try:
                # Refresh under the writer lock; another event may already
                # have moved this session's evidence while admission awaited.
                for previous in previous_scopes:
                    async with connection.execute(
                        "SELECT * FROM user_profiles WHERE profile_scope=?", (previous,)
                    ) as cursor:
                        rows = [dict(row) for row in await cursor.fetchall()]
                    for row in rows:
                        await self._split_profile_session(
                            connection, row, scope, session
                        )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def _session_claim_snapshot(self, row, claim, scope, evidence, context):
        snapshot = {**row, **json.loads(claim["snapshot"]), "profile_scope": scope}
        live = await self._live_evidence(evidence)
        snapshot.update(self._profile_source_projection(live), suppressed=int(not live))
        # Recent account labels in the old user scope may come from another
        # chat. Only this session's observations become its directory context.
        source = max(context, key=lambda item: item["created_at"])
        for field in ("session_id", "group_id", "group_name", "sender_name"):
            snapshot[f"context_{field}"] = source.get(f"source_{field}")
        snapshot["context_updated_at"] = source["created_at"]
        snapshot["context_group_updated_at"] = (
            source["created_at"] if source.get("source_group_id") else None
        )
        return snapshot

    async def _split_profile_session(self, connection, row, scope, session):
        previous, key = row["profile_scope"], row["profile_key"]
        async with connection.execute(
            "SELECT * FROM user_profile_claims WHERE profile_scope=? AND profile_key=? ORDER BY claim_id",
            (previous, key),
        ) as cursor:
            claims = [dict(item) for item in await cursor.fetchall()]
        selected = {}
        for claim in claims:
            evidence = await self._claim_evidence(connection, claim["claim_id"])
            belonging = [
                item for item in evidence if item.get("source_session_id") == session
            ]
            if belonging:
                selected[claim["claim_id"]] = belonging
        if not selected:
            return
        context = [item for evidence in selected.values() for item in evidence]
        async with connection.execute(
            "SELECT * FROM user_profiles WHERE profile_scope=? AND profile_key=?",
            (scope, key),
        ) as cursor:
            target = await cursor.fetchone()
        mapping, snapshots = {}, {}
        for claim in claims:
            old_id = claim["claim_id"]
            if old_id not in selected:
                continue
            snapshot = await self._session_claim_snapshot(
                row, claim, scope, selected[old_id], context
            )
            state = (
                "superseded"
                if target and old_id == row["current_claim_id"]
                else claim["state"]
            )
            authority = claim["authority"]
            if (
                old_id == row["current_claim_id"]
                and row["manual_override"]
                and not any(
                    item["source_memory_id"] is None for item in selected[old_id]
                )
            ):
                # Independent administrator support belongs to its recorded
                # chat. Automatic support elsewhere cannot carry that grant.
                authority = "direct_user"
                snapshot.update(authority=authority, manual_override=0)
            snapshot["authority"] = authority
            copied = await self._insert_claim(connection, snapshot, authority, state)
            # A migration preserves the original history timeline and evidence
            # IDs. It neither renews volatile claims nor spends a source budget.
            await connection.execute(
                "UPDATE user_profile_claims SET created_at=?,updated_at=? WHERE claim_id=?",
                (claim["created_at"], claim["updated_at"], copied),
            )
            await connection.executemany(
                "UPDATE user_profile_evidence SET claim_id=? WHERE evidence_id=?",
                [(copied, item["evidence_id"]) for item in selected[old_id]],
            )
            mapping[old_id], snapshots[old_id] = copied, snapshot
        if not target:
            old_current = row["current_claim_id"]
            if old_current in mapping:
                current = mapping[old_current]
                snapshot = snapshots[old_current]
                authority = snapshot.get("authority", row["authority"])
                manual = bool(row["manual_override"]) and any(
                    item["source_memory_id"] is None for item in selected[old_current]
                )
            else:
                # Expose this chat's pending/history without injecting a
                # current value whose evidence belongs to another chat.
                base = next(
                    claim for claim in claims if claim["claim_id"] == old_current
                )
                snapshot = await self._session_claim_snapshot(
                    row, base, scope, [], context
                )
                snapshot.update(value="[来源已删除]", expires_at=None)
                current = await self._insert_claim(
                    connection, snapshot, "legacy", "withdrawn"
                )
                authority, manual = "legacy", False
            await self._project_claim(
                connection,
                snapshot,
                current,
                authority,
                revision=row["revision"] + 1,
                manual=manual,
            )
        else:
            # Existing chat-scoped values take precedence; migrated statements
            # remain reviewable/history, never overwrite that current value.
            await connection.execute(
                "UPDATE user_profiles SET revision=revision+1 WHERE profile_scope=? AND profile_key=?",
                (scope, key),
            )
        await self._split_profile_transitions(connection, previous, scope, key, mapping)
        await self._finish_profile_split(connection, row)
        await self._prune_ledger(connection, scope, key)

    async def _split_profile_transitions(
        self, connection, previous, scope, key, mapping
    ):
        async with connection.execute(
            "SELECT * FROM user_profile_transitions WHERE profile_scope=? AND profile_key=? ORDER BY transition_id",
            (previous, key),
        ) as cursor:
            transitions = await cursor.fetchall()
        for item in transitions:
            old, new = (
                mapping.get(item["old_claim_id"]),
                mapping.get(item["new_claim_id"]),
            )
            if old is not None or new is not None:
                await connection.execute(
                    "INSERT INTO user_profile_transitions(profile_scope,profile_key,action,old_claim_id,new_claim_id,actor,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        scope,
                        key,
                        item["action"],
                        old,
                        new,
                        item["actor"],
                        item["created_at"],
                    ),
                )

    async def _finish_profile_split(self, connection, row):
        scope, key, current = (
            row["profile_scope"],
            row["profile_key"],
            row["current_claim_id"],
        )
        async with connection.execute(
            "SELECT 1 FROM user_profile_evidence e JOIN user_profile_claims c ON c.claim_id=e.claim_id "
            "WHERE c.profile_scope=? AND c.profile_key=? LIMIT 1",
            (scope, key),
        ) as cursor:
            remaining = await cursor.fetchone()
        if not remaining:
            await self._delete_ledger_keys(connection, scope, key)
            await connection.execute(
                "DELETE FROM user_profiles WHERE profile_scope=? AND profile_key=?",
                (scope, key),
            )
            return
        evidence = await self._claim_evidence(connection, current)
        if not evidence:
            await connection.execute(
                "UPDATE user_profiles SET value='[来源已删除]',manual_override=0 WHERE profile_scope=? AND profile_key=?",
                (scope, key),
            )
            await connection.execute(
                "UPDATE user_profile_claims SET value='[来源已删除]',snapshot='{}',state='withdrawn' WHERE claim_id=?",
                (current,),
            )
        elif row["manual_override"] and not any(
            item["source_memory_id"] is None for item in evidence
        ):
            await connection.execute(
                "UPDATE user_profiles SET manual_override=0,authority='direct_user' WHERE profile_scope=? AND profile_key=?",
                (scope, key),
            )
            await connection.execute(
                "UPDATE user_profile_claims SET authority='direct_user',"
                "snapshot=json_set(snapshot,'$.manual_override',0,'$.authority','direct_user') WHERE claim_id=?",
                (current,),
            )
        await self._project_profile_sources(
            connection, scope, key, await self._live_evidence(evidence)
        )
        async with connection.execute(
            "SELECT claim_id FROM user_profile_claims c WHERE profile_scope=? AND profile_key=? AND claim_id<>? "
            "AND NOT EXISTS(SELECT 1 FROM user_profile_evidence e WHERE e.claim_id=c.claim_id)",
            (scope, key, current),
        ) as cursor:
            emptied = [item[0] for item in await cursor.fetchall()]
        for claim_id in emptied:
            await connection.execute(
                "DELETE FROM user_profile_claims WHERE claim_id=?", (claim_id,)
            )
            await connection.execute(
                "UPDATE user_profile_transitions SET old_claim_id=NULL WHERE profile_scope=? AND old_claim_id=?",
                (scope, claim_id),
            )
            await connection.execute(
                "UPDATE user_profile_transitions SET new_claim_id=NULL WHERE profile_scope=? AND new_claim_id=?",
                (scope, claim_id),
            )
        await self._prune_ledger(connection, scope, key)
