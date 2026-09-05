"""图子系统体积治理测试：关系条目配额、残留行清理与跨记忆共享边保护。"""

from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.managers.graph_memory_manager import (
    GraphMemoryManager,
)
from astrbot_plugin_anamnesis.core.models.graph_models import (
    ExtractedGraph,
    GraphEdge,
    GraphEntry,
    GraphNode,
)
from astrbot_plugin_anamnesis.core.processors.graph_extractor import GraphExtractor
from astrbot_plugin_anamnesis.storage.graph_store import GraphStore

# 2 主题 x 2 事实 = 4 条 describes(0.82)，3 参与者 x 2 事实 = 6 条 mentioned_in(0.88)，
# C(3,2) = 3 条 co_occurs_with(0.7)；合计 13 条关系条目 + 7 条非关系条目 = 20 条。
_METADATA = {
    "session_id": "test:private:s1",
    "persona_id": "persona_1",
    "importance": 0.8,
    "create_time": 1.0,
    "last_access_time": 1.0,
    "canonical_summary": "三人开会确认了两条结论",
    "topics": ["季度会议", "预算调整"],
    "participants": ["张三", "李四", "王五"],
    "key_facts": ["预算上调百分之十", "下周五复盘"],
}

_EXPECTED_EDGE_ENTRIES = 13
_EXPECTED_PLAIN_ENTRIES = 7


def _extract(limit: int | None = None, memory_id: int = 1, **overrides):
    config = {} if limit is None else {"max_edge_entries_per_memory": limit}
    metadata = {**_METADATA, **overrides}
    extractor = GraphExtractor(config)
    return extractor.extract(memory_id, metadata["canonical_summary"], metadata)


def _edge_entries(graph: ExtractedGraph) -> list[GraphEntry]:
    return [entry for entry in graph.entries if entry.entry_type == "edge"]


def _relation_counts(items) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.relation_type] = counts.get(item.relation_type, 0) + 1
    return counts


async def _seed_memory(
    store: GraphStore,
    memory_id: int,
    *,
    facts: list[str] | None = None,
) -> dict[str, int]:
    """把一条记忆的图结构写进 store，返回 node_key -> node_id。"""
    overrides = {} if facts is None else {"key_facts": facts}
    graph = _extract(memory_id=memory_id, **overrides)
    node_map = await store.upsert_nodes(graph.nodes)
    edge_map = await store.add_edges(graph.edges, node_map)
    await store.add_entries(graph.entries, node_map, edge_map)
    return node_map


async def _open_store(tmp_path: Path, name: str) -> GraphStore:
    store = GraphStore(str(tmp_path / name))
    await store.initialize()
    return store


async def _count(db_path: Path, sql: str, params: tuple = ()) -> int:
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


_RESIDUE_ENTRY_ID = 999_002
_RESIDUE_FTS_ENTRY_ID = 999_003
_RESIDUE_MISSING_NODE_ID = 999_001
_RESIDUE_ORPHAN_NODE_KEY = "topic:__anamnesis_orphan__"


