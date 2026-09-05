"""Bug H / LRU 回归测试：vector_retriever 的 faiss 调用不得阻塞事件循环。

faiss 是 CPU 密集的 C 扩展，逐条 reconstruct 与同步 search 直接跑在事件循环里
会卡死整个 bot；_id_cache 满 1000 条后停止写入会让缓存长期失效。
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from astrbot_plugin_anamnesis.core.retrieval.vector_retriever import VectorRetriever

_VECS = {
    1: np.array([1.0, 0.0], dtype=np.float32),
    2: np.array([1.0, 0.1], dtype=np.float32),
    3: np.array([0.0, 1.0], dtype=np.float32),
}


class _FakeIndex:
    """记录调用次数与执行线程的假 faiss 索引。"""

    def __init__(
        self,
        *,
        with_batch: bool = True,
        batch_error: Exception | None = None,
        batch_shape_bug: bool = False,
    ) -> None:
        self.reconstruct_calls: list[int] = []
        self.batch_calls: list[list[int]] = []
        self.search_threads: list[int] = []
        self.reconstruct_threads: list[int] = []
        self._batch_error = batch_error
        self._batch_shape_bug = batch_shape_bug
        if with_batch:
            self.reconstruct_batch = self._reconstruct_batch

    def _reconstruct_batch(self, ids) -> np.ndarray:
        self.batch_calls.append([int(i) for i in np.asarray(ids).tolist()])
        self.reconstruct_threads.append(threading.get_ident())
        if self._batch_error is not None:
            raise self._batch_error
        if self._batch_shape_bug:
            return np.zeros((1, 2), dtype="float32")
        return np.stack([_VECS[int(i)] for i in np.asarray(ids).tolist()]).astype(
            "float32"
        )

    def reconstruct(self, doc_id: int) -> np.ndarray:
        self.reconstruct_calls.append(int(doc_id))
        self.reconstruct_threads.append(threading.get_ident())
        return _VECS[int(doc_id)]

    def reconstruct_n(self, start, count):  # pragma: no cover - 必须永不被调用
        raise AssertionError(
            "reconstruct_n 在本机 faiss 1.13.2 + IndexIDMap2 上会导致进程硬崩溃"
        )

    def search(self, matrix, k):
        self.search_threads.append(threading.get_ident())
        ids = list(_VECS.keys())
        all_vecs = np.stack([_VECS[i] for i in ids]).astype("float32")
        diff = matrix[:, None, :] - all_vecs[None, :, :]
        dist = (diff**2).sum(-1)
        order = np.argsort(dist, axis=1)[:, :k]
        d = np.take_along_axis(dist, order, axis=1)
        i = np.array(ids)[order]
        return d.astype("float32"), i.astype("int64")


def _make_retriever(index, config=None) -> VectorRetriever:
    class _Storage:
        def __init__(self) -> None:
            self.index = index

    class _FaissDB:
        def __init__(self) -> None:
            self.embedding_storage = _Storage()

    return VectorRetriever(_FaissDB(), config)  # type: ignore[arg-type]


# ── Bug H：批量重建 + to_thread ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_similar_pairs_prefers_reconstruct_batch():
    """有 reconstruct_batch 时只调一次批量 API，不再逐条 reconstruct。"""
    index = _FakeIndex()
    retriever = _make_retriever(index)

    pairs = await retriever.find_similar_pairs([1, 2, 3], threshold=0.9, k=2)

    assert index.batch_calls == [[1, 2, 3]]
    assert index.reconstruct_calls == []
    sims = {tuple(sorted((a, b))): s for a, b, s in pairs}
    assert (1, 2) in sims and sims[(1, 2)] >= 0.9
    assert (1, 3) not in sims and (2, 3) not in sims


@pytest.mark.asyncio
async def test_find_similar_pairs_runs_faiss_off_the_event_loop():
    """reconstruct 与 search 都必须在工作线程里执行。"""
    index = _FakeIndex()
    retriever = _make_retriever(index)
    main_ident = threading.get_ident()

    await retriever.find_similar_pairs([1, 2, 3], threshold=0.9, k=2)

    assert index.search_threads, "search 未被调用"
    assert index.reconstruct_threads, "重建未被调用"
    assert all(ident != main_ident for ident in index.search_threads)
    assert all(ident != main_ident for ident in index.reconstruct_threads)


@pytest.mark.asyncio
async def test_find_similar_pairs_falls_back_when_batch_raises():
    """reconstruct_batch 遇缺失 id 会整批抛错，此时退化为逐条重建。"""
    index = _FakeIndex(batch_error=RuntimeError("id not found"))
    retriever = _make_retriever(index)

    pairs = await retriever.find_similar_pairs([1, 2, 3], threshold=0.9, k=2)

    assert index.batch_calls == [[1, 2, 3]]
    assert index.reconstruct_calls == [1, 2, 3]
    assert {tuple(sorted((a, b))) for a, b, _ in pairs} == {(1, 2)}


@pytest.mark.asyncio
async def test_find_similar_pairs_falls_back_on_unexpected_batch_shape():
    """批量 API 返回形状与请求 id 数不一致时必须回退，避免向量错位。"""
    index = _FakeIndex(batch_shape_bug=True)
    retriever = _make_retriever(index)

    pairs = await retriever.find_similar_pairs([1, 2, 3], threshold=0.9, k=2)

    assert index.reconstruct_calls == [1, 2, 3]
    assert {tuple(sorted((a, b))) for a, b, _ in pairs} == {(1, 2)}


@pytest.mark.asyncio
async def test_find_similar_pairs_without_batch_api_uses_loop():
    """老索引没有 reconstruct_batch 时行为与改造前一致。"""
    index = _FakeIndex(with_batch=False)
    retriever = _make_retriever(index)

    pairs = await retriever.find_similar_pairs([1, 2, 3], threshold=0.9, k=2)

    assert index.reconstruct_calls == [1, 2, 3]
    assert {tuple(sorted((a, b))) for a, b, _ in pairs} == {(1, 2)}


@pytest.mark.asyncio
async def test_find_similar_pairs_short_circuits_on_empty_input():
    index = _FakeIndex()
    retriever = _make_retriever(index)

    assert await retriever.find_similar_pairs([], threshold=0.9) == []
    assert await retriever.find_similar_pairs([7], threshold=0.9) == []
    assert index.batch_calls == []


# ── _id_cache 真 LRU ────────────────────────────────────────────────────────


class _DocStorage:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def get_documents(self, metadata_filters=None, ids=None, limit=1, offset=0):
        doc_id = int((ids or [0])[0])
        self.calls.append(doc_id)
        return [{"id": doc_id, "doc_id": f"uuid-{doc_id}"}]


def _make_cache_retriever(max_size: int) -> tuple[VectorRetriever, _DocStorage]:
    storage = _DocStorage()

    class _FaissDB:
        def __init__(self) -> None:
            self.document_storage = storage

    retriever = VectorRetriever(_FaissDB(), {"id_cache_size": max_size})  # type: ignore[arg-type]
    return retriever, storage


@pytest.mark.asyncio
async def test_id_cache_evicts_least_recently_used_instead_of_freezing():
    """缓存满后应淘汰最久未用项，而不是停止缓存（否则长期运行缓存全失效）。"""
    retriever, storage = _make_cache_retriever(2)

    assert await retriever._get_uuid_from_id(1) == "uuid-1"
    assert await retriever._get_uuid_from_id(2) == "uuid-2"
    # 触碰 1 使其成为最近使用项
    assert await retriever._get_uuid_from_id(1) == "uuid-1"
    # 写入 3 应淘汰 2（而不是拒绝写入 3）
    assert await retriever._get_uuid_from_id(3) == "uuid-3"

    assert len(retriever._id_cache) == 2
    assert set(retriever._id_cache) == {1, 3}

    storage.calls.clear()
    await retriever._get_uuid_from_id(3)
    assert storage.calls == [], "新写入的 id 必须命中缓存"


@pytest.mark.asyncio
async def test_id_cache_hit_does_not_requery_storage():
    retriever, storage = _make_cache_retriever(8)

    await retriever._get_uuid_from_id(5)
    await retriever._get_uuid_from_id(5)

    assert storage.calls == [5]
