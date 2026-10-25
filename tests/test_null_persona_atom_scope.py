"""Real writes and recalls keep atom evidence inside its parent persona scope."""

import time

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.models.memory_atom import AtomType, MemoryAtom
from astrbot_plugin_anamnesis.core.recall_persona import NO_PERSONA
from astrbot_plugin_anamnesis.core.retrieval.temporal_retrieval import search_with_time

from tests.test_memory_transfer_runtime_safety import _api, _engine, _import

SCOPE = "qq:GroupMessage:1"
QUERY = "公开会话的游戏讨论"
FOREIGN = "另一人格的偏好是沙盒游戏"
PERSONAS = [None, NO_PERSONA, "selected-persona"]
MODES = ["semantic", "time", "hybrid"]


@pytest_asyncio.fixture
async def engine(tmp_path):
    instance = await _engine(tmp_path / "atom-persona.db")
    instance.atom_enabled = True
    try:
        yield instance
    finally:
        await instance.close()


def atom(content, persona, instant, *, parent=0, expired=False):
    return MemoryAtom(
        parent_memory_id=parent,
        content=content,
        atom_type=AtomType.PREFERENCE,
        session_id=SCOPE,
        persona_id=persona,
        event_time=instant,
        expires_at=time.time() + (-3600 if expired else 86400),
    )


async def add_parent(engine, persona, atoms, instant, *, content=QUERY):
    return await engine.add_memory(
        content=content,
        session_id=SCOPE,
        persona_id=persona,
        metadata={
            "status": "active",
            "key_facts": [content],
            "create_time": instant - 10 * 86400,
        },
        atoms=atoms,
        preserve_create_time=True,
        preserve_atom_lifecycle=True,
    )


async def recall(engine, persona, mode, instant, *, k=5, candidate_limit=10):
    config = ConfigManager({"filtering_settings": {"use_persona_filtering": True}})
    options = (
        {"start_time": instant - 3600, "end_time": instant + 3600}
        if mode != "semantic"
        else {}
    )
    results, _ = await search_with_time(
        engine,
        QUERY,
        k=k,
        session_id=SCOPE,
        persona_id=persona,
        persona_filtered=True,
        config_manager=config,
        mode=mode,
        record_access=False,
        candidate_limit=candidate_limit,
        **options,
    )
    return results


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("mode", MODES)
async def test_public_add_foreign_atoms_never_return_from_scoped_recall(
    engine, persona, mode
):
    instant = time.time()
    memory_id = await add_parent(
        engine, persona, [atom(FOREIGN, "foreign-persona", instant)], instant
    )
    # The public writer can retain explicit atom provenance. Read policy must
    # enforce the boundary for existing databases as well as new writes.
    stored = await engine.atom_store.get_by_parent(memory_id)
    assert stored[0].persona_id == "foreign-persona"
    assert (await engine.get_memory(memory_id))["metadata"].get("persona_id") == persona
    assert await recall(engine, persona, mode, instant) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("mode", MODES)
async def test_mixed_atoms_keep_matching_and_legacy_null_facts(engine, persona, mode):
    instant = time.time()
    # None and AstrBot's legacy marker are one no-persona boundary. Named
    # parents also retain old atoms whose persona was never populated.
    matching_persona = NO_PERSONA if persona in (None, NO_PERSONA) else persona
    memory_id = await add_parent(
        engine,
        persona,
        [
            atom("合法的合作游戏偏好", matching_persona, instant - 200),
            atom(FOREIGN, "foreign-persona", instant - 100),
        ],
        instant,
    )
    legacy = atom("旧版未填写人格的合法偏好", None, instant - 300, parent=memory_id)
    # AtomStore's public writer preserves old omitted-provenance records;
    # MemoryEngine.add_memory would inherit a named parent's persona.
    await engine.atom_store.insert(legacy, preserve_lifecycle=True)
    assert any(
        item.persona_id is None
        for item in await engine.atom_store.get_by_parent(memory_id)
    )
    results = await recall(engine, persona, mode, instant)
    assert len(results) == 1 and results[0].doc_id == memory_id
    assert results[0].content.splitlines() == [
        "合法的合作游戏偏好",
        "旧版未填写人格的合法偏好",
    ]
    assert results[0].metadata["key_facts"] == results[0].content.splitlines()
    assert FOREIGN not in results[0].metadata["persona_summary"]
    assert await engine.get_memory_atom_lifecycle(memory_id) == {
        "total": 3,
        "active": 2,
        "hidden": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("mode", ["time", "hybrid"])
async def test_foreign_atom_event_time_does_not_consume_temporal_top_k(
    engine, persona, mode
):
    instant = time.time()
    good_id = await add_parent(
        engine, persona, [atom("合法的活动安排", persona, instant - 500)], instant
    )
    await add_parent(
        engine, persona, [atom(FOREIGN, "foreign-persona", instant - 50)], instant
    )
    # The more recent foreign event must be rejected inside candidate SQL,
    # before the one-slot quota, so later lifecycle filtering cannot starve K.
    results = await recall(engine, persona, mode, instant, k=1, candidate_limit=1)
    assert [item.doc_id for item in results] == [good_id]
    assert results[0].content == "合法的活动安排"
    assert results[0].metadata["temporal_time_basis"] == "atom_event_time"


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", PERSONAS)
async def test_lifecycle_uses_parent_persona_without_hiding_legal_named_atoms(
    engine, persona
):
    instant = time.time()
    memory_id = await add_parent(
        engine,
        persona,
        [
            atom("合法当前事实", persona, instant),
            atom(FOREIGN, "foreign-persona", instant),
            atom("合法但已过期的事实", persona, instant, expired=True),
        ],
        instant,
    )
    assert await engine.get_memory_atom_lifecycle(memory_id) == {
        "total": 3,
        "active": 1,
        "hidden": 2,
    }
    memory = await engine.get_memory(memory_id)
    assert await engine.resolve_live_facts(
        memory_id, memory["text"], memory["metadata"]
    ) == ["合法当前事实"]


@pytest.mark.asyncio
async def test_public_import_rebinds_atom_persona_to_null_parent(engine):
    result = await _import(
        _api(engine),
        [
            {
                "content": QUERY,
                "session_id": SCOPE,
                "persona_id": None,
                "metadata": {"key_facts": [QUERY]},
                "atoms": [
                    {
                        "content": "导入的当前偏好",
                        "persona_id": "foreign-persona",
                        "session_id": SCOPE,
                        "expires_at": time.time() + 86400,
                    }
                ],
            }
        ],
    )
    assert result["status"] == "ok", result
    memory_id = result["data"]["imported_ids"][0]
    stored = await engine.atom_store.get_by_parent(memory_id)
    assert len(stored) == 1 and stored[0].persona_id is None
    results = await recall(engine, None, "semantic", time.time())
    assert [item.doc_id for item in results] == [memory_id]
    assert results[0].content == "导入的当前偏好"