async def _inject_residue(db_path: Path, node_id: int, live_memory_id: int) -> None:
    """人为制造五类残留行，每类各一条，且互不触发级联删除。"""
    edge_columns = (
        "edge_key, source_node_id, target_node_id, relation_type, source_memory_id,"
        " weight, confidence, status, metadata, created_at, updated_at"
    )
    stamp = "2020-01-01T00:00:00"
    async with aiosqlite.connect(str(db_path)) as db:
        # 故意关掉外键，才能写出真实故障时才会出现的破损行。
        await db.execute("PRAGMA foreign_keys=OFF")
        # 1) 所属记忆完全没有条目的边（分批写入中途失败的典型残留）
        await db.execute(
            f"INSERT INTO graph_edges({edge_columns})"
            " VALUES(?, ?, ?, ?, ?, 1.0, 0.5, 'active', '{}', ?, ?)",
            ("residue-edge-no-entries", node_id, node_id, "describes", 4242, stamp, stamp),
        )
        # 2) 源节点已消失的边（记忆本身仍有条目，只能被第二步命中）
        await db.execute(
            f"INSERT INTO graph_edges({edge_columns})"
            " VALUES(?, ?, ?, ?, ?, 1.0, 0.5, 'active', '{}', ?, ?)",
            (
                "residue-edge-no-nodes",
                _RESIDUE_MISSING_NODE_ID,
                node_id,
                "describes",
                live_memory_id,
                stamp,
                stamp,
            ),
        )
        # 3) 指向已消失条目的关联行
        await db.execute(
            "INSERT INTO graph_entry_nodes(entry_id, node_id) VALUES(?, ?)",
            (_RESIDUE_ENTRY_ID, node_id),
        )
        # 4) 条目已删除的 FTS 影子行
        await db.execute(
            "INSERT INTO livingmemory_graph_entries_fts(content, entry_id)"
            " VALUES(?, ?)",
            ("残留影子行", _RESIDUE_FTS_ENTRY_ID),
        )
        # 5) 既无条目关联又无边引用的孤儿节点
        await db.execute(
            "INSERT INTO graph_nodes(node_key, node_type, node_value,"
            " canonical_value, metadata, created_at, updated_at)"
            " VALUES(?, 'topic', '孤儿节点', '__anamnesis_orphan__', '{}', ?, ?)",
            (_RESIDUE_ORPHAN_NODE_KEY, stamp, stamp),
        )
        await db.commit()


# --------------------------------------------------------------------------
# 关系条目配额（GraphExtractor._apply_edge_entry_quota）
# --------------------------------------------------------------------------


def test_edge_quota_disabled_by_default_keeps_every_entry():
    """默认 0 = 不限，行为必须和裁剪功能引入之前完全一致。"""
    graph = _extract()
    assert len(_edge_entries(graph)) == _EXPECTED_EDGE_ENTRIES
    assert len(graph.edges) == _EXPECTED_EDGE_ENTRIES
    assert len(graph.entries) == _EXPECTED_EDGE_ENTRIES + _EXPECTED_PLAIN_ENTRIES


@pytest.mark.parametrize("limit", [-5, 0])
def test_edge_quota_non_positive_limit_is_a_no_op(limit: int):
    graph = _extract(limit)
    assert len(_edge_entries(graph)) == _EXPECTED_EDGE_ENTRIES
    assert len(graph.edges) == _EXPECTED_EDGE_ENTRIES


def test_edge_quota_above_actual_count_changes_nothing():
    graph = _extract(_EXPECTED_EDGE_ENTRIES + 100)
    assert len(_edge_entries(graph)) == _EXPECTED_EDGE_ENTRIES
    assert len(graph.edges) == _EXPECTED_EDGE_ENTRIES


def test_edge_quota_keeps_highest_confidence_relations_first():
    """置信度最高的 mentioned_in(0.88) 先保留，共现边(0.7)最先被砍。"""
    graph = _extract(6)
    entries = _edge_entries(graph)
    assert len(entries) == 6
    assert _relation_counts(entries) == {"mentioned_in": 6}
    # 两条抽取路径保持 1:1 顺序，边集合应同步裁剪，不留未被引用的边。
    assert len(graph.edges) == 6
    assert _relation_counts(graph.edges) == {"mentioned_in": 6}


def test_edge_quota_fills_remaining_budget_with_next_tier():
    graph = _extract(8)
    entries = _edge_entries(graph)
    assert len(entries) == 8
    assert _relation_counts(entries) == {"mentioned_in": 6, "describes": 2}
    assert len(graph.edges) == 8


def test_edge_quota_never_touches_non_edge_entries():
    """事实/主题/参与者条目是检索语义主干，配额再小也不能动。"""
    graph = _extract(1)
    assert len(_edge_entries(graph)) == 1
    plain = [entry for entry in graph.entries if entry.entry_type != "edge"]
    assert len(plain) == _EXPECTED_PLAIN_ENTRIES
    assert _relation_counts(plain) == {
        "fact": 2,
        "topic": 2,
        "participant": 3,
    }


