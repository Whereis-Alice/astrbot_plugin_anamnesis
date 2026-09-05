"""记忆整合的规模上限回归测试（Bug F / Bug G / 可配置冷却间隔）。

- Bug F：候选查询原来无 LIMIT，`fetchall()` 会把全表读进内存。
- Bug G：语义聚类的并查集传递闭包会把弱相关记忆雪球成一个巨大组，
  且 `merge_memories` 对组大小与输入长度均无上限。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from astrbot_plugin_anamnesis.core.managers.consolidation_manager import (
    MemoryConsolidationManager,
)
from astrbot_plugin_anamnesis.core.processors import memory_processor_build
from astrbot_plugin_anamnesis.core.processors.memory_processor import MemoryProcessor

BASE_SECTION: dict[str, Any] = {
    "enabled": True,
    "trigger": "daily",
    "granularity": "session",
    "keep_original": "archive",
    "min_memories_per_group": 2,
    "min_age_days": 7,
    "max_importance": 0.5,
    "max_groups_per_run": 5,
    "semantic_similarity_threshold": 0.7,
}


class _StubConfigManager:
    """轻量配置管理器桩。

    真实 ConfigManager 会丢弃未在 pydantic 模型里声明的键，
    新增的 max_candidates / max_group_size / min_run_interval_hours
    需要主代理补进 schema，本地测试用桩直接注入。
    """

    def __init__(self, **overrides: Any) -> None:
        self._section = {**BASE_SECTION, **overrides}

    def get_section(self, name: str) -> dict[str, Any]:
        if name != "memory_consolidation":
            return {}
        return dict(self._section)


class _FakeCursor:
    def __init__(self, rows: list[sqlite3.Row]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[sqlite3.Row]:
        return self._rows


class _FakeAsyncDB:
    """用同步 sqlite3 承载真实 SQL，既验证 SQL 合法性也验证 LIMIT/ORDER BY 语义。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FakeCursor:
        self.statements.append((sql, tuple(params)))
        return _FakeCursor(self._conn.execute(sql, params).fetchall())


class _FakeEngine:
    def __init__(self, db: _FakeAsyncDB) -> None:
        self.db_connection = db

    @staticmethod
    def _safe_json_dict(raw: Any) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}


def _make_db(rows: list[tuple[str, dict[str, Any]]]) -> _FakeAsyncDB:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE documents ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, metadata TEXT)"
    )
    for text, metadata in rows:
        conn.execute(
            "INSERT INTO documents (text, metadata) VALUES (?, ?)",
            (text, json.dumps(metadata)),
        )
    conn.commit()
    return _FakeAsyncDB(conn)


def _old_rows(count: int) -> list[tuple[str, dict[str, Any]]]:
    old = time.time() - 40 * 86400
    return [
        (
            f"m{i}",
            {
                "status": "active",
                "importance": round(0.05 + i * 0.01, 4),
                "create_time": old + i,
                "session_id": "s1",
            },
        )
        for i in range(1, count + 1)
    ]


class TestQueryCandidatesLimit:
    @pytest.mark.asyncio
    async def test_applies_max_candidates_limit_and_deterministic_order(self):
        rows = _old_rows(25)
        old = time.time() - 40 * 86400
        # 干扰项：重要度过高 / 太新 / 已归档，必须仍被 WHERE 过滤
        rows.append(("high", {"status": "active", "importance": 0.9, "create_time": old}))
        rows.append(
            ("fresh", {"status": "active", "importance": 0.1, "create_time": time.time()})
        )
        rows.append(
            ("archived", {"status": "archived", "importance": 0.1, "create_time": old})
        )
        db = _make_db(rows)
        mgr = MemoryConsolidationManager(
            _FakeEngine(db), Mock(), _StubConfigManager(max_candidates=10)
        )

        candidates = await mgr._query_candidates(mgr.config)

        assert len(candidates) == 10
        # 按 importance 升序取最适合整合的候选，结果稳定可复现
        assert [c["content"] for c in candidates] == [f"m{i}" for i in range(1, 11)]
        sql, params = db.statements[0]
        assert "LIMIT" in sql.upper()
        assert "ORDER BY" in sql.upper()
        assert 10 in params

    @pytest.mark.asyncio
    async def test_falls_back_to_default_limit(self):
        db = _make_db(_old_rows(25))
        mgr = MemoryConsolidationManager(_FakeEngine(db), Mock(), _StubConfigManager())

        candidates = await mgr._query_candidates(mgr.config)

        assert len(candidates) == 25
        assert 500 in db.statements[0][1]

    @pytest.mark.asyncio
    async def test_invalid_max_candidates_falls_back(self):
        db = _make_db(_old_rows(3))
        mgr = MemoryConsolidationManager(
            _FakeEngine(db), Mock(), _StubConfigManager(max_candidates="oops")
        )

        candidates = await mgr._query_candidates(mgr.config)

        assert len(candidates) == 3
        assert 500 in db.statements[0][1]


