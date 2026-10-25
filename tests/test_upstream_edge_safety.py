"""Exercise transaction, cancellation and formatting edges of adapted fixes."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.base.constants import (
    MEMORY_INJECTION_FOOTER,
    MEMORY_INJECTION_HEADER,
)
from astrbot_plugin_anamnesis.core.managers.memory_engine_crud import (
    MemoryEngineCrudMixin,
)
from astrbot_plugin_anamnesis.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_anamnesis.core.schedulers.decay_scheduler import DecayScheduler
from astrbot_plugin_anamnesis.core.utils.formatting import (
    format_memories_for_fake_tool_call,
    format_memories_for_injection,
)


async def _decay_engine(connection, metadata_rows, config=None):
    connection.row_factory = aiosqlite.Row
    await connection.execute(
        "CREATE TABLE documents(id INTEGER PRIMARY KEY, metadata TEXT)"
    )
    await connection.executemany(
        "INSERT INTO documents VALUES(?, ?)",
        [(index, json.dumps(value)) for index, value in enumerate(metadata_rows, 1)],
    )
    await connection.commit()
    engine = MemoryEngineCrudMixin()
    engine.db_connection = connection
    engine.config = config or {}
    engine._safe_json_dict = json.loads
    engine._invalidate_search_cache = Mock()
    return engine


async def _stored_metadata(connection):
    cursor = await connection.execute("SELECT id, metadata FROM documents ORDER BY id")
    return {row["id"]: json.loads(row["metadata"]) for row in await cursor.fetchall()}


@pytest.mark.asyncio
async def test_decay_mid_batch_failure_rolls_back_every_row():
    originals = [
        {"importance": 0.8, "access_count": 2, "note": "first"},
        {"importance": 0.7, "access_count": 4, "note": "second"},
    ]
    async with aiosqlite.connect(":memory:") as connection:
        engine = await _decay_engine(connection, originals)
        await connection.execute(
            "CREATE TRIGGER reject_second BEFORE UPDATE ON documents "
            "WHEN NEW.id = 2 BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
        await connection.commit()

        assert await engine.apply_daily_decay(0.1) == -1
        assert await _stored_metadata(connection) == dict(enumerate(originals, 1))
        assert connection.in_transaction is False
        engine._invalidate_search_cache.assert_not_called()

        # A retry after removing the failure must decay each record only once.
        await connection.execute("DROP TRIGGER reject_second")
        await connection.commit()
        assert await engine.apply_daily_decay(0.1) == 2
        stored = await _stored_metadata(connection)
        assert stored[1]["importance"] == pytest.approx(0.724)
        assert stored[1]["access_count"] == 1
        assert stored[2]["importance"] == pytest.approx(0.637)
        assert stored[2]["access_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["not-a-number", "NaN", "Infinity", None, [], {}])
async def test_decay_invalid_numeric_metadata_uses_finite_defaults(invalid):
    async with aiosqlite.connect(":memory:") as connection:
        engine = await _decay_engine(connection, [{
            "importance": invalid,
            "access_count": invalid,
            "last_access_time": invalid,
            "profile_note": "preserve me",
        }])

        assert await engine.apply_daily_decay(0.1) == 1
        stored = (await _stored_metadata(connection))[1]
        assert stored["importance"] == pytest.approx(0.45)
        assert stored["access_count"] == 0
        assert stored["last_access_time"] == invalid
        assert stored["profile_note"] == "preserve me"


@pytest.mark.asyncio
async def test_decay_rechecks_protection_and_reports_actual_updated_rows(monkeypatch):
    originals = [
        {"importance": 0.8, "access_count": 2},
        {"importance": 0.8, "access_count": 2},
        {"importance": 0.95, "access_count": 10},
    ]
    async with aiosqlite.connect(":memory:") as connection:
        engine = await _decay_engine(
            connection, originals, {"protected_importance_threshold": 0.9}
        )
        real_to_thread = asyncio.to_thread

        async def protect_after_read(function, *args, **kwargs):
            await connection.execute(
                "UPDATE documents SET metadata=json_set(metadata, '$.importance', .99) "
                "WHERE id=2"
            )
            await connection.commit()
            return await real_to_thread(function, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", protect_after_read)
        assert await engine.apply_daily_decay(0.1) == 1
        stored = await _stored_metadata(connection)
        assert stored[1]["importance"] == pytest.approx(0.724)
        assert stored[2] == {"importance": 0.99, "access_count": 2}
        assert stored[3] == originals[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_decay_task_cancellation_waits_for_release(monkeypatch, cancel_count):
    async with aiosqlite.connect(":memory:") as connection:
        engine = await _decay_engine(
            connection, [{"importance": 0.8, "access_count": 2}]
        )
        reached_release = asyncio.Event()
        allow_release = asyncio.Event()
        real_execute = connection.execute

        async def gate_release(sql, *args, **kwargs):
            if sql == "RELEASE SAVEPOINT anam_daily_decay":
                reached_release.set()
                await allow_release.wait()
            return await real_execute(sql, *args, **kwargs)

        monkeypatch.setattr(connection, "execute", gate_release)
        task = asyncio.create_task(engine.apply_daily_decay(0.1))
        try:
            await asyncio.wait_for(reached_release.wait(), 2)
            for _ in range(cancel_count):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
            allow_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert connection.in_transaction is False
            stored = (await _stored_metadata(connection))[1]
            assert stored == {"importance": 0.724, "access_count": 1}
            engine._invalidate_search_cache.assert_called_once()
        finally:
            allow_release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["apply", "date"])
async def test_scheduler_cancel_records_committed_day_and_does_not_repeat(tmp_path, stage):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def apply_decay(*args):
        if stage == "apply":
            entered.set()
            await release.wait()
        return 1

    engine = SimpleNamespace(
        apply_daily_decay=AsyncMock(side_effect=apply_decay),
        cleanup_old_memories=AsyncMock(),
        maintain_storage=AsyncMock(),
        config={},
    )
    scheduler = DecayScheduler(engine, 0.1, str(tmp_path), backup_enabled=False)
    real_set_date = scheduler._set_last_decay_date

    async def record_date(date):
        if stage == "date":
            entered.set()
            await release.wait()
        await real_set_date(date)

    scheduler._set_last_decay_date = record_date
    task = asyncio.create_task(scheduler._execute_decay())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        restarted = DecayScheduler(engine, 0.1, str(tmp_path), backup_enabled=False)
        assert await restarted._get_last_decay_date() == restarted._get_today_str()
        await restarted._check_and_execute()
        engine.apply_daily_decay.assert_awaited_once_with(0.1, 1)
        engine.cleanup_old_memories.assert_not_awaited()
        engine.maintain_storage.assert_not_awaited()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class _StreamingProvider:
    provider_config = {"id": "edge-stream"}

    def __init__(self, error="This model only supports streaming requests", cancel=None):
        self.text_chat = AsyncMock(side_effect=RuntimeError(error))
        self.stream_calls = []
        self.cancel = cancel

    async def text_chat_stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        yield SimpleNamespace(is_chunk=True, completion_text="partial fragment")
        if self.cancel == "before_final":
            raise asyncio.CancelledError()
        yield SimpleNamespace(is_chunk=False, completion_text='{"summary":"complete"}')
        if self.cancel == "after_final":
            raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_always_stream_uses_complete_response_without_non_stream_request():
    provider = _StreamingProvider()
    processor = MemoryProcessor(
        llm_provider=provider, config={"llm_stream_mode": "always"}
    )
    assert await processor._call_llm_with_retry("prompt", "system", 1) == (
        '{"summary":"complete"}'
    )
    provider.text_chat.assert_not_awaited()
    assert provider.stream_calls == [{"prompt": "prompt", "system_prompt": "system"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    "This model only supports streaming requests",
    "stream=true is required for this model",
    "当前模型仅支持流式调用",
])
async def test_explicit_stream_only_errors_fall_back_and_are_remembered(error):
    provider = _StreamingProvider(error)
    processor = MemoryProcessor(llm_provider=provider)
    for _ in range(2):
        assert await processor._call_llm_with_retry("p", "s", 1) == (
            '{"summary":"complete"}'
        )
    assert provider.text_chat.await_count == 1
    assert len(provider.stream_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["non_stream", "before_final", "after_final"])
async def test_cancelled_llm_calls_do_not_retry_or_accept_partial_response(
    monkeypatch, cancel
):
    provider = _StreamingProvider(cancel=cancel)
    if cancel == "non_stream":
        provider.text_chat.side_effect = asyncio.CancelledError()
    processor = MemoryProcessor(llm_provider=provider)
    retry_sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", retry_sleep)

    with pytest.raises(asyncio.CancelledError):
        await processor._call_llm_with_retry("p", "s", 3)
    assert provider.text_chat.await_count == 1
    assert len(provider.stream_calls) == (0 if cancel == "non_stream" else 1)
    assert not processor._stream_only_providers
    retry_sleep.assert_not_awaited()


def test_oversized_custom_framing_falls_back_within_budget(monkeypatch):
    from astrbot_plugin_anamnesis.core.prompts import prompt_manager

    manager = SimpleNamespace(get_prompt=lambda key: f"custom-{key}" * 1000)
    monkeypatch.setattr(prompt_manager, "get_prompt_manager", lambda: manager)
    memory = [{"id": 1, "content": "user likes tea", "metadata": {}}]
    rendered = format_memories_for_injection(memory, max_chars=1500)
    assert len(rendered) <= 1500
    assert rendered.startswith(MEMORY_INJECTION_HEADER)
    assert rendered.endswith(MEMORY_INJECTION_FOOTER)
    assert "user likes tea" in rendered
    assert "custom-memory_injection" not in rendered
    assert format_memories_for_injection(memory, max_chars=100) == ""


def test_fake_tool_budget_handles_escaped_text_and_oversized_fixed_fields():
    memory = [{
        "id": 1,
        "content": 'quoted "line"\n\t' * 5000,
        "metadata": {"session_id": "group" * 300, "persona_id": "persona" * 300},
    }]
    messages = format_memories_for_fake_tool_call(
        memory, 'quoted "query"\n', max_chars=4200, entry_max_chars=1200
    )
    assert len(messages) == 2
    assert len(messages[1]["content"]) <= 4200
    result = json.loads(messages[1]["content"])
    assert result["count"] == len(result["results"]) == 1
    assert result["results"][0]["id"] == 1
    memory[0]["metadata"]["session_id"] = "group" * 3000
    assert format_memories_for_fake_tool_call(memory, "q", max_chars=6000) == []