def test_edge_quota_is_deterministic():
    first = [entry.entry_key for entry in _edge_entries(_extract(8))]
    second = [entry.entry_key for entry in _edge_entries(_extract(8))]
    assert first == second


def test_edge_quota_degrades_safely_when_edges_are_misaligned():
    """边与条目数量不一致时只裁条目，宁可留下多余的边也不误删关系数据。"""
    graph = ExtractedGraph()
    graph.nodes.append(
        GraphNode(node_type="topic", value="话题", canonical_value="话题")
    )
    graph.edges.append(
        GraphEdge(
            source_key="a",
            target_key="b",
            relation_type="describes",
            source_memory_id=1,
        )
    )
    for index, confidence in enumerate((0.9, 0.5, 0.1)):
        graph.entries.append(
            GraphEntry(
                entry_key=f"edge-{index}",
                source_memory_id=1,
                session_id=None,
                persona_id=None,
                entry_type="edge",
                content=f"边条目 {index}",
                metadata={"graph_confidence": confidence},
                node_keys=["a", "b"],
                relation_type="describes",
            )
        )
    graph.entries.append(
        GraphEntry(
            entry_key="fact-0",
            source_memory_id=1,
            session_id=None,
            persona_id=None,
            entry_type="fact",
            content="事实条目",
            metadata={"graph_confidence": 0.0},
            node_keys=["a"],
            relation_type="fact",
        )
    )

    extractor = GraphExtractor({"max_edge_entries_per_memory": 1})
    result = extractor._apply_edge_entry_quota(graph)

    kept = _edge_entries(result)
    assert [entry.entry_key for entry in kept] == ["edge-0"]
    assert len(result.edges) == 1
    assert any(entry.entry_type == "fact" for entry in result.entries)


def test_edge_quota_treats_broken_confidence_as_zero():
    graph = ExtractedGraph()
    for index, confidence in enumerate(("坏值", None, 0.6)):
        graph.entries.append(
            GraphEntry(
                entry_key=f"edge-{index}",
                source_memory_id=1,
                session_id=None,
                persona_id=None,
                entry_type="edge",
                content=f"边条目 {index}",
                metadata={"graph_confidence": confidence},
                node_keys=["a", "b"],
                relation_type="describes",
            )
        )
    extractor = GraphExtractor({"max_edge_entries_per_memory": 1})
    kept = _edge_entries(extractor._apply_edge_entry_quota(graph))
    assert [entry.entry_key for entry in kept] == ["edge-2"]

# --------------------------------------------------------------------------
# 残留行清理（GraphStore.prune_orphans）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_orphans_reports_no_residue_on_clean_store(tmp_path: Path):
    store = await _open_store(tmp_path, "clean.db")
    await _seed_memory(store, 1)
    before = await store.get_memory_entry_stats()

    result = await store.prune_orphans()

    assert result["success"] is True
    assert result["total"] == 0
    assert result["truncated"] is False
    assert result["summary"] == "无残留"
    assert await store.get_memory_entry_stats() == before


@pytest.mark.asyncio
async def test_prune_orphans_dry_run_counts_without_writing(tmp_path: Path):
    db_path = tmp_path / "dry_run.db"
    store = await _open_store(tmp_path, "dry_run.db")
    node_map = await _seed_memory(store, 1)
    await _inject_residue(db_path, next(iter(node_map.values())), 1)
    before = await store.get_memory_entry_stats()

    result = await store.prune_orphans(dry_run=True)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["deleted"] == {
        "edges_without_entries": 1,
        "edges_without_nodes": 1,
        "entry_nodes": 1,
        "fts_rows": 1,
        "nodes": 1,
    }
    assert result["total"] == 5
    assert result["truncated"] is False
    # dry_run 只统计，一行都不能落库。
    assert await store.get_memory_entry_stats() == before


