"""Audit and repair mislabelled Bot identities in the conversation log.

Older builds resolved the ``sender_id`` of an ``assistant`` row from the
*triggering user* instead of the Bot account.  Every reply the Bot made was
therefore filed under whichever member happened to speak last, and the rest of
the pipeline faithfully inherited the mistake:

* ``person_aliases.is_bot`` is aggregated with ``MAX()``, so a single bad row
  marks a real member's account as the Bot forever;
* graph ``person`` nodes OR the same flag into their metadata.

This module is deliberately conservative.  It never guesses: the authoritative
Bot identity comes from the live adapter (``event.get_self_id()``) whenever the
command is issued on that platform, and the statistical fallback only fires
when one ``sender_id`` owns a clear majority of the platform's assistant rows.
Anything ambiguous is reported and skipped rather than "repaired" on a coin
flip.  Every rewritten row is copied into an undo log first, so :meth:`repair`
is reversible via :meth:`rollback`.

Only the conversation log is touched.  Already-extracted memories and graph
nodes are left alone: rewriting them would mean re-running extraction over the
whole history, and the stale attributions age out through normal decay.
"""

from __future__ import annotations

import time
from typing import Any

import aiosqlite

from astrbot.api import logger

#: A statistically inferred Bot id must own more than this share of a
#: platform's ``assistant`` rows before it is trusted.
MIN_AUTHORITY_RATIO = 0.5

#: How many distinct wrong identities to list per platform in the report.
TOP_OFFENDERS = 5

#: Row-level undo log written by :meth:`IdentityRepair.repair`.
BACKUP_TABLE = "identity_repair_backup"


def _is_synthetic_bot_id(value: Any) -> bool:
    """``bot:<scope>`` ids are placeholders emitted when the adapter is mute."""
    return str(value or "").startswith("bot:")


def normalise_platform(value: Any) -> str:
    """Platform names arrive with inconsistent casing across adapters."""
    return str(value or "").strip().lower()