class TestGroupSizeLimit:
    def _semantic_manager(self, pairs, **overrides):
        engine = Mock()
        engine.vector_retriever = Mock()
        engine.vector_retriever.find_similar_pairs = AsyncMock(return_value=pairs)
        config = _StubConfigManager(granularity="semantic", **overrides)
        return MemoryConsolidationManager(engine, Mock(), config)

    @pytest.mark.asyncio
    async def test_transitive_snowball_is_split(self):
        candidates = [
            {"id": i, "content": f"m{i}", "metadata": {"session_id": "s1"}}
            for i in range(1, 21)
        ]
        # 链式相似：1-2, 2-3, ... 19-20，并查集传递闭包会串成一个 20 条大组
        pairs = [(i, i + 1, 0.71) for i in range(1, 20)]
        mgr = self._semantic_manager(pairs, max_group_size=10)

        raw_groups = await mgr._group_semantic(candidates, mgr.config)
        assert [len(g) for g in raw_groups] == [20]

        groups = await mgr._build_groups(candidates, mgr.config)

        assert groups
        assert all(len(g) <= 10 for g in groups)
        # 超限组必须被切分而不是丢弃：候选一条都不能少
        assert sum(len(g) for g in groups) == 20
        assert {mem["id"] for group in groups for mem in group} == set(range(1, 21))

    @pytest.mark.asyncio
    async def test_default_max_group_size_is_ten(self):
        candidates = [
            {"id": i, "content": f"m{i}", "metadata": {"session_id": "s1"}}
            for i in range(1, 32)
        ]
        pairs = [(i, i + 1, 0.71) for i in range(1, 31)]
        mgr = self._semantic_manager(pairs)

        groups = await mgr._build_groups(candidates, mgr.config)

        assert all(len(g) <= 10 for g in groups)
        assert sum(len(g) for g in groups) == 31

    @pytest.mark.asyncio
    async def test_session_groups_are_split_too(self):
        candidates = [
            {"id": i, "content": f"m{i}", "metadata": {"session_id": "s1"}}
            for i in range(1, 4)
        ]
        mgr = MemoryConsolidationManager(
            Mock(), Mock(), _StubConfigManager(max_group_size=2)
        )

        groups = await mgr._build_groups(candidates, mgr.config)

        assert sorted(len(g) for g in groups) == [1, 2]
        assert sum(len(g) for g in groups) == 3

    @pytest.mark.asyncio
    async def test_invalid_max_group_size_falls_back_to_default(self):
        candidates = [
            {"id": i, "content": f"m{i}", "metadata": {"session_id": "s1"}}
            for i in range(1, 15)
        ]
        mgr = MemoryConsolidationManager(
            Mock(), Mock(), _StubConfigManager(max_group_size="nope")
        )

        groups = await mgr._build_groups(candidates, mgr.config)

        assert all(len(g) <= 10 for g in groups)
        assert sum(len(g) for g in groups) == 14


class TestMinRunInterval:
    def test_default_is_six_hours(self):
        mgr = MemoryConsolidationManager(Mock(), Mock(), _StubConfigManager())
        assert mgr._min_run_interval == pytest.approx(6 * 3600.0)

    def test_reads_configured_hours(self):
        mgr = MemoryConsolidationManager(
            Mock(), Mock(), _StubConfigManager(min_run_interval_hours=1.5)
        )
        assert mgr._min_run_interval == pytest.approx(1.5 * 3600.0)

    def test_invalid_value_falls_back(self):
        mgr = MemoryConsolidationManager(
            Mock(), Mock(), _StubConfigManager(min_run_interval_hours="soon")
        )
        assert mgr._min_run_interval == pytest.approx(6 * 3600.0)

    def test_broken_config_manager_falls_back(self):
        mgr = MemoryConsolidationManager(Mock(), Mock(), Mock())
        assert mgr._min_run_interval == pytest.approx(6 * 3600.0)

    @pytest.mark.asyncio
    async def test_cooldown_respects_configured_interval(self):
        mgr = MemoryConsolidationManager(
            Mock(), Mock(), _StubConfigManager(min_run_interval_hours=24)
        )
        mgr._last_run_at = time.time()

        result = await mgr.run_consolidation(force=False)

        assert result.get("reason") == "cooldown"

    @pytest.mark.asyncio
    async def test_zero_interval_disables_cooldown(self):
        mgr = MemoryConsolidationManager(
            Mock(), Mock(), _StubConfigManager(min_run_interval_hours=0)
        )
        mgr._query_candidates = AsyncMock(return_value=[])
        mgr._last_run_at = time.time()

        result = await mgr.run_consolidation(force=False)

        assert result == {"candidates": 0, "groups": 0, "merged": 0}