@pytest.mark.asyncio
async def test_prune_orphans_removes_every_residue_kind(tmp_path: Path):
    db_path = tmp_path / "residue.db"
    store = await _open_store(tmp_path, "residue.db")
    node_map = await _seed_memory(store, 1)
    healthy = await store.get_memory_entry_stats()
    await _inject_residue(db_path, next(iter(node_map.values())), 1)

    result = await store.prune_orphans()

    assert result["success"] is True
    assert result["dry_run"] is False
    assert result["deleted"] == {
        "edges_without_entries": 1,
        "edges_without_nodes": 1,
        "entry_nodes": 1,
        "fts_rows": 1,
        "nodes": 1,
    }
    assert result["total"] == 5
    assert result["truncated"] is False
    assert "edges_without_entries=1" in result["summary"]

    # 健康数据一行都不能少。
    assert await store.get_memory_entry_stats() == healthy
    leftover_entry_nodes = await _count(
        db_path,
        "SELECT COUNT(*) FROM graph_entry_nodes WHERE entry_id = ?",
        (_RESIDUE_ENTRY_ID,),
    )
    leftover_fts = await _count(
        db_path,
        "SELECT COUNT(*) FROM livingmemory_graph_entries_fts WHERE entry_id = ?",
        (_RESIDUE_FTS_ENTRY_ID,),
    )
    leftover_node = await _count(
        db_path,
        "SELECT COUNT(*) FROM graph_nodes WHERE node_key = ?",
        (_RESIDUE_ORPHAN_NODE_KEY,),
    )
    assert (leftover_entry_nodes, leftover_fts, leftover_node) == (0, 0, 0)

    # 幂等：再跑一次应当无事可做。
    again = await store.prune_orphans()
    assert again["total"] == 0
    assert again["summary"] == "无残留"


@pytest.mark.asyncio
async def test_prune_orphans_respects_row_budget(tmp_path: Path):
    db_path = tmp_path / "budget.db"
    store = await _open_store(tmp_path, "budget.db")
    node_map = await _seed_memory(store, 1)
    await _inject_residue(db_path, next(iter(node_map.values())), 1)

    limited = await store.prune_orphans(max_rows=2)
    assert limited["success"] is True
    assert limited["total"] == 2
    assert limited["truncated"] is True
    assert "未清完" in limited["summary"]

    # 下一次维护继续把剩下的清完。
    rest = await store.prune_orphans()
    assert rest["success"] is True
    assert rest["total"] == 3
    assert rest["truncated"] is False


@pytest.mark.asyncio
async def test_prune_orphans_dry_run_marks_truncated_budget(tmp_path: Path):
    db_path = tmp_path / "budget_dry.db"
    store = await _open_store(tmp_path, "budget_dry.db")
    node_map = await _seed_memory(store, 1)
    await _inject_residue(db_path, next(iter(node_map.values())), 1)
    before = await store.get_memory_entry_stats()

    result = await store.prune_orphans(dry_run=True, max_rows=2)

    assert result["total"] == 2
    assert result["truncated"] is True
    assert await store.get_memory_entry_stats() == before


@pytest.mark.asyncio
async def test_prune_orphans_deletes_large_residue_in_batches(tmp_path: Path):
    """batch_size 会被钳制到下限 100，多批循环必须收敛且不漏行。"""
    db_path = tmp_path / "batched.db"
    store = await _open_store(tmp_path, "batched.db")
    await _seed_memory(store, 1)
    healthy = await store.get_memory_entry_stats()

    stamp = "2020-01-01T00:00:00"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executemany(
            "INSERT INTO graph_nodes(node_key, node_type, node_value,"
            " canonical_value, metadata, created_at, updated_at)"
            " VALUES(?, 'topic', ?, ?, '{}', ?, ?)",
            [
                (
                    f"topic:__bulk_orphan_{index}__",
                    f"批量孤儿{index}",
                    f"__bulk_orphan_{index}__",
                    stamp,
                    stamp,
                )
                for index in range(250)
            ],
        )
        await db.commit()

    result = await store.prune_orphans(batch_size=1)

    assert result["success"] is True
    assert result["deleted"]["nodes"] == 250
    assert result["truncated"] is False
    assert await store.get_memory_entry_stats() == healthy


