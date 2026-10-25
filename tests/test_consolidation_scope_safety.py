"""Consolidation must preserve scope and a surviving copy of every source."""

from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_anamnesis.core.managers.consolidation_manager import (
    MemoryConsolidationManager,
)


def _memory(memory_id, session="group-a", persona="alice"):
    return {
        "id": memory_id,
        "content": f"memory {memory_id}",
        "metadata": {"session_id": session, "persona_id": persona},
    }


def _manager(granularity="session", skipped_ids=None):
    cfg = {
        "granularity": granularity,
        "min_memories_per_group": 2,
        "max_group_size": 10,
        "keep_original": "archive",
    }
    config_manager = Mock()
    config_manager.get_section.return_value = cfg
    engine = Mock()
    engine.add_memory = AsyncMock(return_value=100)
    engine.delete_memory = AsyncMock(return_value=True)
    engine.archive_memories = AsyncMock(return_value=2)
    engine.batch_delete_memories = AsyncMock(return_value=2)
    engine._safe_json_dict = lambda value: value if isinstance(value, dict) else {}
    processor = Mock()
    processor.merge_memories = AsyncMock(
        return_value={
            "summary": "merged",
            "key_facts": ["fact"],
            "topics": ["topic"],
            "importance": 0.4,
            "skipped_ids": skipped_ids or [],
        }
    )
    return MemoryConsolidationManager(engine, processor, config_manager), cfg


@pytest.mark.asyncio
@pytest.mark.parametrize("granularity", ["session", "semantic"])
async def test_both_granularities_separate_sessions_and_personas(granularity):
    mgr, cfg = _manager(granularity)
    candidates = [
        _memory(1),
        _memory(2),
        _memory(3, persona="bob"),
        _memory(4, persona="bob"),
        _memory(5, session="group-b"),
        _memory(6, session="group-b"),
        _memory(7, session=None),
        _memory(8, session=None),
    ]
    # A semantic similarity chain may join all candidates before scope splitting.
    mgr._group_semantic = AsyncMock(return_value=[candidates])

    groups = await mgr._build_groups(candidates, cfg)

    assert {tuple(mem["id"] for mem in group) for group in groups} == {
        (1, 2), (3, 4), (5, 6)
    }


@pytest.mark.asyncio
async def test_semantic_result_keeps_scope_and_skipped_sources():
    mgr, cfg = _manager("semantic", skipped_ids=[3])

    result = await mgr._consolidate_group([_memory(i) for i in (1, 2, 3)], cfg)

    kwargs = mgr.memory_engine.add_memory.await_args.kwargs
    assert kwargs["session_id"] == "group-a"
    assert kwargs["persona_id"] == "alice"
    assert kwargs["metadata"]["consolidated_from"] == [1, 2]
    mgr.memory_engine.archive_memories.assert_awaited_once_with([1, 2])
    assert result["merged"] == 2


@pytest.mark.asyncio
async def test_all_skipped_sources_are_preserved():
    mgr, cfg = _manager(skipped_ids=[1, 2])

    with pytest.raises(RuntimeError, match="没有实际参与"):
        await mgr._consolidate_group([_memory(1), _memory(2)], cfg)

    mgr.memory_engine.add_memory.assert_not_awaited()
    mgr.memory_engine.archive_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_scope_group_is_rejected_before_llm_or_write():
    mgr, cfg = _manager()

    with pytest.raises(RuntimeError, match="同一会话"):
        await mgr._consolidate_group([_memory(1), _memory(2, persona="bob")], cfg)

    mgr.memory_processor.merge_memories.assert_not_awaited()
    mgr.memory_engine.add_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_failure_rolls_back_only_when_all_sources_remain_active():
    mgr, cfg = _manager()
    mgr.memory_engine.archive_memories.side_effect = RuntimeError("archive failed")
    mgr.memory_engine.get_memory = AsyncMock(side_effect=[_memory(1), _memory(2)])

    with pytest.raises(RuntimeError, match="archive failed"):
        await mgr._consolidate_group([_memory(1), _memory(2)], cfg)

    mgr.memory_engine.delete_memory.assert_awaited_once_with(100)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_state", ["archived", "missing", "unknown"])
async def test_partial_archive_failure_preserves_merged_copy(source_state):
    mgr, cfg = _manager()
    mgr.memory_engine.archive_memories.side_effect = RuntimeError("archive failed")
    if source_state == "unknown":
        mgr.memory_engine.get_memory = AsyncMock(side_effect=RuntimeError("db failed"))
    else:
        source = _memory(1)
        source["metadata"]["status"] = "archived"
        mgr.memory_engine.get_memory = AsyncMock(
            return_value=source if source_state == "archived" else None
        )

    with pytest.raises(RuntimeError, match="archive failed"):
        await mgr._consolidate_group([_memory(1), _memory(2)], cfg)

    mgr.memory_engine.delete_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_delete_count_does_not_discard_only_remaining_copy():
    mgr, cfg = _manager()
    cfg["keep_original"] = "delete"
    mgr.memory_engine.batch_delete_memories.return_value = 1
    mgr.memory_engine.get_memory = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError, match="清理不完整"):
        await mgr._consolidate_group([_memory(1), _memory(2)], cfg)

    mgr.memory_engine.delete_memory.assert_not_awaited()
