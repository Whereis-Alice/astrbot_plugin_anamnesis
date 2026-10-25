"""Integration checks for profile directory, persisted settings and scoped tools."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.base.exceptions import ConfigurationError
from astrbot_plugin_anamnesis.core.managers.conversation_manager import (
    ConversationManager,
)
from astrbot_plugin_anamnesis.core.managers.memory_engine_operations import (
    MemoryEngineOperationsMixin,
)
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.core.page_api_modules.settings_handler import (
    SettingsHandler,
)
from astrbot_plugin_anamnesis.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_anamnesis.core.tools import agentic_recall
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore


def event(sender="alice", session="qq:GroupMessage:1", group="旧群", platform="qq"):
    return SimpleNamespace(unified_msg_origin=session, get_sender_id=lambda: sender,
                           get_sender_name=lambda: "爱丽丝", get_platform_name=lambda: platform,
                           get_group_id=lambda: "1" if group else "",
                           message_obj=SimpleNamespace(group=SimpleNamespace(group_id="1", group_name=group)) if group else None)


def fact(key="coffee", value="喜欢咖啡", label="饮品偏好"):
    return {"key": key, "value": value, "label": label, "category": "preference",
            "confidence": .95, "explicit": True}


@pytest_asyncio.fixture
async def profiles(tmp_path):
    store = ConversationStore(str(tmp_path / "conversations.db"))
    await store.initialize()
    manager = UserProfileManager(ConfigManager({"user_profile": {"enabled": True}}), SimpleNamespace(store=store))
    yield manager
    await store.close()


@pytest.mark.asyncio
async def test_directory_user_pagination_preserves_chat_scopes(profiles):
    first, second = event(), event(session="qq:GroupMessage:2", group="新群")
    await profiles.upsert_facts(first, [fact()], source_memory_id=1)
    await profiles.upsert_facts(second, [fact(value="更喜欢茶")], source_memory_id=2)
    await profiles.upsert_facts(event(sender="bob"), [fact()], source_memory_id=3)
    users = await profiles.list_users_for_web(query="alice", limit=1)
    assert users["total"] == 1 and len(users["users"]) == 1
    user = users["users"][0]
    assert user["scope_count"] == 2 and user["fact_count"] == 2
    assert user["last_group_name"] == "新群"
    details = await profiles.list_for_web(identity=user["identity"], limit=1)
    assert details["total"] == 2 and details["has_more"]
    assert details["items"][0]["display_label"] == "饮品偏好"
    assert (await profiles.get_profile(first))[0]["value"] == "喜欢咖啡"
    assert (await profiles.get_profile(second))[0]["value"] == "更喜欢茶"


@pytest.mark.asyncio
async def test_private_chat_keeps_latest_known_group_in_directory(profiles):
    await profiles.upsert_facts(event(), [fact()])
    await profiles.upsert_facts(event(session="qq:FriendMessage:alice", group=None), [fact("city", "住在杭州")])
    user = (await profiles.list_users_for_web())["users"][0]
    assert user["last_session_id"] == "qq:FriendMessage:alice"
    assert user["last_group_name"] == "旧群"


@pytest.mark.asyncio
async def test_archive_profile_restore_does_not_overwrite_correction(profiles):
    owner = event()
    await profiles.upsert_facts(owner, [fact()], source_memory_id=10)
    await profiles.set_source_suppressed([10], True)
    assert await profiles.get_profile(owner) == []
    assert (await profiles.list_users_for_web())["users"][0]["suppressed_fact_count"] == 1
    await profiles.upsert_facts(owner, [fact(value="现在喜欢茶")], source_memory_id=11)
    await profiles.set_source_suppressed([10], False)
    assert (await profiles.get_profile(owner))[0]["value"] == "现在喜欢茶"


@pytest.mark.asyncio
async def test_legacy_identity_backfill_uses_exact_session_suffix(profiles):
    owner = "anamnesis:profile:qq:alias:session:word"
    session = "qq:FriendMessage:9"
    await profiles._ensure_schema()
    await profiles.store.connection.execute(
        "INSERT INTO user_profiles(profile_scope,profile_key,category,value,confidence,source_session_id,updated_at) VALUES(?,?,?,?,?,?,?)",
        (owner + ":session:" + session, "city", "identity", "杭州", .9, session, time.time()))
    await profiles.store.connection.commit()
    profiles._schema_ready = False
    user = (await profiles.list_users_for_web())["users"][0]
    assert user["identity"] == owner
    assert user["sender_id"] == "alias:session:word"


class SavedConfig(dict):
    def __init__(self, *args, fail=False):
        super().__init__(*args)
        self.saved = 0
        self.fail = fail

    def save_config(self):
        if self.fail:
            raise OSError("disk full")
        self.saved += 1


def test_settings_save_preserves_runtime_and_accumulates_pending():
    raw = SavedConfig()
    manager = ConfigManager(raw)
    allowed = {"recall_engine.search_timeout_seconds", "agent_tools.agentic_recall_enabled"}
    manager.save_settings({"recall_engine.search_timeout_seconds": 12}, allowed)
    manager.save_settings({"agent_tools.agentic_recall_enabled": True}, allowed)
    assert raw.saved == 2
    assert manager.get("recall_engine.search_timeout_seconds") == 5
    assert manager.get_saved_settings()["recall_engine"]["search_timeout_seconds"] == 12
    assert manager.get_saved_settings()["agent_tools"]["agentic_recall_enabled"]


def test_settings_rejects_invalid_values_and_rolls_back_failed_save():
    raw = SavedConfig()
    manager = ConfigManager(raw)
    allowed = {"recall_engine.search_timeout_seconds"}
    with pytest.raises(ValueError):
        manager.save_settings({"recall_engine.search_timeout_seconds": -1}, allowed)
    with pytest.raises(ConfigurationError):
        manager.save_settings({"provider_settings.secret": "bad"}, allowed)
    assert raw.saved == 0
    raw.fail = True
    before = dict(raw)
    with pytest.raises(OSError):
        manager.save_settings({"recall_engine.search_timeout_seconds": 20}, allowed)
    assert dict(raw) == before
    assert manager.get_saved_settings()["recall_engine"]["search_timeout_seconds"] == 5


def test_schema_settings_includes_new_fields_and_english_labels():
    handler = SettingsHandler(None)
    data = handler.describe(ConfigManager(), "en")
    fields = {field["path"]: field for section in data["sections"] for field in section["fields"]}
    assert fields["agent_tools.agentic_recall_enabled"]["value"] is False
    assert fields["recall_engine.fusion_mode"]["options"] == ["rrf", "score"]
    assert fields["recall_engine.fusion_mode"]["label"] == "Fusion algorithm"
    assert len(fields["recall_engine.fusion_mode"]["option_labels"]) == 2
    for name, default in {
        "align_webchat_history": True,
        "split_by_day": True,
        "idle_gap_minutes": 180,
        "dedup_enabled": False,
        "dedup_candidate_count": 10,
        "dedup_similarity_threshold": .97,
    }.items():
        assert fields[f"reflection_engine.{name}"]["value"] == default
        assert f"importance_decay.{name}" not in fields


@pytest.mark.asyncio
async def test_agentic_pagination_shared_budget_and_scope_bound_evidence(monkeypatch):
    config = ConfigManager({"agent_tools": {"agentic_recall_enabled": True, "recall_max_calls": 3},
                            "filtering_settings": {"memory_scope_mode": "session"}})
    current = event(session="qq:GroupMessage:1")
    metadata = {"session_id": current.unified_msg_origin, "source_session_id": current.unified_msg_origin,
                "persona_id": "p", "create_time": 100, "participants": ["alice"], "status": "active"}
    memories = [HybridResult(doc_id=i, content="喜欢咖啡", metadata=metadata.copy(), final_score=1/i,
                             rrf_score=0, bm25_score=0, vector_score=0) for i in (1,2,3)]
    engine = SimpleNamespace(search_memories=AsyncMock(return_value=memories),
                             get_memory=AsyncMock(return_value={"text":"喜欢咖啡","metadata":metadata}),
                             resolve_live_facts=AsyncMock(return_value=["喜欢咖啡"]),
                             get_memory_source=AsyncMock(return_value=[{"content":"原话"}]),
                             get_memory_atom_lifecycle=AsyncMock(return_value={"active":1}))
    tool = SimpleNamespace(config_manager=config, memory_engine=engine, context=object())
    monkeypatch.setattr(agentic_recall, "get_persona_id", AsyncMock(return_value="p"))
    first = json.loads(await agentic_recall.agentic_search(tool, current, "咖啡", 1, speaker="alice", exclude_ids=[3]))
    assert first["results"][0]["id"] == 1 and first["next_cursor"]
    second = json.loads(await agentic_recall.agentic_search(tool, current, "咖啡", 1, speaker="alice", exclude_ids=[3], cursor=first["next_cursor"]))
    assert second["results"][0]["id"] == 2
    assert engine.search_memories.await_count == 1
    third = json.loads(await agentic_recall.agentic_evidence(tool, current, 1))
    assert third["source_messages"][0]["content"] == "原话"
    assert json.loads(await agentic_recall.agentic_search(tool, current, "咖啡", 1))["error"] == "recall_budget_exhausted"


@pytest.mark.asyncio
async def test_agentic_evidence_refuses_cross_chat_in_global_recall(monkeypatch):
    config = ConfigManager({"filtering_settings": {"memory_scope_mode": "global"}})
    current = event()
    engine = SimpleNamespace(get_memory=AsyncMock(return_value={"metadata":{"session_id":"anamnesis:global", "source_session_id":"other", "persona_id":"p"}}))
    tool = SimpleNamespace(config_manager=config, memory_engine=engine, context=object())
    monkeypatch.setattr(agentic_recall, "get_persona_id", AsyncMock(return_value="p"))
    data = json.loads(await agentic_recall.agentic_evidence(tool, current, 1))
    assert data["error"] == "memory is not available in current scope"


@pytest.mark.asyncio
async def test_agentic_timeout_and_output_are_bounded():
    tool = SimpleNamespace(config_manager=ConfigManager({"agent_tools":{"recall_budget_seconds":1,"recall_output_max_chars":1000}}))
    current = event()
    state = agentic_recall.request_state(current)
    state["seconds"] = .99
    async def slow(_):
        await asyncio.sleep(.1)
        return {"results":[]}
    assert json.loads(await agentic_recall.with_budget(tool,current,slow))["error"] == "recall_time_budget_exhausted"
    output = agentic_recall.bounded_json({"results":[{"content":"字"*10000} for _ in range(10)]},1000)
    assert len(output) <= 1000 and json.loads(output)["truncated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("old,new", [("喜欢咖啡","不喜欢咖啡"),("每天喝2杯","每天喝3杯")])
async def test_dedup_preserves_negation_and_numeric_changes(old,new):
    engine = MemoryEngineOperationsMixin()
    metadata = {"key_facts":["饮品偏好"],"source_session_id":"s","session_id":"s","persona_id":"p"}
    result = SimpleNamespace(doc_id=1)
    engine.search_memories = AsyncMock(return_value=[result])
    engine.get_memory = AsyncMock(return_value={"text":old,"metadata":metadata})
    engine.add_memory = AsyncMock(return_value=2)
    engine.update_memory = AsyncMock()
    memory_id, previous = await engine.add_memory_conservatively(content=new,metadata=metadata,session_id="s",persona_id="p")
    assert memory_id == 2 and previous is None
    engine.update_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedup_equivalent_facts_keep_id_and_append_provenance():
    engine = MemoryEngineOperationsMixin()
    metadata = {"key_facts":["喜欢咖啡"],"source_session_id":"s","session_id":"s","persona_id":"p", "source_window":{"start_index":0,"end_index":2}}
    engine.search_memories = AsyncMock(return_value=[SimpleNamespace(doc_id=1)])
    engine.get_memory = AsyncMock(return_value={"text":"喜欢咖啡。","metadata":metadata})
    engine.get_memory_source = AsyncMock(return_value=[{"content":"喜欢咖啡"}])
    engine.save_memory_source = AsyncMock()
    engine.add_memory = AsyncMock()
    engine.update_memory = AsyncMock(return_value=True)
    memory_id, previous = await engine.add_memory_conservatively(
        content="喜欢咖啡。",metadata=dict(metadata, source_window={"start_index":2,"end_index":4}),
        session_id="s",persona_id="p",source_messages=[{"content":"还是喜欢咖啡"}])
    assert memory_id == 1 and previous["metadata"] == metadata
    assert len(engine.update_memory.await_args.args[1]["metadata"]["source_windows"]) == 2
    assert len(engine.save_memory_source.await_args.args[1]) == 2
    engine.add_memory.assert_not_awaited()


def test_platform_time_accepts_milliseconds_and_rejects_future():
    now=time.time()
    current=event()
    current.message_obj=SimpleNamespace(timestamp=(now-50)*1000)
    assert ConversationManager._event_timestamp(current) == pytest.approx(now-50)
    current.message_obj.timestamp=now+5000
    assert ConversationManager._event_timestamp(current) is None