def _content_chars(item: dict[str, Any]) -> int:
    return (
        len(str(item["summary"]))
        + sum(len(str(f)) for f in item["key_facts"])
        + sum(len(str(t)) for t in item["topics"])
    )


class TestMergeInputBudget:
    def test_default_budget_constant(self):
        assert MemoryProcessor.MERGE_INPUT_CHAR_BUDGET == 12000

    def test_build_merge_items_truncates_to_budget(self):
        processor = MemoryProcessor(
            llm_provider=Mock(), context=None, config={"merge_input_char_budget": 2000}
        )
        memories = [
            {"id": i, "content": "长" * 3000, "metadata": {}} for i in range(1, 11)
        ]

        items, included, skipped = processor._build_merge_items(memories)

        assert sum(_content_chars(item) for item in items) <= 2000
        assert items, "至少要保留一条记忆送进 LLM"
        # 不静默丢失：纳入 + 跳过 == 全部输入
        assert sorted(included + skipped) == list(range(1, 11))

    def test_small_group_is_not_truncated(self):
        processor = MemoryProcessor(llm_provider=Mock(), context=None)
        memories = [
            {"id": 1, "content": "x", "metadata": {"persona_summary": "记忆一"}},
            {"id": 2, "content": "y", "metadata": {"persona_summary": "记忆二"}},
        ]

        items, included, skipped = processor._build_merge_items(memories)

        assert [item["summary"] for item in items] == ["记忆一", "记忆二"]
        assert included == [1, 2]
        assert skipped == []

    @pytest.mark.asyncio
    async def test_merge_memories_bounds_prompt_and_warns(self):
        processor = MemoryProcessor(
            llm_provider=Mock(), context=None, config={"merge_input_char_budget": 2000}
        )
        captured: dict[str, str] = {}

        async def fake_llm(prompt: str, system_prompt: str) -> str:
            captured["prompt"] = prompt
            return '{"summary": "ok", "key_facts": [], "topics": [], "importance": 0.5}'

        processor._call_llm_with_retry = fake_llm
        memories = [
            {"id": i, "content": "长" * 3000, "metadata": {}} for i in range(1, 11)
        ]

        with patch.object(memory_processor_build, "logger") as fake_logger:
            result = await processor.merge_memories(memories)

        assert result["summary"] == "ok"
        # 未加预算保护时 prompt 会超过 30000 字符
        assert len(captured["prompt"]) < 8000
        assert fake_logger.warning.called
        assert sorted(result["merged_ids"] + result["skipped_ids"]) == list(
            range(1, 11)
        )

    @pytest.mark.asyncio
    async def test_merge_memories_reports_ids_for_normal_group(self):
        processor = MemoryProcessor(llm_provider=Mock(), context=None)
        processor._call_llm_with_retry = AsyncMock(
            return_value='{"summary": "ok", "key_facts": [], "topics": [], "importance": 0.5}'
        )

        result = await processor.merge_memories(
            [
                {"id": 7, "content": "x", "metadata": {}},
                {"id": 9, "content": "y", "metadata": {}},
            ]
        )

        assert result["merged_ids"] == [7, 9]
        assert result["skipped_ids"] == []


class TestConsolidateGroupHonorsSkippedIds:
    @pytest.mark.asyncio
    async def test_skipped_memories_are_not_archived(self):
        engine = Mock()
        engine.add_memory = AsyncMock(return_value=100)
        engine.archive_memories = AsyncMock(return_value=2)
        processor = Mock()
        processor.merge_memories = AsyncMock(
            return_value={
                "summary": "merged",
                "key_facts": [],
                "topics": [],
                "importance": 0.4,
                "merged_ids": [1, 2],
                "skipped_ids": [3],
            }
        )
        mgr = MemoryConsolidationManager(engine, processor, _StubConfigManager())
        group = [
            {"id": i, "content": f"m{i}", "metadata": {"session_id": "s1"}}
            for i in (1, 2, 3)
        ]

        result = await mgr._consolidate_group(group, mgr.config)

        engine.archive_memories.assert_awaited_once_with([1, 2])
        assert result["merged"] == 2
        metadata = engine.add_memory.await_args.kwargs["metadata"]
        assert metadata["consolidated_from"] == [1, 2]