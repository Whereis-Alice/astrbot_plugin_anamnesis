"""Production recall boundary regressions found during the 3.4.0 audit."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_anamnesis.storage.atom_store import AtomStore

from tests.test_memory_engine import _FakeFaissDB


@pytest.mark.asyncio
@pytest.mark.parametrize("graph_enabled", [False, True])
async def test_atomic_memory_write_is_recallable_without_graph_vector_route(tmp_path, graph_enabled):
    path = tmp_path / "independent_atoms.db"
    faiss = _FakeFaissDB()
    faiss._sqlite_path = str(path)
    engine = MemoryEngine(str(path), faiss, config={
        "graph_memory_enabled": graph_enabled, "atom_enabled": True,
        "alias_backfill_on_start": False, "recent_memory_count": 0,
    })
    await engine.initialize()
    try:
        memory_id = await engine.add_memory(
            "likes coffee", session_id="scope", persona_id="p",
            atoms=[MemoryAtom(parent_memory_id=0, content="likes coffee", session_id="scope", persona_id="p")],
        )
        result = await engine.search_memories("coffee", k=2, session_id="scope", persona_id="p")
        assert [item.doc_id for item in result] == [memory_id]
        assert engine.atom_store is not None
        assert len(await engine.atom_store.get_by_parent(memory_id)) == 1
        assert result[0].metadata["retrieved_atom_ids"]
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("recent_count", [1, 2])
async def test_expired_recent_atoms_cannot_displace_live_retrieval_candidates(tmp_path, recent_count):
    engine = MemoryEngine(
        str(tmp_path / "recent.db"),
        SimpleNamespace(document_storage=SimpleNamespace()),
        config={"recent_memory_count": recent_count, "alias_backfill_on_start": False},
    )
    await engine.initialize()
    engine.atom_store = AtomStore(engine.db_path)
    await engine.atom_store.initialize()
    now = time.time()
    metadata = {}
    for memory_id in (1, 2, 3, 4):
        metadata[memory_id] = {
            "session_id": "scope", "persona_id": "p", "status": "active",
            "create_time": now - 10 + memory_id, "importance": .9,
            "atom_types": ["factual"],
        }
        await engine.db_connection.execute(
            "INSERT INTO documents(id,text,metadata) VALUES(?,?,?)",
            (memory_id, f"memory {memory_id}", json.dumps(metadata[memory_id])),
        )
    await engine.db_connection.commit()
    await engine.atom_store.insert_many([
        MemoryAtom(parent_memory_id=i, content=f"memory {i}", session_id="scope", persona_id="p")
        for i in (1, 2, 3, 4)
    ])
    await engine.db_connection.execute(
        "UPDATE memory_atoms SET expires_at=? WHERE parent_memory_id IN (3,4)", (now - 1,),
    )
    await engine.db_connection.commit()
    engine.hybrid_retriever.search = AsyncMock(return_value=[
        HybridResult(i, .9, .1, .5, .9, f"memory {i}", metadata[i]) for i in (1, 2)
    ])
    try:
        result = await engine.search_memories("memory", k=2, session_id="scope", persona_id="p")
        assert {item.doc_id for item in result} == {1, 2}
        assert len(result) == 2
        assert all(item.metadata["retrieved_atom_ids"] for item in result)
    finally:
        await engine.close()
