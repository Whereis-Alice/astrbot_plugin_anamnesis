"""Small helpers shared by the plugin's SQLite stores.

SQLite allows many readers but only one writer.  The memory plugin opens a
number of short-lived ``aiosqlite`` connections (BM25, graph, aliases and
atoms) in addition to AstrBot's SQLAlchemy connection.  Keeping the timeout
and connection setup in one place avoids a subtle source of lock contention:
``PRAGMA journal_mode=WAL`` is a database-wide write operation and must not be
run for every read/write connection.
"""

from __future__ import annotations

import math
from typing import Any

import aiosqlite


# Long enough for a normal FAISS/document transaction to finish, but bounded
# so a genuinely wedged process is still reported instead of hanging forever.
DEFAULT_SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
MAX_SQLITE_BUSY_TIMEOUT_SECONDS = 300.0


def resolve_sqlite_timeout(
    config: dict[str, Any] | None = None,
    *,
    default: float = DEFAULT_SQLITE_BUSY_TIMEOUT_SECONDS,
) -> float:
    """Return a safe SQLite busy timeout in seconds.

    ``sqlite_busy_timeout_seconds`` is intentionally optional so older config
    files continue to work.  Invalid, non-finite and non-positive values fall
    back to the supplied default; a tiny positive value is allowed for tests
    and for operators who explicitly want fail-fast behaviour.
    """

    options = config or {}
    raw = options.get("sqlite_busy_timeout_seconds", default)
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    if value <= 0:
        value = float(default)
    return min(value, MAX_SQLITE_BUSY_TIMEOUT_SECONDS)


async def configure_sqlite_connection(
    db: aiosqlite.Connection,
    config: dict[str, Any] | None = None,
    *,
    foreign_keys: bool = False,
) -> None:
    """Apply per-connection SQLite settings.

    WAL mode is deliberately *not* changed here.  It is a persistent
    database-level setting and should be enabled once during store
    initialisation, never on every hot-path connection.
    """

    # SQLite accepts integer milliseconds; preserve a tiny positive timeout as
    # at least one millisecond instead of rounding it down to zero (which
    # disables the busy handler altogether).
    timeout_ms = max(1, round(resolve_sqlite_timeout(config) * 1000.0))
    await db.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    if foreign_keys:
        await db.execute("PRAGMA foreign_keys = ON")


def sqlite_connect_kwargs(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return kwargs for ``aiosqlite.connect`` with a consistent timeout."""

    return {"timeout": resolve_sqlite_timeout(config)}


__all__ = [
    "DEFAULT_SQLITE_BUSY_TIMEOUT_SECONDS",
    "MAX_SQLITE_BUSY_TIMEOUT_SECONDS",
    "configure_sqlite_connection",
    "resolve_sqlite_timeout",
    "sqlite_connect_kwargs",
]
