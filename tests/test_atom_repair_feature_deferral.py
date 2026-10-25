"""Disabling atomization postpones captured writes without spending retries."""

import json
import time

import pytest

from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom

from tests.test_graph_atom_disabled_contract import restart
from tests.test_memory_transfer_runtime_safety import _enable_graph, _engine


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["atom_enabled", "graph_memory_atom_enabled"])
@pytest.mark.parametrize("graph_enabled", [False, True])
@pytest.mark.parametrize("expired", [False, True])
async def test_disabled_restarts_preserve_pending_atom_payload_retry_and_absolute_ttl(
    tmp_path, monkeypatch, switch, graph_enabled, expired
):
    engine = await _engine(tmp_path / "pending.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    insert = engine.atom_store.insert

    async def failed_batch(*args, **kwargs):
        raise RuntimeError("temporary batch failure")

    async def failed_one(atom, *args, **kwargs):
        if atom.content == "retryfact":
            raise RuntimeError("temporary single atom failure")
        return await insert(atom, *args, **kwargs)

    expiry = time.time() + (-3600 if expired else 86400)
    monkeypatch.setattr(engine.atom_store, "insert_many", failed_batch)
    monkeypatch.setattr(engine.atom_store, "insert", failed_one)
    try:
        memory_id = await engine.add_memory(
            "savedfact and retryfact", session_id="qq:GroupMessage:1",
            metadata={"key_facts": ["savedfact", "retryfact"]},
            atoms=[
                MemoryAtom(parent_memory_id=0, content="savedfact", expires_at=time.time()+86400),
                MemoryAtom(parent_memory_id=0, content="retryfact", expires_at=expiry),
            ],
            preserve_atom_lifecycle=True,
        )
        assert {atom.content for atom in await engine.atom_store.get_by_parent(memory_id)} == {"savedfact"}
        original = await (await engine.db_connection.execute(
            "SELECT id,payload,retry_count FROM memory_write_ops WHERE memory_id=?", (memory_id,),
        )).fetchone()
        assert json.loads(original["payload"])["failed_atoms"][0]["expires_at"] == expiry
    finally:
        await engine.close()

    previous = engine
    for _ in range(4):
        disabled = await restart(previous, {switch: False, "graph_memory_enabled": graph_enabled})
        try:
            row = await (await disabled.db_connection.execute(
                "SELECT status,step,payload,retry_count FROM memory_write_ops WHERE id=?", (original["id"],),
            )).fetchone()
            assert row["status"] == "needs_repair"
            assert row["step"] == ("atoms_deferred" if graph_enabled else "atoms_deferred_no_graph")
            assert row["payload"] == original["payload"] and row["retry_count"] == original["retry_count"]
            retained = await disabled.prune_write_ops(keep_days=1e-9, keep_max=1, failed_keep_days=1e-9)
            assert retained["remaining_unfinished"] == 1
            record = await disabled.get_memory(memory_id)
            assert await disabled.resolve_live_facts(memory_id, record["text"], record["metadata"]) == ["savedfact and retryfact"]
            if graph_enabled:
                assert await disabled.graph_store.search_entries_by_bm25('"retryfact"', 10)
        finally:
            await disabled.close()
        previous = disabled

    if not graph_enabled:
        graph_enabled_later = await restart(previous, {switch: False})
        try:
            row = await (await graph_enabled_later.db_connection.execute(
                "SELECT status,step,payload,retry_count FROM memory_write_ops WHERE id=?", (original["id"],),
            )).fetchone()
            assert row["status"] == "needs_repair" and row["step"] == "atoms_deferred"
            assert row["payload"] == original["payload"] and row["retry_count"] == original["retry_count"]
            assert await graph_enabled_later.graph_store.search_entries_by_bm25('"retryfact"', 10)
        finally:
            await graph_enabled_later.close()
        previous = graph_enabled_later

    enabled = await restart(previous, {switch: True})
    await enabled.atom_lifecycle_manager.stop()
    try:
        atoms = {atom.content: atom for atom in await enabled.atom_store.get_by_parent(memory_id)}
        assert set(atoms) == {"savedfact", "retryfact"}
        assert atoms["retryfact"].expires_at == expiry
        row = await (await enabled.db_connection.execute(
            "SELECT status,payload,retry_count FROM memory_write_ops WHERE id=?", (original["id"],),
        )).fetchone()
        assert row["status"] == "completed" and json.loads(row["payload"]) == {}
        assert row["retry_count"] == original["retry_count"]
        record = await enabled.get_memory(memory_id)
        facts = await enabled.resolve_live_facts(memory_id, record["text"], record["metadata"])
        assert set(facts or []) == ({"savedfact"} if expired else {"savedfact", "retryfact"})
        assert bool(await enabled.graph_store.search_entries_by_bm25('"retryfact"', 10)) is not expired
    finally:
        await enabled.close()