@pytest.mark.asyncio
async def test_prune_orphans_reports_failure_instead_of_raising(tmp_path: Path):
    """表还没建就被调用时，必须返回失败报告而不是把异常抛给维护任务。"""
    store = GraphStore(str(tmp_path / "never_initialized.db"))

    result = await store.prune_orphans()

    assert result["success"] is False
    assert result["error"]
    assert result["total"] == 0


# --------------------------------------------------------------------------
# 跨记忆共享边保护（storage 层）+ prune_graph 管理器层契约
# --------------------------------------------------------------------------

# 单条记忆只留 1 个事实时：2 条 describes + 3 条 mentioned_in + 3 条 co_occurs_with
# = 8 条边；条目 = 1 事实 + 2 主题 + 3 参与者 + 8 关系 = 14 条。
_SINGLE_FACT_ENTRIES = 14
# 两条记忆的参与者名单完全相同，3 条 co_occurs_with 会被 _add_edge 语义合并，
# 复用第一条记忆名下的边行；这正是上游静默丢数据的触发条件。
_SHARED_EDGES = 3
# 8（记忆 1）+ 5（记忆 2 独占的 describes/mentioned_in）= 13 条边。
_TWO_MEMORY_EDGES = 13


async def _entry_count(db_path: Path, memory_id: int) -> int:
    return await _count(
        db_path,
        "SELECT COUNT(*) FROM graph_entries WHERE source_memory_id = ?",
        (memory_id,),
    )


async def _aliased_entry_count(db_path: Path, memory_id: int, owner_id: int) -> int:
    """统计 memory_id 的条目里有多少条指向 owner_id 名下的边。"""
    return await _count(
        db_path,
        "SELECT COUNT(*) FROM graph_entries e"
        " JOIN graph_edges g ON e.edge_id = g.id"
        " WHERE e.source_memory_id = ? AND g.source_memory_id = ?",
        (memory_id, owner_id),
    )


@pytest.mark.asyncio
async def test_delete_memory_keeps_edges_still_referenced_by_other_memories(
    tmp_path: Path,
):
    """删除记忆 1 不能连带删掉记忆 2 正在复用的共享边（上游会静默丢条目）。"""
    db_path = tmp_path / "shared_edges.db"
    store = await _open_store(tmp_path, "shared_edges.db")
    await _seed_memory(store, 1, facts=["结论甲"])
    await _seed_memory(store, 2, facts=["结论乙"])

    assert await _entry_count(db_path, 1) == _SINGLE_FACT_ENTRIES
    assert await _entry_count(db_path, 2) == _SINGLE_FACT_ENTRIES
    # 前提断言：确实存在跨记忆别名，否则这条测试无法证伪级联删除。
    assert await _aliased_entry_count(db_path, 2, 1) == _SHARED_EDGES
    assert (await store.get_memory_entry_stats())["graph_edges"] == _TWO_MEMORY_EDGES

    await store.delete_memory(1)

    assert await _entry_count(db_path, 1) == 0
    assert await _entry_count(db_path, 2) == _SINGLE_FACT_ENTRIES
    stats = await store.get_memory_entry_stats()
    # 记忆 1 独占的 5 条边被回收，3 条被记忆 2 引用的共享边必须留下。
    assert stats["graph_edges"] == _TWO_MEMORY_EDGES - 5
    assert stats["graph_entries"] == _SINGLE_FACT_ENTRIES


@pytest.mark.asyncio
async def test_batch_delete_memories_keeps_shared_edges(tmp_path: Path):
    """批量删除走的是另一条 SQL，同样不能碰其它记忆仍在引用的边。"""
    db_path = tmp_path / "shared_edges_batch.db"
    store = await _open_store(tmp_path, "shared_edges_batch.db")
    await _seed_memory(store, 1, facts=["结论甲"])
    await _seed_memory(store, 2, facts=["结论乙"])
    await _seed_memory(store, 3, facts=["结论丙"])

    assert await _aliased_entry_count(db_path, 3, 1) == _SHARED_EDGES

    await store.batch_delete_memories([1, 2])

    assert await _entry_count(db_path, 1) == 0
    assert await _entry_count(db_path, 2) == 0
    assert await _entry_count(db_path, 3) == _SINGLE_FACT_ENTRIES
    stats = await store.get_memory_entry_stats()
    # 记忆 3 的 5 条独占边 + 它仍在引用的 3 条共享边。
    assert stats["graph_edges"] == 8
    assert stats["graph_entries"] == _SINGLE_FACT_ENTRIES


