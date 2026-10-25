"""Large atom fan-out must not exceed SQLite's bind parameter limit."""

import sqlite3
import time
from contextlib import asynccontextmanager

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom
from astrbot_plugin_anamnesis.storage.atom_store import AtomStore


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["single", "batch", "forget", "purge"])
async def test_atom_cleanup_handles_many_atoms_per_parent_with_low_bind_limit(tmp_path, monkeypatch, operation):
    store = AtomStore(str(tmp_path / "large.db"))
    await store.initialize()
    await store.insert_many([MemoryAtom(parent_memory_id=1, content=f"source fact {index}") for index in range(1200)])
    await store.insert(MemoryAtom(parent_memory_id=2, content="unrelated live fact"))
    if operation in {"forget", "purge"}:
        async with store._connect() as db:
            await db.execute(
                "UPDATE memory_atoms SET status=?,expires_at=? WHERE parent_memory_id=1",
                ("expired" if operation == "forget" else "forgotten", time.time() - 100 * 86400),
            )
            await db.commit()

    connect = store._connect

    @asynccontextmanager
    async def limited_connection():
        async with connect() as db:
            await db._execute(db._conn.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 1000)
            yield db

    monkeypatch.setattr(store, "_connect", limited_connection)
    if operation == "single":
        affected = await store.delete_by_parent(1)
    elif operation == "batch":
        affected = await store.batch_delete_by_parent([1, 1])
    elif operation == "forget":
        affected = await store.forget_expired_atoms(7)
    else:
        affected = await store.cleanup_forgotten(30)
    assert affected == 1200
    async with store._connect() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM memory_atoms_fts")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM memory_atoms WHERE parent_memory_id=2")).fetchone())[0] == 1
        if operation == "forget":
            assert (await (await db.execute("SELECT COUNT(*) FROM memory_atoms WHERE status='forgotten'")).fetchone())[0] == 1200
        else:
            assert (await (await db.execute("SELECT COUNT(*) FROM memory_atoms WHERE parent_memory_id=1")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_atom_delete_failure_rolls_back_source_and_fts_together(tmp_path):
    store = AtomStore(str(tmp_path / "rollback.db"))
    await store.initialize()
    await store.insert(MemoryAtom(parent_memory_id=1, content="existing source"))
    async with store._connect() as db:
        await db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON memory_atoms BEGIN SELECT RAISE(ABORT,'injected deletion failure'); END")
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="injected deletion failure"):
        await store.delete_by_parent(1)
    assert len(await store.get_by_parent(1)) == 1
    assert [atom.content for atom in await store.search_fts("source")] == ["existing source"]
