"""Regression tests for SQLite contention safeguards."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_anamnesis.core.managers.memory_engine_crud import (
    MemoryEngineCrudMixin,
)
from astrbot_plugin_anamnesis.storage.sqlite_utils import resolve_sqlite_timeout


class _RetryEngine(MemoryEngineCrudMixin):
    def __init__(self, access_connection, main_connection):
        self.config = {
            "sqlite_lock_retries": 1,
            "sqlite_lock_retry_delay_seconds": 0.01,
        }
        self._access_update_lock = asyncio.Lock()
        self._access_update_connection = access_connection
        self.db_connection = main_connection


@pytest.mark.asyncio
async def test_access_update_retries_locked_write_on_isolated_connection():
    """A transient lock is retried without touching the main transaction."""

    cursor = SimpleNamespace(rowcount=1)
    access = Mock()
    access.execute = AsyncMock(
        side_effect=[sqlite3.OperationalError("database is locked"), cursor]
    )
    access.commit = AsyncMock()
    access.rollback = AsyncMock()
    main = Mock()
    main.rollback = AsyncMock()

    engine = _RetryEngine(access, main)

    assert await engine._update_access_times_internal([7, 7]) is True
    assert access.execute.await_count == 2
    access.rollback.assert_awaited_once()
    access.commit.assert_awaited_once()
    main.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_update_does_not_retry_non_lock_errors():
    access = Mock()
    access.execute = AsyncMock(side_effect=RuntimeError("schema failure"))
    access.rollback = AsyncMock()
    access.commit = AsyncMock()
    main = Mock()
    main.rollback = AsyncMock()

    engine = _RetryEngine(access, main)

    assert await engine._update_access_times_internal([1]) is False
    assert access.execute.await_count == 1
    access.rollback.assert_awaited_once()
    access.commit.assert_not_awaited()
    main.rollback.assert_not_awaited()


@pytest.mark.parametrize(
    "message",
    [
        "database is locked",
        "database table is locked",
        "database schema is locked",
        "database is busy",
    ],
)
def test_sqlite_lock_error_variants_are_recognized(message):
    assert MemoryEngineCrudMixin._is_sqlite_lock_error(
        sqlite3.OperationalError(message)
    )


def test_unrelated_locked_error_is_not_retried():
    assert not MemoryEngineCrudMixin._is_sqlite_lock_error(
        RuntimeError("resource locked by another worker")
    )


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, 30.0),
        ({"sqlite_busy_timeout_seconds": 12.5}, 12.5),
        ({"sqlite_busy_timeout_seconds": 9999}, 300.0),
        ({"sqlite_busy_timeout_seconds": 0}, 30.0),
        ({"sqlite_busy_timeout_seconds": "invalid"}, 30.0),
    ],
)
def test_sqlite_busy_timeout_is_bounded(config, expected):
    assert resolve_sqlite_timeout(config) == expected
