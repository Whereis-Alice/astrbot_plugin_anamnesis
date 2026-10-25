"""Regression tests for selected upstream fixes adapted to Anamnesis."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.base.exceptions import InitializationError
from astrbot_plugin_anamnesis.core.managers.memory_engine_crud import (
    MemoryEngineCrudMixin,
)
from astrbot_plugin_anamnesis.core.plugin_initializer import PluginInitializer
from astrbot_plugin_anamnesis.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_anamnesis.core.schedulers.decay_scheduler import DecayScheduler
from astrbot_plugin_anamnesis.core.utils.formatting import (
    format_memories_for_fake_tool_call,
    format_memories_for_injection,
)


@pytest.mark.asyncio
async def test_decay_preserves_concurrent_access_and_metadata(tmp_path, monkeypatch):
    async with aiosqlite.connect(tmp_path / "decay.db") as db:
        db.row_factory = aiosqlite.Row
        await db.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, metadata TEXT)")
        await db.execute("INSERT INTO documents VALUES(1, ?)", (
            json.dumps({"importance": 0.8, "access_count": 2, "last_access_time": 0}),
        ))
        await db.commit()
        engine = MemoryEngineCrudMixin()
        engine.db_connection = db
        engine.config = {}
        engine._safe_json_dict = lambda value: json.loads(value)
        engine._invalidate_search_cache = Mock()
        real_to_thread = asyncio.to_thread

        async def concurrent_write(function, *args, **kwargs):
            # Occurs after decay's SELECT, before its metadata UPDATE.
            await db.execute("UPDATE documents SET metadata=json_set(metadata, "
                             "'$.access_count', 10, '$.last_access_time', 12345, "
                             "'$.profile_note', 'new') WHERE id=1")
            await db.commit()
            return await real_to_thread(function, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", concurrent_write)
        assert await engine.apply_daily_decay(0.1) == 1
        cursor = await db.execute("SELECT metadata FROM documents WHERE id=1")
        metadata = json.loads((await cursor.fetchone())[0])
        assert metadata["last_access_time"] == 12345
        assert metadata["profile_note"] == "new"
        assert metadata["access_count"] == 5
        assert metadata["importance"] < 0.8


@pytest.mark.asyncio
async def test_failed_decay_does_not_mark_today_completed(tmp_path):
    engine = SimpleNamespace(apply_daily_decay=AsyncMock(return_value=-1), config={})
    scheduler = DecayScheduler(engine, 0.1, str(tmp_path), backup_enabled=False)
    scheduler._set_last_decay_date = AsyncMock()
    assert await scheduler._execute_decay() is False
    scheduler._set_last_decay_date.assert_not_awaited()


@pytest.mark.asyncio
async def test_migration_backup_failure_prevents_migration(tmp_path):
    initializer = PluginInitializer(Mock(), ConfigManager(), str(tmp_path))
    initializer.db_migration = SimpleNamespace(
        needs_migration=AsyncMock(return_value=True),
        create_backup=AsyncMock(return_value=None),
        migrate=AsyncMock(),
    )
    with pytest.raises(InitializationError, match="备份失败"):
        await initializer._check_and_migrate_database()
    initializer.db_migration.migrate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_failed_migration_restores_backup_and_stops(tmp_path, raises):
    initializer = PluginInitializer(Mock(), ConfigManager(), str(tmp_path))
    initializer.db_migration = SimpleNamespace(
        needs_migration=AsyncMock(return_value=True),
        create_backup=AsyncMock(return_value="before.db"),
        migrate=AsyncMock(side_effect=RuntimeError("broken") if raises else None,
                          return_value={"success": False, "message": "broken"}),
    )
    initializer._restore_db_backup = AsyncMock(return_value=True)
    with pytest.raises(InitializationError, match="已恢复"):
        await initializer._check_and_migrate_database()
    initializer._restore_db_backup.assert_awaited_once_with("before.db")


@pytest.mark.asyncio
async def test_restore_sqlite_backup_with_open_destination_connection(tmp_path):
    database = tmp_path / "migration.db"
    backup = tmp_path / "before.db"
    async with aiosqlite.connect(database) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY, text TEXT)")
        await db.execute("INSERT INTO memories VALUES(1, 'original')")
        await db.commit()
        async with aiosqlite.connect(backup) as saved:
            await db.backup(saved)
        await db.execute("ALTER TABLE memories ADD COLUMN damaged TEXT")
        await db.execute("UPDATE memories SET text='changed'")
        await db.commit()
        initializer = PluginInitializer(Mock(), ConfigManager(), str(tmp_path))
        initializer.db_migration = SimpleNamespace(db_path=str(database))
        assert await initializer._restore_db_backup(str(backup)) is True
        cursor = await db.execute("SELECT * FROM memories")
        assert tuple(await cursor.fetchone()) == (1, "original")


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_missing_or_empty_backup_never_overwrites_database(tmp_path, empty):
    database = tmp_path / "migration.db"
    backup = tmp_path / "invalid.db"
    if empty:
        backup.touch()
    async with aiosqlite.connect(database) as db:
        await db.execute("CREATE TABLE memories(text TEXT)")
        await db.execute("INSERT INTO memories VALUES('safe')")
        await db.commit()
        initializer = PluginInitializer(Mock(), ConfigManager(), str(tmp_path))
        initializer.db_migration = SimpleNamespace(db_path=str(database))
        assert await initializer._restore_db_backup(str(backup)) is False
        cursor = await db.execute("SELECT text FROM memories")
        assert (await cursor.fetchone())[0] == "safe"
    assert backup.exists() is empty


class StreamProvider:
    provider_config = {"id": "stream-test"}

    def __init__(self, *, error="Non-stream requests are not supported", final=True, fail=False):
        self.text_chat = AsyncMock(side_effect=RuntimeError(error))
        self.stream_count = 0
        self.final = final
        self.fail = fail

    async def text_chat_stream(self, **kwargs):
        self.stream_count += 1
        yield SimpleNamespace(is_chunk=True, completion_text="partial")
        if self.final:
            yield SimpleNamespace(is_chunk=False, completion_text='{"summary":"complete"}')
        if self.fail:
            raise RuntimeError("stream interrupted")


@pytest.mark.asyncio
async def test_stream_fallback_is_remembered_only_after_complete_response():
    provider = StreamProvider()
    processor = MemoryProcessor(llm_provider=provider)
    for _ in range(2):
        assert await processor._call_llm_with_retry("p", "s", 1) == '{"summary":"complete"}'
    assert provider.text_chat.await_count == 1
    assert provider.stream_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("final,fail", [(False, False), (True, True)])
async def test_partial_or_interrupted_stream_rejected(final, fail):
    processor = MemoryProcessor(llm_provider=StreamProvider(final=final, fail=fail))
    with pytest.raises(RuntimeError):
        await processor._call_llm_with_retry("p", "s", 1)
    assert not processor._stream_only_providers


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,error", [
    ("never", "Non-stream requests are not supported"),
    ("auto", "non-stream request failed: unauthorized"),
])
async def test_stream_fallback_does_not_mask_other_errors(mode, error):
    provider = StreamProvider(error=error)
    processor = MemoryProcessor(llm_provider=provider, config={"llm_stream_mode": mode})
    with pytest.raises(RuntimeError):
        await processor._call_llm_with_retry("p", "s", 1)
    assert provider.stream_count == 0


def test_budgets_keep_markers_and_valid_fake_tool_json():
    memories = [
        {"id": i, "content": 'very long "quoted" content ' * 3000,
         "metadata": {"importance": 0.8, "key_facts": ["fact" * 5000]}}
        for i in range(20)
    ]
    rendered = format_memories_for_injection(memories)
    assert len(rendered) <= 6000
    assert rendered.startswith("<Anamnesis-Memory>")
    assert rendered.endswith("</Anamnesis-Memory>")
    fake = format_memories_for_fake_tool_call(memories, "query")
    result = json.loads(fake[1]["content"])
    assert len(fake[1]["content"]) <= 6000
    assert result["count"] == len(result["results"])
    assert result["results"][0]["id"] == 0


def test_tiny_budget_skips_and_zero_disables_both_limits():
    memory = [{"id": 1, "content": "x" * 9000, "metadata": {}}]
    assert format_memories_for_injection(memory, max_chars=5) == ""
    assert format_memories_for_fake_tool_call(memory, "q", max_chars=5) == []
    assert "x" * 9000 in format_memories_for_injection(
        memory, max_chars=0, entry_max_chars=0
    )