@pytest.mark.asyncio
async def test_prune_orphans_never_cascades_live_entries(tmp_path: Path):
    """被保护的残留边要等最后一个引用者消失才回收，且回收时不能牵连活条目。"""
    db_path = tmp_path / "prune_no_cascade.db"
    store = await _open_store(tmp_path, "prune_no_cascade.db")
    await _seed_memory(store, 1, facts=["结论甲"])
    await _seed_memory(store, 2, facts=["结论乙"])
    await store.delete_memory(1)

    # 此时 3 条共享边的 source_memory_id 指向已删记忆，但仍被记忆 2 引用。
    first_pass = await store.prune_orphans()

    assert first_pass["success"] is True
    assert first_pass["deleted"]["edges_without_entries"] == 0
    assert first_pass["total"] == 0
    assert await _entry_count(db_path, 2) == _SINGLE_FACT_ENTRIES

    # 最后一个引用者也删掉之后，残留边与随之孤立的节点才应该被收走。
    await store.delete_memory(2)
    stranded = await store.get_memory_entry_stats()
    assert stranded["graph_entries"] == 0
    assert stranded["graph_edges"] == _SHARED_EDGES
    assert stranded["graph_nodes"] == 3

    second_pass = await store.prune_orphans()

    assert second_pass["success"] is True
    assert second_pass["deleted"]["edges_without_entries"] == _SHARED_EDGES
    assert second_pass["deleted"]["nodes"] == 3
    assert await store.get_memory_entry_stats() == {
        "graph_nodes": 0,
        "graph_edges": 0,
        "graph_entries": 0,
    }


# --------------------------------------------------------------------------
# GraphMemoryManager.prune_graph（maintain_storage 的调用契约）
# --------------------------------------------------------------------------


class _ExplodingGraphStore:
    """prune_orphans 抛异常的桩，用于验证异常不会冒泡到维护任务。"""

    async def prune_orphans(self, *, dry_run: bool = False) -> dict:
        raise RuntimeError("磁盘忙")


def _manager(graph_store) -> GraphMemoryManager:
    return GraphMemoryManager(
        graph_store=graph_store,
        graph_vector_retriever=SimpleNamespace(),
        graph_extractor=GraphExtractor({}),
    )


@pytest.mark.asyncio
async def test_prune_graph_can_be_called_without_arguments(tmp_path: Path):
    """maintain_storage 是无参调用的，签名必须允许。"""
    store = await _open_store(tmp_path, "manager_prune.db")
    await _seed_memory(store, 1, facts=["结论甲"])

    result = await _manager(store).prune_graph()

    assert isinstance(result, dict)
    assert result["success"] is True
    assert result["total"] == 0
    assert result["summary"]


@pytest.mark.asyncio
async def test_prune_graph_skips_while_rebuild_is_active(tmp_path: Path):
    """图谱重建期间必须跳过，避免和影子表切换互相干扰。"""
    store = await _open_store(tmp_path, "manager_prune_busy.db")
    manager = _manager(store)
    manager._rebuild_active = True

    result = await manager.prune_graph()

    assert result["success"] is True
    assert result["skipped"] is True
    assert result["summary"]


@pytest.mark.asyncio
async def test_prune_graph_reports_unsupported_store():
    """老版本 store 没有 prune_orphans 时要报告失败，而不是 AttributeError。"""
    result = await _manager(SimpleNamespace()).prune_graph()

    assert result["success"] is False
    assert "does not support pruning" in result["error"]


@pytest.mark.asyncio
async def test_prune_graph_converts_store_failure_into_report():
    """存储层异常必须转成报告，否则每日维护会整体中断。"""
    result = await _manager(_ExplodingGraphStore()).prune_graph()

    assert result["success"] is False
    assert "磁盘忙" in result["error"]
