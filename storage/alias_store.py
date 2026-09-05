"""Append-only participant alias storage for stable identity resolution.

The graph keys people by ``account:<platform>:<sender_id>``, which is stable,
but the human-readable nickname changes over time and is frequently shared
between different accounts.  This store keeps the full nickname history so that

* ``person`` nodes can accumulate aliases instead of overwriting them, and
* a recall query mentioning an *old* nickname can still be expanded into the
  other nicknames that belong to the same account.

Writes are append-only upserts keyed by ``(identity_key, canonical_alias)``.
Reads are served from a small bounded in-process snapshot so the hot recall
path never touches SQLite.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import aiosqlite


#: Aliases shorter than this are not used for free-text matching. Single
#: characters match almost any Chinese sentence and would create noise.
MIN_MATCHABLE_ALIAS_LENGTH = 2

#: Upper bound on extra FTS terms injected by alias expansion. Keeps the OR
#: query from exploding when a very active account has a long rename history.
MAX_QUERY_EXPANSION_TERMS = 12


def _load_metadata(payload: Any) -> dict[str, Any]:
    """Parse a stored metadata blob, tolerating legacy/corrupt rows."""
    if isinstance(payload, dict):
        return payload
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def canonicalize_alias(alias: Any) -> str:
    """Normalise an alias for case- and whitespace-insensitive comparison."""
    return " ".join(str(alias or "").split()).casefold()


class AliasStore:
    """Track every nickname ever observed for a stable platform identity."""

    _BACKFILL_BATCH_SIZE = 500

    def __init__(self, db_path: str, config: dict[str, Any] | None = None):
        self.db_path = db_path
        options = config or {}
        self.max_per_identity = max(
            1, int(options.get("alias_max_per_identity", 40) or 40)
        )
        self.cache_max = max(0, int(options.get("alias_cache_max", 2000) or 0))
        self.cache_ttl = max(
            0.0, float(options.get("alias_cache_ttl_seconds", 300.0) or 0.0)
        )
        self.query_expansion = bool(options.get("alias_query_expansion", True))
        # canonical alias -> identity keys that ever used it
        self._alias_index: dict[str, list[str]] = {}
        # identity key -> display aliases (most recent first)
        self._identity_index: dict[str, list[str]] = {}
        self._snapshot_at = 0.0
        self._snapshot_ready = False
        self._initialized = False

    # ------------------------------------------------------------------ infra

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA busy_timeout = 10000")
            yield db
        finally:
            await db.close()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def initialize(self) -> None:
        """Create the alias table and supporting indexes."""
        async with self._connect() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS person_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity_key TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    canonical_alias TEXT NOT NULL,
                    platform TEXT,
                    sender_id TEXT,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    hits INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(identity_key, canonical_alias)
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_person_aliases_canonical "
                "ON person_aliases(canonical_alias)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_person_aliases_identity "
                "ON person_aliases(identity_key, last_seen DESC)"
            )
            await db.commit()
        self._initialized = True
        self.invalidate_cache()

    def invalidate_cache(self) -> None:
        self._snapshot_ready = False
        self._snapshot_at = 0.0

    # ------------------------------------------------------------------ writes

    @staticmethod
    def _iter_alias_candidates(identity: dict[str, Any]) -> list[str]:
        """Collect the real nicknames worth persisting for one identity."""
        sender_id = str(identity.get("sender_id") or "").strip()
        raw: list[str] = []
        display_name = identity.get("display_name")
        if display_name:
            raw.append(str(display_name))
        for alias in identity.get("aliases") or []:
            if alias:
                raw.append(str(alias))

        seen: set[str] = set()
        result: list[str] = []
        for alias in raw:
            trimmed = " ".join(alias.split())
            if not trimmed:
                continue
            # A nickname equal to the raw account id carries no extra signal and
            # would pollute free-text matching with bare numeric strings.
            if sender_id and trimmed == sender_id:
                continue
            canonical = canonicalize_alias(trimmed)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            result.append(trimmed)
        return result

    async def record_identities(self, identities: list[dict[str, Any]]) -> int:
        """Upsert the nicknames of the supplied identities. Returns row count."""
        if not identities:
            return 0

        rows: list[tuple[Any, ...]] = []
        now = self._now_iso()
        touched: set[str] = set()
        for identity in identities:
            if not isinstance(identity, dict):
                continue
            identity_key = str(identity.get("identity_key") or "").strip()
            if not identity_key:
                continue
            # Identities flagged as unstable by IdentityGuard are deliberately
            # not tracked: their nickname stream is upstream noise, not history.
            if identity.get("name_unstable"):
                continue
            platform = str(identity.get("platform") or "").strip() or None
            sender_id = str(identity.get("sender_id") or "").strip() or None
            is_bot = 1 if identity.get("is_bot") else 0
            for alias in self._iter_alias_candidates(identity):
                rows.append(
                    (
                        identity_key,
                        alias,
                        canonicalize_alias(alias),
                        platform,
                        sender_id,
                        is_bot,
                        now,
                        now,
                    )
                )
                touched.add(identity_key)

        if not rows:
            return 0

        async with self._connect() as db:
            await db.executemany(
                """
                INSERT INTO person_aliases (
                    identity_key, alias, canonical_alias, platform, sender_id,
                    is_bot, first_seen, last_seen, hits
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(identity_key, canonical_alias) DO UPDATE SET
                    alias = excluded.alias,
                    platform = COALESCE(excluded.platform, person_aliases.platform),
                    sender_id = COALESCE(excluded.sender_id, person_aliases.sender_id),
                    is_bot = MAX(person_aliases.is_bot, excluded.is_bot),
                    last_seen = excluded.last_seen,
                    hits = person_aliases.hits + 1
                """,
                rows,
            )
            for identity_key in touched:
                await self._prune_identity(db, identity_key)
            await db.commit()

        self.invalidate_cache()
        return len(rows)

    async def _prune_identity(self, db, identity_key: str) -> None:
        """Keep only the most recent/frequent aliases for one identity."""
        cursor = await db.execute(
            "SELECT COUNT(*) FROM person_aliases WHERE identity_key = ?",
            (identity_key,),
        )
        row = await cursor.fetchone()
        total = int(row[0]) if row else 0
        excess = total - self.max_per_identity
        if excess <= 0:
            return
        await db.execute(
            """
            DELETE FROM person_aliases WHERE id IN (
                SELECT id FROM person_aliases WHERE identity_key = ?
                ORDER BY last_seen ASC, hits ASC, id ASC LIMIT ?
            )
            """,
            (identity_key, excess),
        )

    # ------------------------------------------------------------------- reads

    async def refresh_snapshot(self, force: bool = False) -> None:
        """Load a bounded alias snapshot into memory, honouring the TTL."""
        if self.cache_max <= 0:
            self._alias_index = {}
            self._identity_index = {}
            self._snapshot_ready = True
            self._snapshot_at = time.time()
            return
        now = time.time()
        if (
            not force
            and self._snapshot_ready
            and (self.cache_ttl <= 0 or now - self._snapshot_at < self.cache_ttl)
        ):
            return

        alias_index: dict[str, list[str]] = {}
        identity_index: dict[str, list[str]] = {}
        try:
            async with self._connect() as db:
                cursor = await db.execute(
                    """
                    SELECT identity_key, alias, canonical_alias
                    FROM person_aliases
                    ORDER BY last_seen DESC, hits DESC, id DESC
                    LIMIT ?
                    """,
                    (self.cache_max,),
                )
                fetched = await cursor.fetchall()
        except aiosqlite.Error:
            # A missing table simply means "no aliases known yet".
            fetched = []

        for identity_key, alias, canonical in fetched:
            if not identity_key or not canonical:
                continue
            keys = alias_index.setdefault(canonical, [])
            if identity_key not in keys:
                keys.append(identity_key)
            names = identity_index.setdefault(identity_key, [])
            if alias and alias not in names:
                names.append(alias)

        self._alias_index = alias_index
        self._identity_index = identity_index
        self._snapshot_ready = True
        self._snapshot_at = time.time()

    async def aliases_for(self, identity_key: str) -> list[str]:
        """Return known nicknames for one identity, most recent first."""
        if not identity_key:
            return []
        await self.refresh_snapshot()
        cached = self._identity_index.get(identity_key)
        if cached is not None:
            return list(cached)
        # Not in the bounded snapshot: fall back to a targeted query.
        try:
            async with self._connect() as db:
                cursor = await db.execute(
                    """
                    SELECT alias FROM person_aliases WHERE identity_key = ?
                    ORDER BY last_seen DESC, hits DESC LIMIT ?
                    """,
                    (identity_key, self.max_per_identity),
                )
                rows = await cursor.fetchall()
        except aiosqlite.Error:
            return []
        return [row[0] for row in rows if row[0]]

    async def aliases_for_many(
        self, identity_keys: list[str]
    ) -> dict[str, list[str]]:
        """Batch variant of :meth:`aliases_for`."""
        unique = [key for key in dict.fromkeys(identity_keys or []) if key]
        if not unique:
            return {}
        await self.refresh_snapshot()
        result: dict[str, list[str]] = {}
        missing: list[str] = []
        for key in unique:
            cached = self._identity_index.get(key)
            if cached is None:
                missing.append(key)
            else:
                result[key] = list(cached)
        if not missing:
            return result
        placeholders = ", ".join("?" for _ in missing)
        try:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT identity_key, alias FROM person_aliases "
                    "WHERE identity_key IN (" + placeholders + ") "
                    "ORDER BY last_seen DESC, hits DESC",
                    tuple(missing),
                )
                rows = await cursor.fetchall()
        except aiosqlite.Error:
            rows = []
        for identity_key, alias in rows:
            bucket = result.setdefault(identity_key, [])
            if alias and alias not in bucket:
                if len(bucket) < self.max_per_identity:
                    bucket.append(alias)
        return result

    async def resolve_alias(self, name: str) -> list[str]:
        """Return the identity keys that ever used ``name``."""
        canonical = canonicalize_alias(name)
        if not canonical:
            return []
        await self.refresh_snapshot()
        cached = self._alias_index.get(canonical)
        if cached is not None:
            return list(cached)
        try:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT identity_key FROM person_aliases "
                    "WHERE canonical_alias = ? ORDER BY last_seen DESC",
                    (canonical,),
                )
                rows = await cursor.fetchall()
        except aiosqlite.Error:
            return []
        return [row[0] for row in rows if row[0]]

    async def match_in_text(self, text: str, limit: int = 8) -> list[dict[str, Any]]:
        """Find known aliases mentioned in free text (substring match).

        Chinese nicknames are routinely shredded by the tokenizer, so exact
        token equality misses most of them. Matching against the raw string in
        Python is both more accurate and cheaper than extra FTS round-trips.
        """
        if not text or limit <= 0:
            return []
        await self.refresh_snapshot()
        if not self._alias_index:
            return []
        haystack = canonicalize_alias(text)
        if not haystack:
            return []
        matches: list[dict[str, Any]] = []
        for canonical, identity_keys in self._alias_index.items():
            if len(canonical) < MIN_MATCHABLE_ALIAS_LENGTH:
                continue
            if canonical not in haystack:
                continue
            matches.append(
                {"canonical_alias": canonical, "identity_keys": list(identity_keys)}
            )
        # Longest alias first: the most specific match is the most informative.
        matches.sort(key=lambda item: len(item["canonical_alias"]), reverse=True)
        return matches[:limit]

    async def expand_tokens(
        self,
        query: str,
        tokens: list[str] | None = None,
        limit: int = 12,
    ) -> list[str]:
        """Return sibling nicknames of identities mentioned in ``query``."""
        if not query or limit <= 0:
            return []
        matches = await self.match_in_text(query)
        if not matches:
            return []
        identity_keys: list[str] = []
        for match in matches:
            for key in match["identity_keys"]:
                if key not in identity_keys:
                    identity_keys.append(key)
        alias_map = await self.aliases_for_many(identity_keys)
        haystack = canonicalize_alias(query)
        existing = {canonicalize_alias(token) for token in tokens or []}
        expanded: list[str] = []
        for key in identity_keys:
            for alias in alias_map.get(key, []):
                canonical = canonicalize_alias(alias)
                if len(canonical) < MIN_MATCHABLE_ALIAS_LENGTH:
                    continue
                if canonical in haystack or canonical in existing:
                    continue
                existing.add(canonical)
                expanded.append(alias)
                if len(expanded) >= limit:
                    return expanded
        return expanded

    # -------------------------------------------------------------- maintenance

    async def count(self) -> int:
        try:
            async with self._connect() as db:
                cursor = await db.execute("SELECT COUNT(*) FROM person_aliases")
                row = await cursor.fetchone()
        except aiosqlite.Error:
            return 0
        return int(row[0]) if row else 0

    async def stats(self) -> dict[str, Any]:
        """Summarise the alias table for the maintenance/status commands."""
        payload: dict[str, Any] = {
            "aliases": 0,
            "identities": 0,
            "multi_alias_identities": 0,
            "shared_aliases": 0,
            "cached_aliases": len(self._alias_index),
            "cache_max": self.cache_max,
        }
        try:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT identity_key), "
                    "COUNT(DISTINCT canonical_alias) FROM person_aliases"
                )
                row = await cursor.fetchone()
                if row:
                    payload["aliases"] = int(row[0] or 0)
                    payload["identities"] = int(row[1] or 0)
                    payload["distinct_aliases"] = int(row[2] or 0)
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM (SELECT identity_key FROM person_aliases "
                    "GROUP BY identity_key HAVING COUNT(*) > 1)"
                )
                row = await cursor.fetchone()
                payload["multi_alias_identities"] = int(row[0]) if row else 0
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM (SELECT canonical_alias FROM person_aliases "
                    "GROUP BY canonical_alias HAVING COUNT(DISTINCT identity_key) > 1)"
                )
                row = await cursor.fetchone()
                payload["shared_aliases"] = int(row[0]) if row else 0
        except aiosqlite.Error:
            return payload
        return payload

    async def bot_flag_rows(self) -> list[dict[str, Any]]:
        """List identities currently flagged as a Bot.

        ``is_bot`` is aggregated with ``MAX()``, so one mislabelled message
        marks a real member's account as the Bot permanently.  The maintenance
        command needs the raw list to show what a repair would change.
        """
        rows: list[dict[str, Any]] = []
        try:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT identity_key, platform, sender_id, COUNT(*) AS alias_count, "
                    "GROUP_CONCAT(alias, ' / ') AS aliases FROM person_aliases "
                    "WHERE is_bot = 1 GROUP BY identity_key, platform, sender_id "
                    "ORDER BY alias_count DESC, identity_key ASC"
                )
                for row in await cursor.fetchall():
                    rows.append(
                        {
                            "identity_key": str(row[0] or ""),
                            "platform": str(row[1] or ""),
                            "sender_id": str(row[2] or ""),
                            "alias_count": int(row[3] or 0),
                            "aliases": str(row[4] or ""),
                        }
                    )
        except aiosqlite.Error:
            return rows
        return rows

    async def clear_false_bot_flags(self, bot_identity_keys) -> int:
        """Clear ``is_bot`` on every identity that is not a known Bot.

        Requires the caller to name the real Bot identities; without them the
        method is a no-op rather than a table-wide wipe.  The alias text itself
        is preserved because it is a genuine nickname of that account -- only
        the misapplied Bot flag is dropped.
        """
        keys = [str(key) for key in (bot_identity_keys or []) if key]
        if not keys:
            return 0
        placeholders = ", ".join("?" for _ in keys)
        try:
            async with self._connect() as db:
                cursor = await db.execute(
                    "UPDATE person_aliases SET is_bot = 0 WHERE is_bot = 1 "
                    "AND identity_key NOT IN (" + placeholders + ")",
                    keys,
                )
                cleared = cursor.rowcount or 0
                await db.commit()
        except aiosqlite.Error:
            return 0
        self.invalidate_cache()
        return int(cleared)

    async def backfill_from_documents(self, db_connection) -> int:
        """Seed the table from ``documents.metadata.participant_identities``.

        Existing installs already carry years of nickname history inside stored
        memory metadata. Replaying it costs one sequential scan and immediately
        makes alias expansion useful instead of waiting for new traffic.
        """
        if db_connection is None:
            return 0
        offset = 0
        recorded = 0
        while True:
            try:
                cursor = await db_connection.execute(
                    "SELECT metadata FROM documents WHERE metadata IS NOT NULL "
                    "ORDER BY id LIMIT ? OFFSET ?",
                    (self._BACKFILL_BATCH_SIZE, offset),
                )
                rows = await cursor.fetchall()
            except aiosqlite.Error:
                break
            if not rows:
                break
            batch: dict[str, dict[str, Any]] = {}
            for row in rows:
                metadata = _load_metadata(row[0])
                if not metadata:
                    continue
                for identity in metadata.get("participant_identities") or []:
                    if not isinstance(identity, dict):
                        continue
                    key = str(identity.get("identity_key") or "").strip()
                    if not key:
                        continue
                    merged = batch.setdefault(
                        key,
                        {
                            "identity_key": key,
                            "sender_id": identity.get("sender_id"),
                            "platform": identity.get("platform"),
                            "display_name": identity.get("display_name"),
                            "aliases": [],
                            "is_bot": bool(identity.get("is_bot")),
                        },
                    )
                    merged["is_bot"] = bool(merged["is_bot"] or identity.get("is_bot"))
                    for alias in identity.get("aliases") or []:
                        if alias and alias not in merged["aliases"]:
                            merged["aliases"].append(str(alias))
                    display_name = identity.get("display_name")
                    if display_name and display_name not in merged["aliases"]:
                        merged["aliases"].append(str(display_name))
            if batch:
                recorded += await self.record_identities(list(batch.values()))
            if len(rows) < self._BACKFILL_BATCH_SIZE:
                break
            offset += self._BACKFILL_BATCH_SIZE
        return recorded


async def expand_query_tokens(
    alias_store: AliasStore | None,
    query: str,
    tokens: list[str],
) -> list[str]:
    """Append sibling nicknames of the accounts named in ``query``.

    Searching for someone by an old nickname should still find them. Alias
    expansion is best-effort: any failure returns the original tokens so recall
    degrades to today's behaviour instead of breaking.
    """
    if alias_store is None or not getattr(alias_store, "query_expansion", False):
        return tokens
    try:
        extra = await alias_store.expand_tokens(
            query, tokens, limit=MAX_QUERY_EXPANSION_TERMS
        )
    except Exception:
        return tokens
    if not extra:
        return tokens
    return list(tokens) + extra


__all__ = [
    "AliasStore",
    "canonicalize_alias",
    "expand_query_tokens",
    "MIN_MATCHABLE_ALIAS_LENGTH",
    "MAX_QUERY_EXPANSION_TERMS",
]
