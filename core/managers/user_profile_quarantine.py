"""Reversible administrator holds, independent of evidence and claim authority."""

from __future__ import annotations

import time
from typing import Any

_QUARANTINE_COLUMNS = (
    ("quarantined", "INTEGER NOT NULL DEFAULT 0"),
    ("quarantine_reason", "TEXT NOT NULL DEFAULT ''"),
    ("quarantined_at", "REAL"),
)


class UserProfileQuarantineMixin:
    async def _ensure_profile_quarantine_schema(self, connection: Any) -> None:
        async with connection.execute("PRAGMA table_info(user_profiles)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        for column, definition in _QUARANTINE_COLUMNS:
            if column not in columns:
                await connection.execute(
                    f"ALTER TABLE user_profiles ADD COLUMN {column} {definition}"
                )
        async with connection.execute(
            "PRAGMA table_info(user_profile_transitions)"
        ) as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "reason" not in columns:
            await connection.execute(
                "ALTER TABLE user_profile_transitions "
                "ADD COLUMN reason TEXT NOT NULL DEFAULT ''"
            )

    async def set_quarantine_for_web(
        self,
        profile_scope: str,
        profile_key: str,
        *,
        current_claim_id: int,
        expected_revision: int,
        action: str,
        actor: str,
        reason: str = "",
    ) -> dict[str, Any] | None:
        """Hold/resume an exact current claim without rewriting its evidence.

        The Web API supplies its authenticated actor. Even an idempotent request
        must match both the current claim and revision; stale requests fail.
        """
        selector = self._normalize_web_profile_selector(profile_scope, profile_key)
        if (
            selector is None
            or not isinstance(action, str)
            or action not in {"pause", "resume"}
            or not self._valid_profile_review_integer(current_claim_id)
            or not self._valid_profile_review_integer(expected_revision)
            or not isinstance(reason, str)
            or len(reason) > 500
            or not isinstance(actor, str)
            or not actor.startswith("webui:")
            or not actor[6:].strip()
        ):
            return None
        profile_scope, profile_key = selector
        reason = reason.strip()
        if not await self._ensure_schema() or self.store.connection is None:
            return None
        connection = self.store.connection
        async with self.store._write_lock:
            try:
                async with connection.execute(
                    "SELECT p.* FROM user_profiles p "
                    "JOIN user_profile_claims c ON c.claim_id=p.current_claim_id "
                    "AND c.profile_scope=p.profile_scope AND c.profile_key=p.profile_key "
                    "AND c.state='current' WHERE p.profile_scope=? AND p.profile_key=?",
                    (profile_scope, profile_key),
                ) as cursor:
                    record = await cursor.fetchone()
                if (
                    not record
                    or record["current_claim_id"] != current_claim_id
                    or record["revision"] != expected_revision
                ):
                    return None
                quarantine = int(action == "pause")
                if bool(record["quarantined"]) != bool(quarantine):
                    # A changed state needs a fresh revision. SQLite cannot
                    # represent a revision increment beyond its integer range.
                    if expected_revision == 2**63 - 1:
                        return None
                    await connection.execute(
                        "UPDATE user_profiles SET quarantined=?,quarantine_reason=?,"
                        "quarantined_at=?,revision=revision+1 "
                        "WHERE profile_scope=? AND profile_key=?",
                        (
                            quarantine,
                            reason if quarantine else "",
                            time.time() if quarantine else None,
                            profile_scope,
                            profile_key,
                        ),
                    )
                    await self._transition(
                        connection,
                        profile_scope,
                        profile_key,
                        "quarantined" if quarantine else "resumed",
                        current_claim_id,
                        current_claim_id,
                        actor,
                        reason=reason,
                    )
                    await self._commit_profile_invalidation()
            except BaseException:
                await connection.rollback()
                raise
        result = await self.list_for_web(
            profile_scope=profile_scope, key_query=profile_key
        )
        return next(
            (row for row in result["items"] if row["profile_key"] == profile_key), None
        )
