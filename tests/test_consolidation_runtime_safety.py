"""Consolidation never cleans sources from a stale or incomplete result."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.managers.consolidation_manager import (
    MemoryConsolidationManager,
)
from astrbot_plugin_anamnesis.core.processors.memory_processor import MemoryProcessor


def _memory(memory_id):
    return {
        "id": memory_id,
        "content": f"memory {memory_id}",
        "metadata": {
            "session_id": "group-a",
            "persona_id": "alice",
            "status": "active",
            "source_session_id": "group-a",
        },
    }


class _Engine:
    def __init__(self):
        self.documents = {i: _memory(i) for i in (1, 2)}
        self.sources = {i: [] for i in (1, 2)}
        self.added = []
        self.cleaned = []
        self.deleted = []
        self.lock = asyncio.Lock()
        self.add_hook = None
        self.cleanup_hook = None
        self.new_id = 100

    @asynccontextmanager
    async def memory_mutation_guard(self):
        async with self.lock:
            yield

    @staticmethod
    def _safe_json_dict(value):
        return deepcopy(value) if isinstance(value, dict) else {}

    async def get_memory(self, memory_id):
        return deepcopy(self.documents.get(memory_id))

    async def get_memory_source(self, memory_id):
        return deepcopy(self.sources.get(memory_id, []))

    async def add_memory(self, **kwargs):
        self.added.append(deepcopy(kwargs))
        if self.add_hook:
            await self.add_hook()
        if self.new_id is None:
            return None
        self.documents[self.new_id] = {
            "id": self.new_id,
            "content": kwargs["content"],
            "metadata": {**kwargs["metadata"], "status": "active"},
        }
        self.sources[self.new_id] = deepcopy(kwargs.get("source_messages") or [])
        return self.new_id

    async def archive_memories(self, memory_ids):
        self.cleaned.append(list(memory_ids))
        if self.cleanup_hook:
            return await self.cleanup_hook(memory_ids)
        for memory_id in memory_ids:
            self.documents[memory_id]["metadata"]["status"] = "archived"
        return len(memory_ids)

    async def batch_delete_memories(self, memory_ids):
        self.cleaned.append(list(memory_ids))
        if self.cleanup_hook:
            return await self.cleanup_hook(memory_ids)
        for memory_id in memory_ids:
            self.documents.pop(memory_id)
            self.sources.pop(memory_id, None)
        return len(memory_ids)

    async def delete_memory(self, memory_id):
        self.deleted.append(memory_id)
        self.documents.pop(memory_id, None)
        self.sources.pop(memory_id, None)
        return True


def _manager(keep_original="archive"):
    engine = _Engine()
    config = Mock()
    cfg = {
        "enabled": True,
        "min_run_interval_hours": 0,
        "min_memories_per_group": 2,
        "keep_original": keep_original,
    }
    config.get_section.return_value = cfg
    processor = Mock()
    processor.merge_memories = AsyncMock(
        return_value={
            "summary": "merged",
            "key_facts": [],
            "topics": [],
            "importance": 0.4,
        }
    )
    manager = MemoryConsolidationManager(engine, processor, config)
    return manager, engine, cfg


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["archive", "delete", "text", "scope", "persona", "source"]
)
async def test_sources_changed_during_llm_are_preserved(change):
    manager, engine, cfg = _manager()
    group = deepcopy(list(engine.documents.values()))
    merged = manager.memory_processor.merge_memories.return_value

    async def mutate(_group):
        if change == "delete":
            engine.documents.pop(1)
        elif change == "text":
            engine.documents[1]["content"] = "manually corrected"
        elif change == "source":
            engine.sources[1] = [{"id": 1, "content": "corrected source"}]
        else:
            key, value = {
                "archive": ("status", "archived"),
                "scope": ("session_id", "group-b"),
                "persona": ("persona_id", "bob"),
            }[change]
            engine.documents[1]["metadata"][key] = value
        return merged

    manager.memory_processor.merge_memories.side_effect = mutate

    with pytest.raises(RuntimeError, match="来源.*变化|来源.*不存在"):
        await manager._consolidate_group(group, cfg)

    assert not engine.added
    assert not engine.cleaned


@pytest.mark.asyncio
async def test_stale_candidates_are_rejected_before_llm():
    manager, engine, cfg = _manager()
    group = deepcopy(list(engine.documents.values()))
    engine.documents[1]["content"] = "manually corrected"

    with pytest.raises(RuntimeError, match="来源.*变化"):
        await manager._consolidate_group(group, cfg)

    manager.memory_processor.merge_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_consolidation_preserves_and_deduplicates_source_messages():
    manager, engine, cfg = _manager("delete")
    message = {"id": 10, "session_id": "group-a", "role": "user", "content": "original"}
    second = {**message, "id": 11, "content": "second original"}
    engine.sources = {1: [message], 2: [message, second]}
    group = deepcopy(list(engine.documents.values()))

    await manager._consolidate_group(group, cfg)

    assert engine.sources[100] == [message, second]
    assert engine.added[0]["metadata"]["source_session_id"] == "group-a"
    assert engine.added[0]["metadata"]["source_message_ids"] == [10, 11]


@pytest.mark.asyncio
async def test_distinct_source_sessions_in_shared_scope_are_not_consolidated_together():
    manager, engine, cfg = _manager("delete")
    engine.documents[2]["metadata"]["source_session_id"] = "group-b"
    engine.sources = {
        1: [{"id": 10, "session_id": "group-a", "content": "first"}],
        2: [{"id": 10, "session_id": "group-b", "content": "second"}],
    }

    group = deepcopy(list(engine.documents.values()))
    with pytest.raises(RuntimeError, match="同一来源会话"):
        await manager._consolidate_group(group, cfg)

    assert [len(items) for items in manager._group_by_session(group)] == [1, 1]
    assert not engine.added
    assert not engine.cleaned


@pytest.mark.asyncio
async def test_existing_source_message_ids_survive_without_retained_originals():
    manager, engine, cfg = _manager("delete")
    engine.documents[1]["metadata"]["source_message_ids"] = [10, 11]
    engine.documents[2]["metadata"]["source_message_ids"] = [11, 12]

    await manager._consolidate_group(deepcopy(list(engine.documents.values())), cfg)

    assert engine.added[0]["metadata"]["source_message_ids"] == [10, 11, 12]


@pytest.mark.asyncio
async def test_concurrent_forced_runs_do_not_merge_the_same_group_twice():
    manager, engine, _cfg = _manager()
    group = deepcopy(list(engine.documents.values()))
    manager._query_candidates = AsyncMock(return_value=group)
    manager._build_groups = AsyncMock(return_value=[group])
    entered = asyncio.Event()
    release = asyncio.Event()
    merged = manager.memory_processor.merge_memories.return_value

    async def wait_llm(_group):
        entered.set()
        await release.wait()
        return merged

    manager.memory_processor.merge_memories.side_effect = wait_llm
    first = asyncio.create_task(manager.run_consolidation(force=True))
    await entered.wait()
    second = asyncio.create_task(manager.run_consolidation(force=True))
    await asyncio.sleep(0)
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result["merged"] == 2
    assert second_result == {"skipped": True, "reason": "in_progress"}
    manager.memory_processor.merge_memories.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_cleanup_cancellation_retains_a_copy_of_every_source(partial):
    manager, engine, cfg = _manager("delete")
    group = deepcopy(list(engine.documents.values()))

    async def cancel_cleanup(memory_ids):
        if partial:
            engine.documents.pop(memory_ids[0])
        raise asyncio.CancelledError()

    engine.cleanup_hook = cancel_cleanup

    with pytest.raises(asyncio.CancelledError):
        await manager._consolidate_group(group, cfg)

    if partial:
        assert 100 in engine.documents
        assert not engine.deleted
    else:
        assert 100 not in engine.documents
        assert engine.deleted == [100]


@pytest.mark.asyncio
async def test_missing_new_id_never_cleans_sources():
    manager, engine, cfg = _manager()
    engine.new_id = None

    with pytest.raises(RuntimeError, match="ID"):
        await manager._consolidate_group(deepcopy(list(engine.documents.values())), cfg)

    assert not engine.cleaned


@pytest.mark.asyncio
async def test_access_updates_do_not_invalidate_the_source_snapshot():
    manager, engine, cfg = _manager()
    group = deepcopy(list(engine.documents.values()))
    merged = manager.memory_processor.merge_memories.return_value

    async def recall(_group):
        engine.documents[1]["metadata"].update(last_access_time=123, access_count=2)
        return merged

    manager.memory_processor.merge_memories.side_effect = recall

    result = await manager._consolidate_group(group, cfg)

    assert result["merged"] == 2


@pytest.mark.asyncio
async def test_truncated_input_sources_are_not_cleaned():
    manager, engine, cfg = _manager("delete")
    manager.memory_processor.merge_memories.return_value["truncated_ids"] = [2]

    result = await manager._consolidate_group(
        deepcopy(list(engine.documents.values())), cfg
    )

    assert engine.cleaned == [[1]]
    assert 2 in engine.documents
    assert result["merged"] == 1


@pytest.mark.asyncio
async def test_all_truncated_sources_do_not_create_a_redundant_result():
    manager, engine, cfg = _manager()
    manager.memory_processor.merge_memories.return_value["truncated_ids"] = [1, 2]

    with pytest.raises(RuntimeError, match="完整参与"):
        await manager._consolidate_group(deepcopy(list(engine.documents.values())), cfg)

    assert not engine.added
    assert not engine.cleaned


@pytest.mark.asyncio
async def test_processor_reports_truncated_input_ids():
    processor = MemoryProcessor(
        llm_provider=Mock(), context=None, config={"merge_input_char_budget": 2000}
    )
    processor._call_llm_with_retry = AsyncMock(return_value='{"summary":"ok"}')

    result = await processor.merge_memories(
        [
            {"id": 1, "content": "x" * 3000, "metadata": {}},
            {"id": 2, "content": "short", "metadata": {}},
        ]
    )

    assert result["truncated_ids"] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        '{"summary":null}',
        '{"summary":42}',
        '{"summary":{"partial":"fact"}}',
        '{"summary":"truncated',
        '{"summary":"looks complete","key_facts":["partial',
    ],
)
async def test_incomplete_llm_summary_is_rejected(response):
    processor = MemoryProcessor(llm_provider=Mock(), context=None)
    processor._call_llm_with_retry = AsyncMock(return_value=response)

    with pytest.raises(RuntimeError):
        await processor.merge_memories(
            [{"id": 1, "content": "original", "metadata": {}}]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_llm", "after_llm"])
@pytest.mark.parametrize("status", ["pending", "needs_repair"])
async def test_unfinished_source_writes_are_not_consolidated(tmp_path, phase, status):
    manager, engine, cfg = _manager()
    async with aiosqlite.connect(tmp_path / "memory.db") as db:
        engine.db_connection = db
        await db.execute(
            "CREATE TABLE memory_write_ops(memory_id INTEGER, status TEXT)"
        )

        async def add_pending():
            await db.execute("INSERT INTO memory_write_ops VALUES (1, ?)", (status,))
            await db.commit()

        if phase == "before_llm":
            await add_pending()
        else:
            merged = manager.memory_processor.merge_memories.return_value

            async def merge(_group):
                await add_pending()
                return merged

            manager.memory_processor.merge_memories.side_effect = merge

        with pytest.raises(RuntimeError, match="写入.*未完成"):
            await manager._consolidate_group(
                deepcopy(list(engine.documents.values())), cfg
            )

        assert not engine.added
        assert not engine.cleaned
        if phase == "before_llm":
            manager.memory_processor.merge_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_cancellation_waits_for_final_write_to_finish():
    manager, engine, cfg = _manager("delete")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause_add():
        assert engine.lock.locked()
        entered.set()
        await release.wait()

    engine.add_hook = pause_add
    task = asyncio.create_task(
        manager._consolidate_group(deepcopy(list(engine.documents.values())), cfg)
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert 100 in engine.documents
    assert 1 not in engine.documents and 2 not in engine.documents
    assert not engine.lock.locked()