class IdentityRepair:
    """Diagnose and fix ``assistant`` rows attributed to the wrong account."""

    def __init__(self, conversation_manager, alias_store=None):
        self.manager = conversation_manager
        # Accept either the manager or a bare store so tests can stay light.
        self.store = getattr(conversation_manager, "store", conversation_manager)
        self.alias_store = alias_store

    # ---------------------------------------------------------------- helpers

    @property
    def connection(self):
        """The live ``aiosqlite`` connection, or ``None`` when not started."""
        return getattr(self.store, "connection", None)

    async def _ensure_backup_table(self, db) -> None:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS " + BACKUP_TABLE + " ("
            "    message_id INTEGER PRIMARY KEY,"
            "    old_sender_id TEXT,"
            "    old_sender_name TEXT,"
            "    new_sender_id TEXT,"
            "    new_sender_name TEXT,"
            "    platform TEXT,"
            "    repaired_at REAL NOT NULL"
            ")"
        )

    async def _backup_rows(self) -> int:
        db = self.connection
        if db is None:
            return 0
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM " + BACKUP_TABLE)
            row = await cursor.fetchone()
        except aiosqlite.Error:
            # Table absent simply means "never repaired".
            return 0
        return int(row[0]) if row else 0

    async def _invalidate_conversation_cache(self) -> None:
        """Drop the context cache so repaired rows are re-read from SQLite."""
        cache = getattr(self.manager, "_cache", None)
        if cache is None:
            return
        lock = getattr(self.manager, "_cache_lock", None)
        if lock is None:
            cache.clear()
            return
        async with lock:
            cache.clear()

    @staticmethod
    def _dominant_name(entry: dict[str, Any], fallback: str) -> str:
        """Pick the most frequently used display name, ignoring blanks."""
        names = entry.get("names") or {}
        for name, _count in sorted(names.items(), key=lambda item: (-item[1], item[0])):
            if name:
                return name
        return fallback

    async def _collect_histogram(
        self, platform_filter: str | None = None
    ) -> dict[str, dict[str, dict[str, Any]]] | None:
        """Group ``assistant`` rows by platform, sender and display name."""
        db = self.connection
        if db is None:
            return None
        cursor = await db.execute(
            "SELECT COALESCE(platform, '') AS platform, sender_id, "
            "COALESCE(sender_name, '') AS sender_name, COUNT(*) AS cnt "
            "FROM messages WHERE role = 'assistant' "
            "GROUP BY COALESCE(platform, ''), sender_id, COALESCE(sender_name, '')"
        )
        rows = await cursor.fetchall()
        wanted = normalise_platform(platform_filter)
        histogram: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            platform = str(row[0] or "")
            if wanted and normalise_platform(platform) != wanted:
                continue
            sender_id = str(row[1] or "")
            sender_name = str(row[2] or "")
            count = int(row[3] or 0)
            senders = histogram.setdefault(platform, {})
            entry = senders.setdefault(sender_id, {"total": 0, "names": {}})
            entry["total"] += count
            entry["names"][sender_name] = entry["names"].get(sender_name, 0) + count
        return histogram

    def _resolve_authority(
        self,
        platform: str,
        senders: dict[str, dict[str, Any]],
        live_bot_ids: dict[str, tuple[str, str]] | None,
    ) -> dict[str, Any]:
        """Decide which ``sender_id`` really belongs to the Bot.

        The live adapter always wins.  Otherwise the most common id is accepted
        only when it owns a strict majority, because a table where the Bot is
        outnumbered by its own misattributions cannot be repaired safely.
        """
        live = (live_bot_ids or {}).get(normalise_platform(platform))
        if live:
            bot_id = str(live[0] or "")
            if bot_id and not _is_synthetic_bot_id(bot_id):
                entry = senders.get(bot_id) or {"total": 0, "names": {}}
                bot_name = str(live[1] or "") or self._dominant_name(entry, bot_id)
                return {
                    "bot_id": bot_id,
                    "bot_name": bot_name,
                    "source": "live",
                    "ambiguous": False,
                }

        total = sum(entry["total"] for entry in senders.values())
        ranked = sorted(senders.items(), key=lambda item: (-item[1]["total"], item[0]))
        if not ranked or not total:
            return {
                "bot_id": "",
                "bot_name": "",
                "source": "none",
                "ambiguous": True,
            }
        bot_id, entry = ranked[0]
        ratio = entry["total"] / total
        return {
            "bot_id": bot_id,
            "bot_name": self._dominant_name(entry, bot_id),
            "source": "majority",
            "ambiguous": ratio <= MIN_AUTHORITY_RATIO,
        }

    async def _alias_flag_report(
        self, platforms: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """List identities carrying an ``is_bot`` flag they should not have."""
        report: dict[str, Any] = {"false_bot_flags": [], "bot_identity_keys": []}
        if self.alias_store is None:
            return report
        keys = {
            normalise_platform(item["platform"]) + ":" + item["bot_id"]
            for item in platforms
            if item["bot_id"] and not item["ambiguous"]
        }
        report["bot_identity_keys"] = sorted(keys)
        try:
            rows = await self.alias_store.bot_flag_rows()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"[IdentityRepair] 读取别名 is_bot 标记失败: {exc}")
            return report
        report["false_bot_flags"] = [
            row for row in rows if str(row.get("identity_key") or "") not in keys
        ]
        return report

    # --------------------------------------------------------------- analysis

    async def analyse(
        self,
        platform_filter: str | None = None,
        live_bot_ids: dict[str, tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Report per-platform Bot attribution without writing anything."""
        try:
            histogram = await self._collect_histogram(platform_filter)
        except aiosqlite.Error as exc:
            logger.error(f"[IdentityRepair] 扫描会话库失败: {exc}")
            return {"ok": False, "reason": str(exc)}
        if histogram is None:
            return {"ok": False, "reason": "conversation_store_unavailable"}

        platforms: list[dict[str, Any]] = []
        total_wrong = 0
        for platform in sorted(histogram):
            senders = histogram[platform]
            authority = self._resolve_authority(platform, senders, live_bot_ids)
            bot_id = authority["bot_id"]
            total = sum(entry["total"] for entry in senders.values())
            correct = int((senders.get(bot_id) or {}).get("total", 0))
            wrong = total - correct
            if not authority["ambiguous"]:
                total_wrong += wrong
            offenders = [
                {
                    "sender_id": sender_id,
                    "sender_name": self._dominant_name(entry, sender_id),
                    "count": entry["total"],
                }
                for sender_id, entry in sorted(
                    senders.items(), key=lambda item: (-item[1]["total"], item[0])
                )
                if sender_id != bot_id
            ]
            platforms.append(
                {
                    "platform": platform,
                    "bot_id": bot_id,
                    "bot_name": authority["bot_name"],
                    "source": authority["source"],
                    "ambiguous": authority["ambiguous"],
                    "total": total,
                    "correct": correct,
                    "wrong": wrong,
                    "distinct_wrong": len(offenders),
                    "offenders": offenders[:TOP_OFFENDERS],
                }
            )

        payload: dict[str, Any] = {
            "ok": True,
            "platforms": platforms,
            "total_wrong": total_wrong,
            "backup_rows": await self._backup_rows(),
        }
        payload.update(await self._alias_flag_report(platforms))
        return payload

    # ------------------------------------------------------------------ write

    async def repair(
        self,
        platform_filter: str | None = None,
        live_bot_ids: dict[str, tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Rewrite mislabelled rows, keeping a row-level undo log.

        The sticky ``is_bot`` flags cleared here are *not* restored by
        :meth:`rollback`; they are display-only metadata and are re-learned from
        traffic, so recreating them would just reinstate the bug.
        """
        report = await self.analyse(platform_filter, live_bot_ids)
        if not report.get("ok"):
            return report

        db = self.connection
        if db is None:
            return {"ok": False, "reason": "conversation_store_unavailable"}

        now = time.time()
        repaired = 0
        skipped: list[str] = []
        try:
            await self._ensure_backup_table(db)
            for item in report["platforms"]:
                if item["ambiguous"]:
                    skipped.append(item["platform"] or "unknown")
                    continue
                if item["wrong"] <= 0:
                    continue
                await db.execute(
                    "INSERT OR REPLACE INTO " + BACKUP_TABLE + " ("
                    "message_id, old_sender_id, old_sender_name, "
                    "new_sender_id, new_sender_name, platform, repaired_at) "
                    "SELECT id, sender_id, sender_name, ?, ?, "
                    "COALESCE(platform, ''), ? FROM messages "
                    "WHERE role = 'assistant' AND COALESCE(platform, '') = ? "
                    "AND sender_id != ?",
                    (
                        item["bot_id"],
                        item["bot_name"],
                        now,
                        item["platform"],
                        item["bot_id"],
                    ),
                )
                cursor = await db.execute(
                    "UPDATE messages SET sender_id = ?, sender_name = ? "
                    "WHERE role = 'assistant' AND COALESCE(platform, '') = ? "
                    "AND sender_id != ?",
                    (
                        item["bot_id"],
                        item["bot_name"],
                        item["platform"],
                        item["bot_id"],
                    ),
                )
                repaired += cursor.rowcount or 0
            await db.commit()
        except aiosqlite.Error as exc:
            logger.error(f"[IdentityRepair] 修复会话归属失败: {exc}", exc_info=True)
            try:
                await db.rollback()
            except aiosqlite.Error:
                pass
            return {"ok": False, "reason": str(exc)}

        cleared_flags = 0
        keys = report.get("bot_identity_keys") or []
        if self.alias_store is not None and keys and report.get("false_bot_flags"):
            try:
                cleared_flags = await self.alias_store.clear_false_bot_flags(keys)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"[IdentityRepair] 清理别名 is_bot 标记失败: {exc}")

        await self._invalidate_conversation_cache()
        return {
            "ok": True,
            "repaired": repaired,
            "skipped": skipped,
            "cleared_bot_flags": cleared_flags,
            "platforms": report["platforms"],
            "backup_rows": await self._backup_rows(),
        }

    async def rollback(self) -> dict[str, Any]:
        """Restore every row saved by the most recent :meth:`repair` run."""
        db = self.connection
        if db is None:
            return {"ok": False, "reason": "conversation_store_unavailable"}
        pending = await self._backup_rows()
        if pending <= 0:
            return {"ok": True, "restored": 0, "empty": True}
        try:
            cursor = await db.execute(
                "UPDATE messages SET sender_id = (SELECT old_sender_id FROM "
                + BACKUP_TABLE
                + " WHERE message_id = messages.id), sender_name = ("
                "SELECT old_sender_name FROM " + BACKUP_TABLE
                + " WHERE message_id = messages.id) WHERE id IN ("
                "SELECT message_id FROM " + BACKUP_TABLE + ")"
            )
            restored = cursor.rowcount or 0
            await db.execute("DELETE FROM " + BACKUP_TABLE)
            await db.commit()
        except aiosqlite.Error as exc:
            logger.error(f"[IdentityRepair] 回滚身份修复失败: {exc}", exc_info=True)
            try:
                await db.rollback()
            except aiosqlite.Error:
                pass
            return {"ok": False, "reason": str(exc)}
        await self._invalidate_conversation_cache()
        return {"ok": True, "restored": restored, "empty": False}


__all__ = [
    "IdentityRepair",
    "normalise_platform",
    "BACKUP_TABLE",
    "MIN_AUTHORITY_RATIO",
    "TOP_OFFENDERS",
]
