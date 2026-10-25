"""Explicit no-persona selection reaches every optional retrieval channel."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.managers.memory_administration import (
    MemoryAdministration,
)
from astrbot_plugin_anamnesis.core.models.graph_models import GraphEntry, GraphNode
from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom
from astrbot_plugin_anamnesis.core.recall_persona import (
    NO_PERSONA,
    exact_null_persona_scope,
)
from astrbot_plugin_anamnesis.core.retrieval.graph_vector_retriever import (
    GraphVectorRetriever,
)
from astrbot_plugin_anamnesis.core.retrieval.temporal_retrieval import search_with_time
from astrbot_plugin_anamnesis.storage.graph_store import GraphStore

from tests.test_graph_memory import _FakeFaissDB
from tests.test_memory_transfer_runtime_safety import _engine
from tests.test_vector_scope_and_score_sync import _Index

SCOPE = "qq:GroupMessage:1"
PERSONAS = [None, NO_PERSONA, "selected-persona"]


@pytest_asyncio.fixture
async def engine(tmp_path):
    instance = await _engine(tmp_path / "route-persona.db")
    try:
        yield instance
    finally:
        await instance.close()


async def add_memory(
    engine, persona, *, content="unrelated summary", created=None, atoms=None
):
    return await engine.add_memory(
        content=content,
        session_id=SCOPE,
        persona_id=persona,
        metadata={
            "status": "active",
            "key_facts": [content],
            "source_session_id": SCOPE,
            "create_time": time.time() if created is None else created,
        },
        atoms=atoms,
        preserve_atom_lifecycle=True,
        preserve_create_time=True,
    )


async def scoped_search(engine, persona, query, *, filtered=True):
    results, _ = await search_with_time(
        engine,
        query,
        k=1,
        session_id=SCOPE,
        persona_id=persona,
        persona_filtered=filtered,
        mode="semantic",
        record_access=False,
        config_manager=ConfigManager(
            {"filtering_settings": {"use_persona_filtering": True}}
        ),
    )
    return results


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", PERSONAS)
async def test_recent_reserve_filters_persona_before_newer_foreign_records(
    engine, persona
):
    engine.atom_enabled = False
    engine.config["recent_memory_count"] = 1
    engine.config["recent_memory_max_age_hours"] = 72
    # A legitimate empty vector response plus a keyword absent from all
    # document FTS rows isolates the real SQLite recent-memory reservation.
    engine.faiss_db.retrieve = AsyncMock(return_value=[])
    instant = time.time()
    await add_memory(engine, None, created=instant - 300)
    marker_id = await add_memory(engine, NO_PERSONA, created=instant - 200)
    named_id = await add_memory(engine, "selected-persona", created=instant - 100)
    newest_foreign = None
    for index in range(7):
        newest_foreign = await add_memory(
            engine, "foreign-persona", created=instant + index
        )
    results = await scoped_search(engine, persona, "absentkeyword")
    assert [item.doc_id for item in results] == [
        named_id if persona == "selected-persona" else marker_id
    ]
    assert results[0].score_breakdown["recent_memory"] == 1
    unfiltered = await scoped_search(engine, None, "absentkeyword", filtered=False)
    assert [item.doc_id for item in unfiltered] == [newest_foreign]


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", PERSONAS)
async def test_independent_atom_only_candidates_keep_null_and_named_parent_boundaries(
    engine, persona
):
    engine.atom_enabled = True
    engine.config["recent_memory_count"] = 0
    engine.faiss_db.retrieve = AsyncMock(return_value=[])
    ids = {}
    for stored in [None, NO_PERSONA, "selected-persona"] + ["foreign-persona"] * 7:
        atom = MemoryAtom(
            parent_memory_id=0,
            content="atomneedle",
            session_id=SCOPE,
            persona_id=stored,
            expires_at=time.time() + 86400,
        )
        memory_id = await add_memory(engine, stored, atoms=[atom])
        ids.setdefault(stored, []).append(memory_id)
    results = await scoped_search(engine, persona, "atomneedle")
    expected = (
        ids["selected-persona"]
        if persona == "selected-persona"
        else ids[None] + ids[NO_PERSONA]
    )
    assert len(results) == 1 and results[0].doc_id in expected
    assert results[0].content == "atomneedle"
    assert "atom_score" in results[0].score_breakdown


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", PERSONAS)
async def test_graph_keyword_and_node_expansion_filter_before_top_k(tmp_path, persona):
    store = GraphStore(str(tmp_path / "graph-persona.db"))
    await store.initialize()
    try:
        node = GraphNode(
            node_type="topic", value="graphneedle", canonical_value="graphneedle"
        )
        node_ids = await store.upsert_nodes([node])
        records = [
            (10, "foreign-persona"),
            (11, "foreign-persona"),
            (1, None),
            (2, NO_PERSONA),
            (3, "selected-persona"),
            (12, "foreign-persona"),
        ]
        await store.add_entries(
            [
                GraphEntry(
                    entry_key=f"entry:{memory_id}",
                    source_memory_id=memory_id,
                    session_id=SCOPE,
                    persona_id=stored,
                    entry_type="topic",
                    content="graphneedle",
                    metadata={"session_id": SCOPE, "persona_id": stored},
                    node_keys=[node.node_key],
                )
                for memory_id, stored in records
            ],
            node_ids,
            {},
        )
        matches = await store.search_nodes_by_tokens(["graphneedle"])
        assert [item["id"] for item in matches] == [node_ids[node.node_key]]
        expected = {3} if persona == "selected-persona" else {1, 2}
        with exact_null_persona_scope(persona in (None, NO_PERSONA)):
            keyword = await store.search_entries_by_bm25(
                "graphneedle", 20, SCOPE, persona
            )
            expanded = await store.get_entries_for_node_ids(
                [matches[0]["id"]], 20, SCOPE, persona
            )
            keyword_top = await store.search_entries_by_bm25(
                "graphneedle", 1, SCOPE, persona
            )
            expanded_top = await store.get_entries_for_node_ids(
                [matches[0]["id"]], 1, SCOPE, persona
            )
        assert {item["source_memory_id"] for item in keyword} == expected
        assert {item["source_memory_id"] for item in expanded} == expected
        for result in (keyword_top, expanded_top):
            assert len(result) == 1 and result[0]["source_memory_id"] in expected
        # An omitted filter remains useful to administrator graph readers.
        assert len(await store.search_entries_by_bm25("graphneedle", 20, SCOPE)) == len(
            records
        )
        assert len(
            await store.get_entries_for_node_ids([matches[0]["id"]], 20, SCOPE)
        ) == len(records)
    finally:
        await store.close()


class EqualityDocuments:
    """Host SDK equality predicates executed against actual SQLite JSON NULL."""

    def __init__(self, path):
        self.path = path
        self.calls = []

    async def get_documents(self, metadata_filters, ids=None, limit=50, offset=0):
        filters = metadata_filters or {}
        self.calls.append(
            {"filters": dict(filters), "ids": list(ids) if ids is not None else None}
        )
        clauses, values = [], []
        for key, value in filters.items():
            assert key in ("session_id", "persona_id")
            clauses.append(f"json_extract(metadata,'$.{key}') = ?")
            values.append(value)
        if ids is not None:
            if not ids:
                return []
            clauses.append("id IN (" + ",".join("?" for _ in ids) + ")")
            values.extend(ids)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"SELECT id,doc_id,text,metadata FROM documents {where} ORDER BY id LIMIT ? OFFSET ?",
                    (*values, limit, offset),
                )
            ).fetchall()
        return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]


async def graph_vectors(path, *, native):
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE documents(id INTEGER PRIMARY KEY,doc_id TEXT,text TEXT,metadata TEXT)"
        )
        await db.commit()
    vectors = _FakeFaissDB()
    vectors._sqlite_path = str(path)
    documents = EqualityDocuments(str(path))
    vectors.document_storage = documents
    retriever = GraphVectorRetriever(vectors, {"vector_max_fetch_candidates": 8})
    for memory_id, persona in [
        (1, "foreign-persona"),
        (2, "foreign-persona"),
        (3, "foreign-persona"),
        (4, "foreign-persona"),
        (5, None),
        (6, NO_PERSONA),
        (7, "selected-persona"),
        (8, "foreign-persona"),
    ]:
        await retriever.add_entry(
            "graphneedle",
            {
                "source_memory_id": 100 + memory_id,
                "session_id": SCOPE,
                "persona_id": persona,
                "status": "active",
            },
        )
    if native:
        vectors.embedding_storage = SimpleNamespace(index=_Index(8), dimension=2)
        vectors.embedding_provider = SimpleNamespace(
            get_embedding=AsyncMock(return_value=[1.0, 0.0])
        )
        vectors.retrieve = AsyncMock(
            side_effect=AssertionError(
                "native route must share its one query embedding"
            )
        )
    else:

        async def retrieve(query, k, fetch_k, rerank, metadata_filters=None):
            rows = await documents.get_documents(
                metadata_filters, ids=list(range(1, min(fetch_k, 8) + 1)), limit=fetch_k
            )
            return [SimpleNamespace(data=row, similarity=0.9) for row in rows][:k]

        vectors.retrieve = AsyncMock(side_effect=retrieve)
    return retriever, vectors, documents


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("persona", PERSONAS)
async def test_graph_vectors_avoid_sdk_null_equality_and_widen_to_legal_top_k(
    tmp_path, native, persona
):
    retriever, vectors, documents = await graph_vectors(
        tmp_path / "graph-vectors.db", native=native
    )
    # This SDK really cannot find JSON null via '= ?'; scoped retrieval must
    # omit that predicate and enforce no-persona equivalence after loading.
    assert await documents.get_documents({"persona_id": None}, ids=[5], limit=1) == []
    documents.calls.clear()
    with exact_null_persona_scope(persona in (None, NO_PERSONA)):
        results = await retriever.search("graphneedle", 1, SCOPE, persona)
    assert [item.doc_id for item in results] == (
        [107] if persona == "selected-persona" else [105]
    )
    assert all(
        call["filters"].get("persona_id", "omitted") is not None
        for call in documents.calls
    )
    if native:
        vectors.embedding_provider.get_embedding.assert_awaited_once_with("graphneedle")
        vectors.retrieve.assert_not_awaited()
        assert [call["ids"] for call in documents.calls] == [[1, 2, 3, 4], [5, 6, 7, 8]]
    else:
        assert all(
            call.kwargs["metadata_filters"].get("persona_id", "omitted") is not None
            for call in vectors.retrieve.await_args_list
        )
        assert [
            call.kwargs["fetch_k"] for call in vectors.retrieve.await_args_list
        ] == [4, 8]
    if persona in (None, NO_PERSONA):
        with exact_null_persona_scope(True):
            both = await retriever.search("graphneedle", 2, SCOPE, persona)
        assert [item.doc_id for item in both] == [105, 106]


@pytest.mark.asyncio
async def test_native_graph_vector_cap_bounds_unmatched_null_selection(tmp_path):
    retriever, vectors, _ = await graph_vectors(
        tmp_path / "bounded-graph-vectors.db", native=True
    )
    retriever.config["vector_max_fetch_candidates"] = 4
    with exact_null_persona_scope(True):
        assert await retriever.search("graphneedle", 1, SCOPE, None) == []
    vectors.embedding_provider.get_embedding.assert_awaited_once_with("graphneedle")


@pytest.mark.asyncio
async def test_episode_candidates_explicit_null_filter_preserves_unfiltered_web_listing(
    engine,
):
    engine.atom_enabled = False
    service = MemoryAdministration(
        engine, ConfigManager({"episodic_memory": {"enabled": True}})
    )
    engine.memory_administration = service
    await service.initialize()
    expected = {}
    for persona in PERSONAS:
        memory_id = await add_memory(engine, persona, content="coffee event evidence")
        job = await service.create_job(
            "episode",
            {"memory_ids": [memory_id], "use_llm": False, "title": "coffee event"},
        )
        task = service._jobs.get(job["operation_id"])
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), 5)
        operation = await service.store.get(job["operation_id"])
        assert operation["status"] == "completed"
        expected[persona] = operation["result"]["episode_id"]
    for persona in PERSONAS:
        matches = await service.episode_candidates(
            "coffee",
            [],
            session_id=SCOPE,
            persona_id=persona,
            persona_filtered=True,
            k=5,
        )
        ids = {item["id"] for item in matches}
        assert ids == (
            {expected["selected-persona"]}
            if persona == "selected-persona"
            else {expected[None], expected[NO_PERSONA]}
        )
        top = await service.episode_candidates(
            "coffee",
            [],
            session_id=SCOPE,
            persona_id=persona,
            persona_filtered=True,
            k=1,
        )
        assert len(top) == 1 and top[0]["id"] in ids
    assert {item["id"] for item in await service.list_episodes()} == set(
        expected.values()
    )
    assert {
        item["id"]
        for item in await service.episode_candidates(
            "coffee", [], session_id=SCOPE, k=5
        )
    } == set(expected.values())
