"""Bounded scoped vector search with request-local embeddings and ID reuse."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace

from ..faiss_async_persist import get_async_persister
from ..recall_persona import NO_PERSONA, persona_matches

query_embeddings = ContextVar("anamnesis_query_embeddings", default=None)


def _metadata(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@asynccontextmanager
async def query_embedding_scope():
    """Share the same provider/query across document and graph routes."""
    if query_embeddings.get() is not None:
        yield
        return
    cache = {}
    token = query_embeddings.set(cache)
    try:
        yield
    finally:
        try:
            for task in cache.values():
                if not task.done():
                    task.cancel()
            if cache:
                async def cleanup():
                    await asyncio.gather(*cache.values(), return_exceptions=True)
                await _complete_search(asyncio.create_task(cleanup()))
        finally:
            query_embeddings.reset(token)


async def _complete_search(task):
    # Cancellation must not release the mutation lock while a FAISS worker
    # still reads the index. Wait for its bounded CPU operation to finish.
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def _native_search(storage, vector, fetch_k):
    persister = get_async_persister(storage)
    lock = getattr(persister, "_mutation_lock", None)

    async def search():
        index_search = getattr(storage.index, "search", None)
        if callable(index_search):
            return await _complete_search(asyncio.create_task(
                asyncio.to_thread(index_search, vector.reshape(1, -1), fetch_k)
            ))
        # Compatibility with storage implementations exposing only async search.
        return await _complete_search(asyncio.create_task(storage.search(vector, fetch_k)))

    if lock is not None:
        async with lock:
            return await search()
    return await search()


async def retrieve_vectors(
    db, query, k, filters, *, source_key="id", max_candidates=8192
):
    """Widen kNN until enough distinct, active, correctly scoped hits exist.

    Native AstrBot storage embeds once and reuses every loaded/missing/filtered
    ID. Older hosts with only retrieve retain their public API fallback; their
    internal embedding calls cannot be controlled without modifying the host.
    """
    if k <= 0:
        return []
    query = query[:2000]
    filters = filters or {}
    # Some host document stores implement equality with '= ?', which cannot
    # match SQL NULL. Load that persona selection without a host predicate,
    # then enforce it here while widening kNN before choosing Top-K.
    storage_filters = {
        key: value
        for key, value in filters.items()
        if not (key == "persona_id" and value in (None, NO_PERSONA))
    }
    try:
        cap = max(1, min(32768, int(max_candidates)))
    except (TypeError, ValueError, OverflowError):
        cap = 8192
    storage = getattr(db, "embedding_storage", None)
    index = getattr(storage, "index", None)
    total = getattr(index, "ntotal", None)
    provider = getattr(db, "embedding_provider", None)
    native = (
        isinstance(total, int)
        and callable(getattr(provider, "get_embedding", None))
        and callable(getattr(getattr(db, "document_storage", None), "get_documents", None))
        and (callable(getattr(index, "search", None)) or callable(getattr(storage, "search", None)))
    )
    if native:
        cap = min(cap, total)
        if cap <= 0:
            return []
        import numpy as np

        cache = query_embeddings.get()
        if cache is None:
            embedding = await provider.get_embedding(query)
        else:
            key = (id(provider), query)
            if key not in cache:
                cache[key] = asyncio.create_task(provider.get_embedding(query))
            embedding = await asyncio.shield(cache[key])
        vector = np.asarray(embedding, dtype=np.float32).ravel()
        if vector.size != storage.dimension or not np.isfinite(vector).all():
            raise ValueError("invalid query embedding")

    fetch_k = min(cap, max(k * (4 if filters else 2), k))
    checked_ids: set[int] = set()
    documents_by_id: dict[int, dict] = {}
    while True:
        if native:
            distances, indices = await _native_search(storage, vector, fetch_k)
            ids = list(dict.fromkeys(int(value) for value in indices[0] if value >= 0))
            new_ids = [doc_id for doc_id in ids if doc_id not in checked_ids]
            if new_ids:
                documents = await db.document_storage.get_documents(
                    metadata_filters=storage_filters, ids=new_ids, limit=len(new_ids)
                )
                checked_ids.update(new_ids)
                documents_by_id.update({int(doc["id"]): doc for doc in documents})
            raw = [SimpleNamespace(
                data=documents_by_id[int(doc_id)], similarity=1.0-float(distance)/2.0
            ) for distance, doc_id in zip(distances[0], indices[0])
                if int(doc_id) in documents_by_id]
            exhausted = len(ids) < fetch_k
        else:
            raw = await db.retrieve(
                query=query, k=fetch_k, fetch_k=fetch_k, rerank=False,
                metadata_filters=storage_filters or None,
            )
            # Scoped adapters may return few hits even with more kNN candidates
            # available, so only an unfiltered short result proves exhaustion.
            exhausted = not filters and len(raw) < fetch_k

        results, seen = [], set()
        for hit in raw:
            metadata = _metadata(hit.data.get("metadata"))
            if str(metadata.get("status") or "active") != "active":
                continue
            if any(
                not persona_matches(metadata.get(key), value)
                if key == "persona_id"
                else metadata.get(key) != value
                for key, value in filters.items()
            ):
                continue
            identity = hit.data.get("id") if source_key == "id" else metadata.get(source_key)
            if identity is None or identity in seen:
                continue
            seen.add(identity)
            results.append(SimpleNamespace(
                data={**hit.data, "metadata": metadata}, similarity=hit.similarity
            ))
            if len(results) >= k:
                return results
        if exhausted or fetch_k >= cap:
            return results
        fetch_k = min(cap, fetch_k * 2)
