"""SQLite integration for reversible archive and coalesced access counts."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom
from astrbot_plugin_anamnesis.storage.atom_store import AtomStore
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore


async def make_engine(path, *, delete_fails=False):
    async def get_documents(metadata_filters, ids=None, limit=50, offset=0):
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM documents ORDER BY id") as cursor:
                rows=[dict(row) for row in await cursor.fetchall()]
        for row in rows:
            row["metadata"]=json.loads(row["metadata"])
        return [row for row in rows if (ids is None or row["id"] in ids) and
                all(row["metadata"].get(key)==value for key,value in metadata_filters.items())][offset:offset+limit]
    storage=SimpleNamespace(get_documents=get_documents)
    embedding=SimpleNamespace(insert=AsyncMock(), delete=AsyncMock(side_effect=RuntimeError("failed") if delete_fails else None))
    faiss=SimpleNamespace(document_storage=storage,embedding_storage=embedding,
                          embedding_provider=SimpleNamespace(get_embedding=AsyncMock(return_value=[1.,0.])))
    engine=MemoryEngine(str(path),faiss,config={"alias_backfill_on_start":False})
    await engine.initialize()
    for memory_id in (1,2):
        metadata={"session_id":"qq:GroupMessage:1","persona_id":"p","key_facts":["喜欢咖啡"],
                  "source_session_id":"qq:GroupMessage:1","atom_types":["preference"],"status":"active", "access_count":0}
        await engine.db_connection.execute("INSERT INTO documents(id,doc_id,text,metadata) VALUES(?,?,?,?)",
                                           (memory_id,f"uuid-{memory_id}","喜欢咖啡",json.dumps(metadata,ensure_ascii=False)))
    await engine.db_connection.commit()
    return engine


@pytest.mark.asyncio
async def test_access_coalesces_transactions_without_losing_repeat_counts(tmp_path):
    engine=await make_engine(tmp_path/'memory.db')
    engine._queue_access_updates([1,1])
    engine._queue_access_updates([1])
    engine._queue_access_updates([1,2])
    await engine.close()  # Unload flushes queued writes.
    async with aiosqlite.connect(tmp_path/'memory.db') as db:
        async with db.execute("SELECT json_extract(metadata,'$.access_count') FROM documents ORDER BY id") as cursor:
            assert [row[0] for row in await cursor.fetchall()] == [3,1]


@pytest.mark.asyncio
async def test_archive_restore_keeps_atom_expiry_and_profile_tombstone(tmp_path):
    engine=await make_engine(tmp_path/'memory.db')
    engine.atom_store=AtomStore(engine.db_path)
    await engine.atom_store.initialize()
    atom=MemoryAtom(parent_memory_id=1,content="喜欢咖啡",session_id="qq:GroupMessage:1",persona_id="p")
    await engine.atom_store.insert(atom)
    expired=time.time()-10
    await engine.db_connection.execute("UPDATE memory_atoms SET expires_at=?,status='expired' WHERE parent_memory_id=1",(expired,))
    await engine.db_connection.commit()
    store=ConversationStore(str(tmp_path/'conversation.db'))
    await store.initialize()
    profiles=UserProfileManager(ConfigManager({"user_profile":{"enabled":True}}),SimpleNamespace(store=store))
    engine.user_profile_manager=profiles
    owner=SimpleNamespace(unified_msg_origin="qq:GroupMessage:1",get_sender_id=lambda:"alice",get_sender_name=lambda:"爱丽丝",get_platform_name=lambda:"qq")
    await profiles.upsert_facts(owner,[{"key":"coffee","value":"喜欢咖啡","category":"preference","explicit":True,"confidence":.95}],source_memory_id=1)
    assert await engine.archive_memories([1]) == 1
    assert (await engine.get_memory(1))["metadata"]["status"] == "archived"
    assert await profiles.get_profile(owner) == []
    assert await engine.atom_store.get_by_parent(1) == []
    assert await engine.restore_memory(1)
    restored=await engine.atom_store.get_by_parent(1)
    assert len(restored)==1 and restored[0].expires_at==expired and restored[0].status.value=="expired"
    assert await engine.resolve_live_facts(1,"喜欢咖啡",(await engine.get_memory(1))["metadata"]) is None
    assert len(await profiles.get_profile(owner))==1
    await engine.close()
    await store.close()


@pytest.mark.asyncio
async def test_archive_vector_failure_sets_persistent_rebuild_flag(tmp_path):
    engine=await make_engine(tmp_path/'memory.db',delete_fails=True)
    assert await engine.archive_memories([1])==1
    async with engine.db_connection.execute("SELECT value FROM migration_status WHERE key='document_index_rebuild_required'") as cursor:
        assert (await cursor.fetchone())[0]=="true"
    assert (await engine.get_memory(1))["metadata"]["status"]=="archived"
    await engine.close()
