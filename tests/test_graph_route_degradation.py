"""A graph subroute failure must not discard successful ordinary recall."""

import asyncio
import gc
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.retrieval.dual_route_retriever import DualRouteRetriever
from astrbot_plugin_anamnesis.core.retrieval.graph_keyword_retriever import GraphKeywordResult
from astrbot_plugin_anamnesis.core.retrieval.graph_retriever import GraphRetriever
from astrbot_plugin_anamnesis.core.retrieval.graph_vector_retriever import GraphVectorResult
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridResult, HybridRetriever
from astrbot_plugin_anamnesis.core.retrieval.rrf_fusion import RRFFusion


def make_routes():
    metadata = {"session_id": "scope", "persona_id": "p", "importance": .8}
    keyword = SimpleNamespace(search=AsyncMock(return_value=[
        GraphKeywordResult(1, .8, "coffee", metadata),
    ]))
    vector = SimpleNamespace(search=AsyncMock(return_value=[
        GraphVectorResult(2, .8, "tea", metadata),
    ]))
    return keyword, vector


@pytest.mark.asyncio
@pytest.mark.parametrize("failed,expected", [("keyword", 2), ("vector", 1)])
async def test_graph_keeps_successful_channel_after_backend_failure(failed, expected):
    keyword, vector = make_routes()
    getattr({"keyword": keyword, "vector": vector}[failed], "search").side_effect = RuntimeError("backend unavailable")
    retriever = GraphRetriever(keyword, vector, RRFFusion())
    results = await retriever.search("coffee", 5, "scope", "p")
    assert [item.doc_id for item in results] == [expected]
    assert results[0].metadata["session_id"] == "scope"
    assert results[0].metadata["persona_id"] == "p"
    keyword.search.assert_awaited_once_with("coffee", 5, "scope", "p")
    vector.search.assert_awaited_once_with("coffee", 5, "scope", "p")


@pytest.mark.asyncio
async def test_graph_outage_does_not_discard_successful_document_recall():
    keyword, vector = make_routes()
    keyword.search.side_effect = RuntimeError("FTS unavailable")
    vector.search.side_effect = RuntimeError("embedding input too long")
    graph = GraphRetriever(keyword, vector, RRFFusion())
    memory = HybridResult(7, .8, .2, .6, None, "user prefers coffee", {"session_id": "scope"})
    document = SimpleNamespace(search=AsyncMock(return_value=[memory]))
    retriever = DualRouteRetriever(document, graph, AsyncMock())
    assert await retriever.search("coffee", 5, "scope", "p") == [memory]


@pytest.mark.asyncio
async def test_fast_failure_waits_for_other_graph_channel_to_finish():
    keyword, vector = make_routes()
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    keyword.search.side_effect = RuntimeError("FTS unavailable")

    async def slow_vector(*args):
        started.set()
        await release.wait()
        finished.set()
        return []

    vector.search.side_effect = slow_vector
    task = asyncio.create_task(GraphRetriever(keyword, vector, RRFFusion()).search("coffee"))
    try:
        await started.wait()
        await asyncio.sleep(0)
        assert not task.done(), 'the healthy channel must not be abandoned'
        release.set()
        assert await task == []
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_outer_cancellation_settles_both_graph_channels():
    entered = [asyncio.Event(), asyncio.Event()]
    cancelled = [False, False]

    async def wait(index):
        entered[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled[index] = True

    async def keyword(*args):
        return await wait(0)

    async def vector(*args):
        return await wait(1)

    retriever = GraphRetriever(SimpleNamespace(search=keyword), SimpleNamespace(search=vector), RRFFusion())
    task = asyncio.create_task(retriever.search("coffee"))
    await asyncio.gather(*(event.wait() for event in entered))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(cancelled)


@pytest.mark.asyncio
@pytest.mark.parametrize("retriever_type", [GraphRetriever, HybridRetriever])
async def test_cancel_before_channels_start_does_not_leak_search_coroutines(retriever_type):
    entered = []

    async def keyword(*args):
        entered.append("keyword")
        return []

    async def vector(*args):
        entered.append("vector")
        return []

    retriever = retriever_type(SimpleNamespace(search=keyword), SimpleNamespace(search=vector), RRFFusion())
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always", RuntimeWarning)
        task = asyncio.create_task(retriever.search("coffee"))
        # The parent dispatches its channels, then cancellation arrives before
        # either channel has had a chance to run.
        asyncio.get_running_loop().call_soon(task.cancel)
        with pytest.raises(asyncio.CancelledError):
            await task
        del task
        await asyncio.sleep(0)
        gc.collect()
    assert not entered
    assert not [warning for warning in emitted if "was never awaited" in str(warning.message)]


@pytest.mark.asyncio
async def test_channel_cancellation_is_not_treated_as_empty_results():
    keyword, vector = make_routes()
    vector.search.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await GraphRetriever(keyword, vector, RRFFusion()).search("coffee")


@pytest.mark.asyncio
async def test_one_channel_cancellation_cancels_its_pending_sibling():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def keyword(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def vector(*args):
        await started.wait()
        raise asyncio.CancelledError

    retriever = GraphRetriever(SimpleNamespace(search=keyword), SimpleNamespace(search=vector), RRFFusion())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(retriever.search("coffee"), timeout=1)
    assert cancelled.is_set()
