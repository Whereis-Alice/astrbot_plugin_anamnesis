"""Archives and repair must preserve captured atom state and provenance."""

import json
import time
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.managers.memory_administration import (
    MemoryAdministration,
)
from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom

from tests.test_graph_atom_disabled_contract import restart
from tests.test_memory_transfer_runtime_safety import _enable_graph, _engine

pytest_plugins = ["tests.test_memory_administration"]


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_before_archive", [False, True])
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("old_archive", [False, True])
async def test_pending_atoms_survive_archive_restart_restore(
    tmp_path, monkeypatch, disabled_before_archive, expired, old_archive
):
    engine = await _engine(tmp_path / "archive-pending.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    insert = engine.atom_store.insert

    async def fail_batch(*args, **kwargs):
        raise RuntimeError("temporary batch failure")

    async def fail_one(atom, *args, **kwargs):
        if atom.content == "retryfact":
            raise RuntimeError("temporary single failure")
        return await insert(atom, *args, **kwargs)

    monkeypatch.setattr(engine.atom_store, "insert_many", fail_batch)
    monkeypatch.setattr(engine.atom_store, "insert", fail_one)
    expiry = time.time() + (-3600 if expired else 86400)
    memory_id = await engine.add_memory(
        "savedfact and retryfact",
        session_id="qq:GroupMessage:1",
        persona_id="p",
        atoms=[
            MemoryAtom(
                parent_memory_id=0, content="savedfact", expires_at=time.time() + 86400
            ),
            MemoryAtom(parent_memory_id=0, content="retryfact", expires_at=expiry),
        ],
        preserve_atom_lifecycle=True,
    )
    if disabled_before_archive:
        await engine.close()
        engine = await restart(engine, {"atom_enabled": False})
    try:
        if old_archive:
            # Reproduce a 3.5.11 archive, which only retained stored atoms.
            async def stored_only(memory_id, snapshots, metadata):
                return snapshots

            monkeypatch.setattr(engine, "_merge_pending_archive_atoms", stored_only)
        assert await engine.archive_memories([memory_id]) == 1
        archived = await engine.get_memory(memory_id)
        original_intent = await (
            await engine.db_connection.execute(
                "SELECT id,payload,retry_count FROM memory_write_ops WHERE memory_id=?",
                (memory_id,),
            )
        ).fetchone()
    finally:
        await engine.close()
    enabled = await restart(engine, {"atom_enabled": True})
    await enabled.atom_lifecycle_manager.stop()
    try:
        if old_archive:
            current = await enabled.get_memory(memory_id)
            assert current["metadata"] == archived["metadata"]
            retained = await (
                await enabled.db_connection.execute(
                    "SELECT status,payload,retry_count FROM memory_write_ops WHERE id=?",
                    (original_intent["id"],),
                )
            ).fetchone()
            assert retained["status"] == "needs_repair"
            assert retained["payload"] == original_intent["payload"]
            assert retained["retry_count"] == original_intent["retry_count"]
        assert await enabled.restore_memory(memory_id)
        atoms = {
            a.content: a for a in await enabled.atom_store.get_by_parent(memory_id)
        }
        assert set(atoms) == {"savedfact", "retryfact"}
        assert atoms["retryfact"].expires_at == expiry
        source = await enabled.get_memory(memory_id)
        assert set(
            await enabled.resolve_live_facts(
                memory_id, source["text"], source["metadata"]
            )
        ) == ({"savedfact"} if expired else {"savedfact", "retryfact"})
        if old_archive:
            assert await enabled._repair_incomplete_write_ops() == 1
            retained = await (
                await enabled.db_connection.execute(
                    "SELECT status,payload FROM memory_write_ops WHERE id=?",
                    (original_intent["id"],),
                )
            ).fetchone()
            assert (
                retained["status"] == "completed"
                and json.loads(retained["payload"]) == {}
            )
    finally:
        await enabled.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["atom_enabled", "graph_memory_atom_enabled"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_legacy_restore_only_classifies_when_enabled(tmp_path, switch, enabled):
    engine = await _engine(tmp_path / "legacy-restore.db")
    await _enable_graph(engine)
    memory_id = await engine.add_memory(
        "fullcontext and shortfact",
        session_id="qq:GroupMessage:1",
        persona_id="p",
        metadata={"status": "archived", "key_facts": ["shortfact"]},
    )
    await engine.close()
    disabled = await restart(engine, {switch: enabled})
    try:
        assert await disabled.restore_memory(memory_id)
        atoms = await disabled.atom_store.get_by_parent(memory_id)
        assert [atom.content for atom in atoms] == (["shortfact"] if enabled else [])
    finally:
        await disabled.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["session", "persona"])
async def test_failed_foreign_atom_stays_outside_parent_scope_after_repair(
    tmp_path, monkeypatch, foreign
):
    engine = await _engine(tmp_path / "foreign-repair.db")
    await engine.atom_lifecycle_manager.stop()
    await _enable_graph(engine)
    insert = engine.atom_store.insert

    async def fail_batch(*args, **kwargs):
        raise RuntimeError("temporary batch failure")

    async def fail_one(atom, *args, **kwargs):
        if atom.content == "foreignsecret":
            raise RuntimeError("temporary single failure")
        return await insert(atom, *args, **kwargs)

    monkeypatch.setattr(engine.atom_store, "insert_many", fail_batch)
    monkeypatch.setattr(engine.atom_store, "insert", fail_one)
    memory_id = await engine.add_memory(
        "savedfact and foreignsecret",
        session_id="qq:GroupMessage:1",
        persona_id="p",
        atoms=[
            MemoryAtom(
                parent_memory_id=0, content="savedfact", expires_at=time.time() + 86400
            ),
            MemoryAtom(
                parent_memory_id=0,
                content="foreignsecret",
                expires_at=time.time() + 86400,
                session_id="qq:GroupMessage:2"
                if foreign == "session"
                else "qq:GroupMessage:1",
                persona_id="other" if foreign == "persona" else "p",
            ),
        ],
        preserve_atom_lifecycle=True,
    )
    await engine.close()
    enabled = await restart(engine, {"atom_enabled": True})
    await enabled.atom_lifecycle_manager.stop()
    try:
        source = await enabled.get_memory(memory_id)
        assert await enabled.resolve_live_facts(
            memory_id, source["text"], source["metadata"]
        ) == ["savedfact"]
        assert not await enabled.graph_store.search_entries_by_bm25(
            '"foreignsecret"', 10
        )
    finally:
        await enabled.close()


@pytest.mark.asyncio
async def test_archive_intent_write_failure_keeps_original_active_memory(
    tmp_path, monkeypatch
):
    engine = await _engine(tmp_path / "intent-failure.db")
    try:
        memory_id = await engine.add_memory("original", session_id="s", persona_id="p")
        source = await engine.get_memory(memory_id)
        monkeypatch.setattr(
            engine,
            "_merge_pending_archive_atoms",
            AsyncMock(side_effect=RuntimeError("journal read failed")),
        )
        with pytest.raises(RuntimeError, match="journal read failed"):
            await engine.archive_memories([memory_id])
        assert await engine.get_memory(memory_id) == source
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_old_recoverable_archive_retains_fingerprint_and_pending_facts(
    administration, monkeypatch, expired
):
    engine = administration.engine
    engine.atom_enabled = True
    insert = engine.atom_store.insert
    expiry = time.time() + (-3600 if expired else 86400)

    async def fail_batch(*args, **kwargs):
        raise RuntimeError("temporary batch failure")

    async def fail_one(atom, *args, **kwargs):
        if atom.content == "retryfact":
            raise RuntimeError("temporary single failure")
        return await insert(atom, *args, **kwargs)

    async def stored_only(memory_id, snapshots, metadata):
        return snapshots

    monkeypatch.setattr(engine.atom_store, "insert_many", fail_batch)
    monkeypatch.setattr(engine.atom_store, "insert", fail_one)
    memory_id = await engine.add_memory(
        "savedfact and retryfact",
        session_id="qq:GroupMessage:1",
        persona_id="p",
        atoms=[
            MemoryAtom(
                parent_memory_id=0, content="savedfact", expires_at=time.time() + 86400
            ),
            MemoryAtom(parent_memory_id=0, content="retryfact", expires_at=expiry),
        ],
        preserve_atom_lifecycle=True,
    )
    monkeypatch.setattr(engine, "_merge_pending_archive_atoms", stored_only)
    deleted = await administration.delete([memory_id], actor="webui:a")
    archived = await administration.snapshot(memory_id)
    await engine.close()
    enabled = await restart(engine, {"atom_enabled": True})
    await enabled.atom_lifecycle_manager.stop()
    fresh = MemoryAdministration(
        enabled, administration.config, administration.processor
    )
    try:
        await fresh.initialize()
        assert await fresh.snapshot(memory_id) == archived
        restored = await fresh.restore(deleted["operation_id"], actor="webui:a")
        assert restored["ids"] == [memory_id] and restored["conflicts"] == []
        atoms = {
            a.content: a for a in await enabled.atom_store.get_by_parent(memory_id)
        }
        assert set(atoms) == {"savedfact", "retryfact"}
        assert atoms["retryfact"].expires_at == expiry
        assert await enabled._repair_incomplete_write_ops() == 1
    finally:
        await enabled.close()
