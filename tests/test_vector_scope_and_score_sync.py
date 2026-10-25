"""Scoped candidate widening, query embedding sharing and score fusion."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from astrbot_plugin_anamnesis.core.retrieval import vector_search
from astrbot_plugin_anamnesis.core.retrieval.graph_vector_retriever import (
    GraphVectorRetriever,
)
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridRetriever
from astrbot_plugin_anamnesis.core.retrieval.rrf_fusion import (
    BM25Result,
    RRFFusion,
    VectorResult,
)
from astrbot_plugin_anamnesis.core.retrieval.vector_retriever import VectorRetriever


class _Index:
    def __init__(self, total):
        self.ntotal = total

    def search(self, vector, k):
        return np.zeros((1, k), dtype="float32"), np.array([list(range(1, k+1))])


def _native_database(documents, provider=None):
    async def get_documents(metadata_filters, ids, limit):
        return [documents[i] for i in ids if i in documents and all(
            documents[i]["metadata"].get(key) == value for key, value in metadata_filters.items()
        )][:limit]

    return SimpleNamespace(
        embedding_storage=SimpleNamespace(index=_Index(max(documents)), dimension=2),
        embedding_provider=provider or SimpleNamespace(get_embedding=AsyncMock(return_value=[1.0, 0.0])),
        document_storage=SimpleNamespace(get_documents=AsyncMock(side_effect=get_documents)),
        retrieve=AsyncMock(side_effect=AssertionError("native search must not re-embed through retrieve")),
    )


def _documents(count=8):
    return {i: {"id": i, "text": f"memory {i}", "metadata": {
        "session_id": "other" if i <= 4 else "wanted", "status": "active",
        "source_memory_id": 10 if i <= 4 else 11,
    }} for i in range(1, count+1)}


@pytest.mark.asyncio
async def test_scoped_expansion_finds_later_match_and_caches_negative_ids():
    db = _native_database(_documents())
    results = await VectorRetriever(db).search("tea", 1, "wanted")
    assert [r.doc_id for r in results] == [5]
    calls = db.document_storage.get_documents.await_args_list
    assert [c.kwargs["ids"] for c in calls] == [[1, 2, 3, 4], [5, 6, 7, 8]]
    db.embedding_provider.get_embedding.assert_awaited_once_with("tea")


@pytest.mark.asyncio
async def test_candidate_cap_bounds_empty_scoped_search():
    db = _native_database(_documents())
    results = await VectorRetriever(db, {"vector_max_fetch_candidates": 4}).search("tea", 1, "wanted")
    assert results == []
    assert db.document_storage.get_documents.await_count == 1
    db.embedding_provider.get_embedding.assert_awaited_once()


@pytest.mark.asyncio
async def test_graph_vectors_widen_past_duplicate_source_memories():
    db = _native_database(_documents())
    results = await GraphVectorRetriever(db).search("tea", 2)
    assert [r.doc_id for r in results] == [10, 11]
    assert [c.kwargs["ids"] for c in db.document_storage.get_documents.await_args_list] == [
        [1, 2, 3, 4], [5, 6, 7, 8],
    ]


@pytest.mark.asyncio
async def test_document_and_graph_share_one_concurrent_embedding():
    provider = SimpleNamespace(get_embedding=AsyncMock(return_value=[1.0, 0.0]))
    document_db, graph_db = _native_database(_documents(), provider), _native_database(_documents(), provider)
    async with vector_search.query_embedding_scope():
        document, graph = await asyncio.gather(
            VectorRetriever(document_db).search("tea", 1, "wanted"),
            GraphVectorRetriever(graph_db).search("tea", 1, "wanted"),
        )
    assert document and graph
    provider.get_embedding.assert_awaited_once_with("tea")


@pytest.mark.asyncio
async def test_legacy_retrieve_adapter_still_widens_and_revalidates_scope():
    docs = _documents()

    async def retrieve(**kwargs):
        return [SimpleNamespace(data=docs[i], similarity=0.9)
                for i in range(1, min(8, kwargs["fetch_k"])+1)]

    db = SimpleNamespace(retrieve=AsyncMock(side_effect=retrieve))
    results = await VectorRetriever(db, {"vector_max_fetch_candidates": 8}).search("tea", 1, "wanted")
    assert [r.doc_id for r in results] == [5]
    assert [call.kwargs["fetch_k"] for call in db.retrieve.await_args_list] == [4, 8]


@pytest.mark.asyncio
async def test_cancelled_faiss_worker_finishes_before_mutation_lock_is_released(monkeypatch):
    db = _native_database(_documents())
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    mutation_lock = asyncio.Lock()

    def gated_search(vector, k):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return np.zeros((1, k)), np.array([list(range(1, k+1))])

    db.embedding_storage.index.search = gated_search
    monkeypatch.setattr(vector_search, "get_async_persister", lambda storage: SimpleNamespace(_mutation_lock=mutation_lock))
    task = asyncio.create_task(VectorRetriever(db).search("tea", 1))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert mutation_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not mutation_lock.locked()
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_shared_embedding_is_cleaned_up_by_owning_scope():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def embedding(query):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    db = _native_database(_documents(), SimpleNamespace(get_embedding=AsyncMock(side_effect=embedding)))

    async def recall():
        async with vector_search.query_embedding_scope():
            await VectorRetriever(db).search("tea", 1)

    task = asyncio.create_task(recall())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_embedding_cleanup_and_resets_context():
    started, cleaning, release, finished = (asyncio.Event() for _ in range(4))

    async def embedding(query):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    db = _native_database(_documents(), SimpleNamespace(get_embedding=AsyncMock(side_effect=embedding)))

    async def recall():
        try:
            async with vector_search.query_embedding_scope():
                await VectorRetriever(db).search("tea", 1)
        except asyncio.CancelledError:
            assert vector_search.query_embeddings.get() is None
            raise

    task = asyncio.create_task(recall())
    try:
        await started.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert finished.is_set()
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


def _fusion_inputs():
    return (
        [BM25Result(1, 10.0, "strong exact match", {}), BM25Result(2, .1, "weak agreement", {}), BM25Result(3, .09, "noise", {})],
        [VectorResult(2, .51, "weak agreement", {}), VectorResult(3, .50, "noise", {}), VectorResult(4, .49, "other noise", {})],
    )


def test_score_fusion_retains_strong_single_route_evidence_and_legacy_rrf():
    bm25, vectors = _fusion_inputs()
    fusion = RRFFusion()
    assert fusion.fuse(bm25, vectors, 1)[0].doc_id == 2
    assert fusion.fuse_scores(bm25, vectors, 1, agreement_bonus=.25)[0].doc_id == 1
    assert fusion.fuse_scores(bm25, vectors, 1, bm25_weight=0)[0].doc_id == 2


def test_score_consensus_rewards_shared_evidence_and_has_stable_equal_scores():
    bm25 = [BM25Result(2, 1, "shared", {}), BM25Result(1, 1, "keyword", {})]
    vectors = [VectorResult(2, 1, "shared", {}), VectorResult(3, 1, "vector", {})]
    results = RRFFusion().fuse_scores(bm25, vectors, 3, agreement_bonus=.25)
    assert [r.doc_id for r in results] == [2, 1, 3]
    assert results[0].rrf_score > results[1].rrf_score


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, expected, requested", [("rrf", 2, 1), ("score", 1, 3)])
async def test_hybrid_mode_wires_fusion_and_preserves_scope(mode, expected, requested):
    bm25, vectors = _fusion_inputs()
    now = time.time()
    for result in [*bm25, *vectors]:
        result.metadata = {"importance": .5, "create_time": now, "last_access_time": now}
    keyword, vector = SimpleNamespace(search=AsyncMock(return_value=bm25)), SimpleNamespace(search=AsyncMock(return_value=vectors))
    retriever = HybridRetriever(keyword, vector, RRFFusion(), config={"fusion_mode": mode})
    result = await retriever.search("tea", 1, "group", "persona")
    assert result[0].doc_id == expected
    keyword.search.assert_awaited_once_with("tea", requested, "group", "persona")
    vector.search.assert_awaited_once_with("tea", requested, "group", "persona")


@pytest.mark.asyncio
async def test_score_fusion_exposes_enough_candidates_for_rerank():
    async def search(query, k, session_id, persona_id):
        return [BM25Result(i, 5-i, f"memory {i}", {"importance": .5, "create_time": time.time()}) for i in range(1, min(k, 4)+1)]

    async def rerank(query, documents):
        return [SimpleNamespace(index=i, relevance_score=1.0 if doc == "memory 4" else 0.0)
                for i, doc in enumerate(documents)]

    route = SimpleNamespace(search=AsyncMock(side_effect=search))
    provider = SimpleNamespace(rerank=AsyncMock(side_effect=rerank))
    retriever = HybridRetriever(route, route, RRFFusion(), config={
        "fusion_mode": "score", "rerank_enabled": True, "rerank_candidates": 4,
    }, rerank_provider_resolver=lambda: provider)
    result = await retriever.search("tea", 1, "group", "persona")
    assert result[0].doc_id == 4
    assert len(provider.rerank.await_args.kwargs["documents"]) == 4
