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
            has_legacy = await cursor.fetchone()
        if not has_legacy and not await self._misplaced_profile_rows(
            connection, legacy
        ):
            return
        async with self.store._write_lock:
            try:
                await self._repair_profile_scopes(connection, legacy)
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

    async def _misplaced_profile_rows(self, connection, identity=None):
        expressions = "json_extract(e.source_context,'$.source_session_id')"
        identity_clause = " AND p.identity=?" if identity is not None else ""
        async with connection.execute(
            "SELECT DISTINCT p.* FROM user_profiles p JOIN user_profile_claims c "
            "ON c.profile_scope=p.profile_scope AND c.profile_key=p.profile_key "
            "JOIN user_profile_evidence e ON e.claim_id=c.claim_id "
            "WHERE (substr(p.profile_scope,1,length(p.identity)+9)=p.identity || ':session:' "
            "OR substr(p.profile_scope,1,length(p.identity)+10)=p.identity || ':isolated:') "
            f"AND {expressions} IS NOT NULL AND {expressions}<>'' "
            f"AND p.profile_scope NOT IN (p.identity || ':session:' || {expressions},"
            f"p.identity || ':isolated:' || {expressions})" + identity_clause,
            [identity] if identity is not None else [],
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def _repair_profile_scopes(self, connection, identity=None):
        """Repair published whole-claim migrations without guessing missing origins."""
        for row in await self._misplaced_profile_rows(connection, identity):
            prefix = next(
                marker
                for marker in (":session:", ":isolated:")
                if row["profile_scope"].startswith(row["identity"] + marker)
            )
            recorded = row["profile_scope"][len(row["identity"] + prefix) :]
            async with connection.execute(
                "SELECT DISTINCT json_extract(e.source_context,'$.source_session_id') FROM user_profile_claims c "
                "JOIN user_profile_evidence e ON e.claim_id=c.claim_id WHERE c.profile_scope=? AND c.profile_key=?",
                (row["profile_scope"], row["profile_key"]),
            ) as cursor:
                sessions = [item[0] for item in await cursor.fetchall()]
            for session in sessions:
                if (
                    not isinstance(session, str)
                    or not session.strip()
                    or session == recorded
                ):
                    continue  # Unknown/manual origins retain their stored scope.
                async with connection.execute(
                    "SELECT * FROM user_profiles WHERE profile_scope=? AND profile_key=?",
                    (row["profile_scope"], row["profile_key"]),
                ) as cursor:
                    remaining = await cursor.fetchone()
                if remaining:
                    await self._split_profile_session(
                        connection,
                        dict(remaining),
                        row["identity"] + ":session:" + session,
                        session,
                    )

    @staticmethod
    def _profile_evidence_key(item):
        return (
            item["source_memory_id"],
            item["quote"],
            item.get("source_session_id") if item["source_memory_id"] is None else None,
        )

    async def _move_profile_evidence(self, connection, target_claim, evidence):
        known = {
            self._profile_evidence_key(item)
            for item in await self._claim_evidence(connection, target_claim)
        }
        added = False
        for item in evidence:
            signature = self._profile_evidence_key(item)
            if signature in known:
                await connection.execute(
                    "DELETE FROM user_profile_evidence WHERE evidence_id=?",
                    (item["evidence_id"],),
                )
            else:
                await connection.execute(
                    "UPDATE user_profile_evidence SET claim_id=? WHERE evidence_id=?",
                    (target_claim, item["evidence_id"]),
                )
                known.add(signature)
                added = True
        return added

    async def _exclude_rejected_profile_evidence(
        self, connection, scope, key, value, evidence
    ):
        async with connection.execute(
            "SELECT claim_id FROM user_profile_claims WHERE profile_scope=? AND profile_key=? AND value=? AND state='rejected'",
            (scope, key, value),
        ) as cursor:
            rejected = await cursor.fetchall()
        known = set()
        for row in rejected:
            known.update(
                self._profile_evidence_key(item)
                for item in await self._claim_evidence(connection, row[0])
            )
        remaining = []
        for item in evidence:
            if self._profile_evidence_key(item) in known:
                await connection.execute(
                    "DELETE FROM user_profile_evidence WHERE evidence_id=?",
                    (item["evidence_id"],),
                )
            else:
                remaining.append(item)
        return remaining

    async def _merge_profile_observation(
        self,
        connection,
        claim_id,
        incoming,
        target=None,
        *,
        manual_grant=False,
        manual_observation=False,
    ):
        async with connection.execute(
            "SELECT snapshot,updated_at,authority FROM user_profile_claims WHERE claim_id=?",
            (claim_id,),
        ) as cursor:
            claim = await cursor.fetchone()
        snapshot = json.loads(claim["snapshot"])
        before = dict(target) if target is not None else snapshot
        if target is not None and target["manual_override"]:
            return
        manual_proposal = (
            target is None and manual_observation and claim["authority"] != "manual"
        )
        expiry = (
            incoming.get("expires_at")
            if manual_grant or manual_proposal
            else before.get("expires_at")
        )
        preserve_manual_expiry = (
            target is None and claim["authority"] == "manual"
        ) or (
            target is not None
            and target["authority"] == "manual"
            and manual_observation
        )
        if (
            not manual_grant
            and not manual_proposal
            and not preserve_manual_expiry
            and expiry is not None
            and incoming.get("expires_at") is not None
        ):
            expiry = max(expiry, incoming["expires_at"])
        observed = max(
            before.get("updated_at", claim["updated_at"]), incoming.get("updated_at", 0)
        )
        selected_fact = (
            {
                field: incoming[field]
                for field in ("value", "category", "display_label", "confidence")
                if field in incoming
            }
            if manual_grant or manual_proposal
            else {}
        )
        snapshot.update(selected_fact)
        snapshot.update(expires_at=expiry, updated_at=observed)
        authority = claim["authority"]
        if manual_grant or manual_proposal:
            # The independent administrator assertion is a real saved choice,
            # including a shorter or unlimited expiry; automatic TTL cannot
            # replace it merely because this chat already had the same value.
            authority = "manual"
            snapshot["authority"] = authority
            if manual_grant:
                snapshot["manual_override"] = 1
        await connection.execute(
            "UPDATE user_profile_claims SET snapshot=?,updated_at=?,authority=? WHERE claim_id=?",
            (json.dumps(snapshot, ensure_ascii=False), observed, authority, claim_id),
        )
        if target is not None:
            projected = {**dict(target), **selected_fact}
            await connection.execute(
                "UPDATE user_profiles SET value=?,category=?,display_label=?,confidence=?,expires_at=?,updated_at=?,"
                "authority=?,manual_override=? WHERE profile_scope=? AND profile_key=?",
                (
                    projected["value"],
                    projected["category"],
                    projected["display_label"],
                    projected["confidence"],
                    expiry,
                    observed,
                    authority,
                    int(manual_grant or target["manual_override"]),
                    target["profile_scope"],
                    target["profile_key"],
                ),
            )

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
            state, recipient = claim["state"], None
            if target and old_id == row["current_claim_id"]:
                if target["value"] == claim["value"]:
                    recipient = target["current_claim_id"]
                else:
                    observed = max(
                        snapshot.get("updated_at", 0),
                        max(item["created_at"] for item in selected[old_id]),
                    )
                    state = (
                        "pending" if observed > target["updated_at"] else "superseded"
                    )
            if target and recipient is None and state == "pending":
                selected[old_id] = await self._exclude_rejected_profile_evidence(
                    connection, scope, key, claim["value"], selected[old_id]
                )
                if not selected[old_id]:
                    continue
                async with connection.execute(
                    "SELECT claim_id FROM user_profile_claims WHERE profile_scope=? AND profile_key=? AND value=? AND state='pending' ORDER BY claim_id DESC LIMIT 1",
                    (scope, key, claim["value"]),
                ) as cursor:
                    candidate = await cursor.fetchone()
                recipient = candidate[0] if candidate else None
            if recipient is not None:
                manual_grant = (
                    recipient == target["current_claim_id"]
                    and not target["manual_override"]
                    and target["authority"] != "manual"
                    and old_id == row["current_claim_id"]
                    and bool(row["manual_override"])
                    and claim["authority"] == "manual"
                    and any(
                        item["source_memory_id"] is None for item in selected[old_id]
                    )
                )
                added = await self._move_profile_evidence(
                    connection, recipient, selected[old_id]
                )
                if (
                    added
                    or manual_grant
                    or (
                        recipient != target["current_claim_id"]
                        and authority == "manual"
                    )
                ):
                    await self._merge_profile_observation(
                        connection,
                        recipient,
                        snapshot,
                        target if recipient == target["current_claim_id"] else None,
                        manual_grant=manual_grant,
                        manual_observation=authority == "manual",
                    )
                mapping[old_id] = recipient
                continue
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
                    (claim for claim in claims if claim["claim_id"] == old_current),
                    {"snapshot": "{}"},
                )
                snapshot = await self._session_claim_snapshot(
                    row, base, scope, [], context
                )
                snapshot.update(
                    value="[来源已删除]",
                    expires_at=None,
                    authority="legacy",
                    manual_override=0,
                )
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
            # Preserve the selected current value and manual grant. New support
            # joins it; conflicting current values become reviewable proposals.
            current_evidence = await self._claim_evidence(
                connection, target["current_claim_id"]
            )
            await self._project_profile_sources(
                connection, scope, key, await self._live_evidence(current_evidence)
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
