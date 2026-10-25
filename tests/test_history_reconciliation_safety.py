"""Revert, retry and summary races must never resurrect stale chat state."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from astrbot.api.platform import MessageType
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.event_handler_modules.memory_reflection import (
    MemoryReflection,
)
from astrbot_plugin_anamnesis.core.managers.conversation_manager import (
    ConversationManager,
)
from astrbot_plugin_anamnesis.core.managers.memory_engine_operations import (
    MemoryEngineOperationsMixin,
)
from astrbot_plugin_anamnesis.core.models.conversation_models import Message
from astrbot_plugin_anamnesis.core.utils.history_alignment import split_summary_windows
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore

SESSION = "webchat:FriendMessage:alice"
class Event:
    unified_msg_origin = SESSION
    def __init__(self, checkpoint, cid=None):
        self.checkpoint = checkpoint
        self.cid = cid
    def get_platform_name(self): return "webchat"
    def get_extra(self, key): return self.checkpoint if key == "llm_checkpoint_id" else self.cid if key == "anamnesis_conversation_id" else None
    def get_message_type(self): return MessageType.FRIEND_MESSAGE
    def get_sender_id(self): return "alice"
    def get_sender_name(self): return "Alice"
    def get_self_id(self): return "bot"

@pytest_asyncio.fixture
async def manager(tmp_path):
    store = ConversationStore(str(tmp_path / "conversation.db"))
    await store.initialize()
    manager = ConversationManager(store)
    await manager.add_message(SESSION, "user", "initial", "alice", platform="webchat", metadata={"llm_checkpoint_id": "A"}, timestamp=1)
    try:
        yield manager
    finally:
        await store.close()

async def add(manager, role, checkpoint, timestamp):
    return await manager.add_message(SESSION, role, checkpoint, "alice", platform="webchat", metadata={"llm_checkpoint_id": checkpoint}, timestamp=timestamp)
def contexts(*ids): return [{"role":"_checkpoint", "content":{"id":value}} for value in ids]
def reflection(manager, processor=None, engine=None):
    processor = processor or SimpleNamespace(process_conversation=AsyncMock(return_value=("summary", {"topics": ["tea"]}, .8)), classify_atoms_from_metadata=Mock(return_value=[]))
    engine = engine or SimpleNamespace(add_memory=AsyncMock(return_value=44), undo_summary_write=AsyncMock())
    return MemoryReflection(SimpleNamespace(get_config=lambda: {"timezone":"UTC"}), ConfigManager({}), engine, processor, manager, Mock(), set(), set(), asyncio.Lock())
def messages():
    return [Message(id=index+1, session_id=SESSION, role=role, content=role, sender_id="alice", timestamp=stamp) for index,(role,stamp) in enumerate([("user",1),("assistant",2),("user",86401),("assistant",86402)])]

@pytest.mark.asyncio
async def test_compressed_head_is_not_a_revert(manager):
    await add(manager, "assistant", "A", 2)
    await add(manager, "user", "B", 3)
    await add(manager, "assistant", "B", 4)
    await add(manager, "user", "C", 5)
    assert await manager.align_webchat_history(Event("D"), contexts("B","C")) is None
    assert await manager.store.get_message_count(SESSION) == 5
    assert await manager.get_session_revision(SESSION) == 0

@pytest.mark.asyncio
async def test_revert_deletes_only_tail_and_atomically_clamps_summary_state(manager):
    for role,checkpoint,stamp in [("assistant","A",2),("user","B",3),("assistant","B",4),("user","C",5),("assistant","C",6)]:
        await add(manager, role, checkpoint, stamp)
    await manager.update_session_metadata(SESSION,"last_summarized_index",6)
    await manager.update_session_metadata(SESSION,"pending_summary",{"start_index":4,"end_index":6})
    assert await manager.align_webchat_history(Event("new"),contexts("A","B")) == {"cutoff":4,"revision":0,"removed_message_ids":[5,6]}
    assert await manager.get_session_revision(SESSION) == 1
    assert await manager.get_session_metadata(SESSION,"last_summarized_index") == 4
    assert await manager.get_session_metadata(SESSION,"pending_summary") is None
    assert await manager.store.get_message_count(SESSION) == 4
    assert await manager.add_message_from_event(Event("C"),"assistant","late C") is None

@pytest.mark.asyncio
async def test_late_and_duplicate_assistant_are_rejected_inside_append_lock(manager):
    await add(manager,"user","B",2)
    assert await manager.add_message_from_event(Event("A"),"assistant","obsolete") is None
    assert await manager.add_message_from_event(Event("B"),"assistant","current") is not None
    assert await manager.add_message_from_event(Event("B"),"assistant","duplicate") is None
    assert await manager.store.get_message_count(SESSION) == 3

@pytest.mark.asyncio
async def test_response_after_clear_is_rejected_even_before_next_user(manager):
    await manager.clear_session(SESSION)
    assert await manager.add_message_from_event(Event("A"),"assistant","old reply") is None
    assert await manager.store.get_message_count(SESSION) == 0

@pytest.mark.asyncio
async def test_latest_checkpoint_uses_append_order_when_user_timestamp_is_backdated(manager):
    await add(manager,"assistant","A",1000)
    await add(manager,"user","B",10)
    assert await manager.add_message_from_event(Event("B"),"assistant","current") is not None

@pytest.mark.asyncio
async def test_backdated_message_remains_after_the_summary_cursor(manager):
    await add(manager, "assistant", "A", 1000)
    await manager.update_session_metadata(SESSION, "last_summarized_index", 2)
    late = await add(manager, "user", "B", 10)
    tail = await manager.get_messages_range(SESSION, start_index=2, end_index=3)
    assert [message.id for message in tail] == [late.id]

@pytest.mark.asyncio
async def test_backdated_checkpoint_revert_preserves_the_allowed_turn(manager):
    await add(manager, "assistant", "A", 1000)
    await add(manager, "user", "B", 10)
    await add(manager, "assistant", "B", 2000)
    result = await manager.align_webchat_history(Event("retry"), contexts("A"))
    assert result == {"cutoff": 2, "revision": 0, "removed_message_ids": [3, 4]}
    remaining = await manager.get_messages_range(SESSION)
    assert [message.metadata["llm_checkpoint_id"] for message in remaining] == ["A", "A"]

@pytest.mark.asyncio
async def test_trim_summarized_prefix_never_deletes_a_backdated_new_message(manager):
    await add(manager, "assistant", "A", 1000)
    await manager.update_session_metadata(SESSION, "last_summarized_index", 2)
    late = await add(manager, "user", "B", 10)
    deleted = await manager.store.trim_session_messages(SESSION, 2)
    assert deleted == 2
    remaining = await manager.get_messages_range(SESSION)
    assert [message.id for message in remaining] == [late.id]

@pytest.mark.asyncio
async def test_trim_invalidates_old_summary_cas_and_shifts_pending_window(manager):
    await add(manager, "assistant", "A", 2)
    await add(manager, "user", "B", 3)
    await add(manager, "assistant", "B", 4)
    await manager.update_session_metadata(SESSION, "last_summarized_index", 2)
    await manager.update_session_metadata(SESSION, "pending_summary", {"start_index": 2, "end_index": 4, "retry_count": 1})
    assert await manager.store.trim_session_messages(SESSION, 2) == 2
    assert await manager.get_session_revision(SESSION) == 1
    assert await manager.get_session_metadata(SESSION, "pending_summary") == {"start_index": 0, "end_index": 2, "retry_count": 1}
    assert not await manager.update_session_metadata_batch_if_revision(SESSION, {"last_summarized_index": 4}, 0)

@pytest.mark.asyncio
async def test_trim_during_summary_write_compensates_old_window(manager):
    await add(manager, "assistant", "A", 2)
    await add(manager, "user", "B", 3)
    await add(manager, "assistant", "B", 4)
    await manager.update_session_metadata(SESSION, "last_summarized_index", 2)
    window = await manager.get_messages_range(SESSION, 2, 4)
    async def write(**kwargs):
        await manager.store.trim_session_messages(SESSION, 2)
        return 44
    engine = SimpleNamespace(add_memory=AsyncMock(side_effect=write), undo_summary_write=AsyncMock())
    worker = reflection(manager, engine=engine)
    await worker._storage_task(SESSION, window, None, 2, 4, 0, expected_revision=0)
    engine.undo_summary_write.assert_awaited_once_with(44, None)
    assert await manager.get_session_metadata(SESSION, "last_summarized_index") == 0

@pytest.mark.asyncio
async def test_trim_rolls_back_deleted_messages_if_metadata_write_fails(manager):
    await add(manager, "assistant", "A", 2)
    await manager.update_session_metadata(SESSION, "last_summarized_index", 2)
    await manager.store.connection.execute("CREATE TRIGGER reject_trim_metadata BEFORE UPDATE ON sessions BEGIN SELECT RAISE(ABORT,'trim failure'); END")
    await manager.store.connection.commit()
    with pytest.raises(Exception, match="trim failure"):
        await manager.store.trim_session_messages(SESSION, 2)
    assert not manager.store.connection.in_transaction
    assert await manager.store.get_message_count(SESSION) == 2

@pytest.mark.asyncio
async def test_successive_reverts_remove_earlier_revision_summary_by_message_ids(manager):
    for role, checkpoint, stamp in [("assistant", "A", 2), ("user", "B", 3),
                                    ("assistant", "B", 4), ("user", "C", 5),
                                    ("assistant", "C", 6)]:
        await add(manager, role, checkpoint, stamp)
    connection = manager.store.connection
    await connection.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, metadata TEXT)")
    engine = MemoryEngineOperationsMixin()
    engine.db_connection = connection
    async def delete(memory_id):
        cursor = await connection.execute("DELETE FROM documents WHERE id=?", (memory_id,))
        await connection.commit()
        return cursor.rowcount == 1
    engine.delete_memory = delete
    async def summary(memory_id, revision, ids, end):
        await connection.execute("INSERT INTO documents VALUES (?,?)", (memory_id, json.dumps({
            "source_session_id": SESSION, "source_revision": revision,
            "source_message_ids": ids, "source_window": {"end_index": end},
        })))
        await connection.commit()
    await summary(101, 0, [1, 2], 2)
    await summary(102, 0, [3, 4], 4)
    await summary(103, 0, [5, 6], 6)
    first = await manager.align_webchat_history(Event("retry"), contexts("A", "B"))
    assert await engine.delete_reverted_summaries(SESSION, first["cutoff"], first["revision"],
                                                 removed_message_ids=first["removed_message_ids"]) == [103]
    new_user = await add(manager, "user", "C2", 7)
    new_assistant = await add(manager, "assistant", "C2", 8)
    await summary(104, 1, [new_user.id, new_assistant.id], 6)
    second = await manager.align_webchat_history(Event("retry2"), contexts("A"))
    deleted = await engine.delete_reverted_summaries(SESSION, second["cutoff"], second["revision"],
                                                     removed_message_ids=second["removed_message_ids"])
    assert sorted(deleted) == [102, 104]
    async with connection.execute("SELECT id FROM documents") as cursor:
        assert [row[0] for row in await cursor.fetchall()] == [101]

@pytest.mark.asyncio
async def test_summary_provenance_keeps_stable_ids_without_retaining_raw_text(manager):
    worker = reflection(manager)
    window = messages()[:2]
    worker.memory_processor.process_conversation.return_value = ("summary", {}, .5)
    await worker._storage_task(SESSION, window, None, 0, 2, 0, expected_revision=0)
    write = worker.memory_engine.add_memory.await_args.kwargs
    assert write["metadata"]["source_message_ids"] == [message.id for message in window]
    assert write["source_messages"] is None

@pytest.mark.asyncio
async def test_corrupt_legacy_provenance_does_not_authorize_deletion(manager):
    await manager.store.connection.execute("UPDATE messages SET metadata='not json' WHERE session_id=?",(SESSION,))
    await manager.store.connection.commit()
    assert await manager.align_webchat_history(Event("B"),[]) is None
    assert await manager.store.get_message_count(SESSION) == 1

@pytest.mark.asyncio
async def test_same_umo_new_conversation_preserves_long_term_and_rejects_old_response(manager):
    await manager.clear_session(SESSION)
    await manager.add_message_from_event(Event("A","old-cid"),"user","old question")
    await manager.add_message_from_event(Event("A","old-cid"),"assistant","old reply")
    revision = await manager.get_session_revision(SESSION)
    await manager.update_session_metadata(SESSION,"pending_summary",{"start_index":0,"end_index":2})
    result = await manager.align_webchat_history(Event("B","new-cid"),[],conversation_id="new-cid")
    assert result == {"cutoff":0,"revision":revision,"preserve_long_term":True}
    assert await manager.get_session_metadata(SESSION,"pending_summary") is None
    assert await manager.store.get_message_count(SESSION) == 0
    await manager.add_message_from_event(Event("B","new-cid"),"user","new question")
    assert await manager.add_message_from_event(Event("A","old-cid"),"assistant","late old reply") is None
    assert await manager.store.get_message_count(SESSION) == 1

@pytest.mark.asyncio
async def test_same_conversation_first_turn_retry_is_a_true_revert(manager):
    await manager.clear_session(SESSION)
    await manager.add_message_from_event(Event("A","same-cid"),"user","question")
    await manager.add_message_from_event(Event("A","same-cid"),"assistant","reply")
    revision = await manager.get_session_revision(SESSION)
    result = await manager.align_webchat_history(Event("retry","same-cid"),[],conversation_id="same-cid")
    assert result == {"cutoff":0,"revision":revision,"removed_message_ids":[2,3]}
    assert await manager.store.get_message_count(SESSION) == 0

@pytest.mark.asyncio
async def test_missing_conversation_id_empty_history_preserves_long_term(manager):
    await manager.update_session_metadata(SESSION,"pending_summary",{"start_index":0,"end_index":1})
    assert await manager.align_webchat_history(Event("B"),[]) == {"cutoff":0,"revision":0,"preserve_long_term":True}
    assert await manager.get_session_revision(SESSION) == 1
    assert await manager.get_session_metadata(SESSION,"pending_summary") is None
    assert await manager.store.get_message_count(SESSION) == 0

@pytest.mark.asyncio
async def test_batch_cas_cannot_clear_a_new_revision_pending_summary(manager):
    await manager.bump_session_revision(SESSION)
    pending = {"start_index":0,"end_index":2,"retry_count":1}
    await manager.update_session_metadata(SESSION,"pending_summary",pending)
    assert not await manager.update_session_metadata_batch_if_revision(SESSION,{"last_summarized_index":10,"pending_summary":None},0)
    assert await manager.get_session_metadata(SESSION,"pending_summary") == pending
    assert await manager.get_session_metadata(SESSION,"last_summarized_index",0) == 0
    assert await manager.update_session_metadata_batch_if_revision(SESSION,{"last_summarized_index":2,"pending_summary":None},1)
    assert await manager.get_session_metadata(SESSION,"last_summarized_index") == 2

@pytest.mark.asyncio
async def test_clear_serializes_old_cas_and_new_message_without_revision_gap(manager):
    await manager.store._write_lock.acquire()
    clearing = asyncio.create_task(manager.clear_session(SESSION))
    await asyncio.sleep(0)
    old_commit = asyncio.create_task(manager.update_session_metadata_batch_if_revision(SESSION,{"last_summarized_index":8,"pending_summary":None},0))
    new_user = asyncio.create_task(manager.add_message_from_event(Event("B"),"user","new chat"))
    manager.store._write_lock.release()
    await clearing
    assert not await old_commit
    await new_user
    assert await manager.get_session_revision(SESSION) == 1
    assert await manager.get_session_metadata(SESSION,"last_summarized_index",0) == 0
    assert await manager.store.get_message_count(SESSION) == 1

@pytest.mark.asyncio
async def test_failed_cas_raises_and_rolls_back_all_fields(manager):
    await manager.store.connection.execute("CREATE TRIGGER reject_metadata BEFORE UPDATE ON sessions BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    await manager.store.connection.commit()
    with pytest.raises(Exception,match="injected failure"):
        await manager.update_session_metadata_batch_if_revision(SESSION,{"last_summarized_index":2,"pending_summary":None},0)
    assert await manager.get_session_metadata(SESSION,"last_summarized_index",0) == 0
    assert not manager.store.connection.in_transaction

@pytest.mark.asyncio
async def test_old_summary_written_during_retry_is_compensated(manager):
    engine = SimpleNamespace(undo_summary_write=AsyncMock())
    async def write(**kwargs):
        await manager.bump_session_revision(SESSION)
        return 44
    engine.add_memory = AsyncMock(side_effect=write)
    worker = reflection(manager,engine=engine)
    await worker._storage_task(SESSION,messages()[:2],None,0,2,0,expected_revision=0)
    engine.undo_summary_write.assert_awaited_once_with(44,None)
    assert await manager.get_session_metadata(SESSION,"last_summarized_index",0) == 0
    assert await manager.get_session_metadata(SESSION,"pending_summary") is None

@pytest.mark.asyncio
async def test_provider_failure_from_old_revision_cannot_restore_pending(manager):
    async def provider(**kwargs):
        await manager.bump_session_revision(SESSION)
        raise RuntimeError("late failure")
    processor = SimpleNamespace(process_conversation=AsyncMock(side_effect=provider))
    worker = reflection(manager,processor=processor)
    await worker._storage_task(SESSION,messages()[:2],None,0,2,0,expected_revision=0)
    assert await manager.get_session_metadata(SESSION,"pending_summary") is None

@pytest.mark.asyncio
@pytest.mark.parametrize("missing_engine",[True,False])
async def test_missing_engine_or_missing_written_id_never_advances_progress(manager,missing_engine):
    worker = reflection(manager)
    if missing_engine:
        worker.memory_engine = None
    else:
        worker.memory_engine.add_memory.return_value = None
    await worker._storage_task(SESSION,messages()[:2],None,0,2,0,expected_revision=0)
    assert await manager.get_session_metadata(SESSION,"last_summarized_index",0) == 0
    assert await manager.get_session_metadata(SESSION,"pending_summary") == {"start_index":0,"end_index":2,"retry_count":1}

@pytest.mark.asyncio
async def test_metadata_retry_cannot_resurrect_state_after_clear(manager):
    worker = reflection(manager)
    original = manager.update_session_metadata_batch_if_revision
    attempts = 0
    async def flaky(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await manager.clear_session(SESSION)
            raise RuntimeError("retry after clear")
        return await original(*args)
    manager.update_session_metadata_batch_if_revision = AsyncMock(side_effect=flaky)
    await worker._storage_task(SESSION,messages()[:2],None,0,2,0,expected_revision=0)
    worker.memory_engine.undo_summary_write.assert_awaited_once_with(44,None)
    assert await manager.get_session_metadata(SESSION,"last_summarized_index",0) == 0
    assert await manager.get_session_revision(SESSION) == 1

@pytest.mark.asyncio
async def test_failed_first_window_does_not_advance_into_next_day(manager):
    processor = SimpleNamespace(process_conversation=AsyncMock(side_effect=RuntimeError("provider offline")))
    worker = reflection(manager,processor=processor)
    await worker._storage_windows_task(SESSION,messages(),None,0,4,0,expected_revision=0)
    processor.process_conversation.assert_awaited_once()
    assert await manager.get_session_metadata(SESSION,"pending_summary") == {"start_index":0,"end_index":2,"retry_count":1}
    assert await manager.get_session_metadata(SESSION,"last_summarized_index",0) == 0

@pytest.mark.asyncio
async def test_later_window_gets_its_own_retry_budget(manager):
    processor = SimpleNamespace(process_conversation=AsyncMock(side_effect=[("summary",{},.8),RuntimeError("second day offline")]),classify_atoms_from_metadata=Mock(return_value=[]))
    worker = reflection(manager,processor=processor)
    await worker._storage_windows_task(SESSION,messages(),None,0,4,2,expected_revision=0)
    assert await manager.get_session_metadata(SESSION,"last_summarized_index") == 2
    assert await manager.get_session_metadata(SESSION,"pending_summary") == {"start_index":2,"end_index":4,"retry_count":1}

def test_cross_midnight_question_and_answer_stay_in_one_window():
    def stamp(value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
    history = [{"role":"user","timestamp":stamp("2026-10-24T23:58")},{"role":"assistant","timestamp":stamp("2026-10-25T00:03")},{"role":"user","timestamp":stamp("2026-10-25T18:00")},{"role":"assistant","timestamp":stamp("2026-10-25T18:01")}]
    assert split_summary_windows(history,tz=timezone.utc) == [(0,2),(2,4)]

@pytest.mark.asyncio
@pytest.mark.parametrize("split_by_day,idle_gap_minutes,new_timestamp",[(True,0,86401),(False,10,1000)])
async def test_sealed_short_stretch_triggers_before_ten_rounds_and_keeps_current_open(
    manager, monkeypatch, split_by_day, idle_gap_minutes, new_timestamp,
):
    await add(manager,"assistant","A",2)
    await add(manager,"user","B",new_timestamp)
    worker = reflection(manager)
    worker.config_manager = ConfigManager({"reflection_engine":{"summary_trigger_rounds":10,"split_by_day":split_by_day,"idle_gap_minutes":idle_gap_minutes}})
    worker.message_utils = SimpleNamespace(enforce_message_limit=AsyncMock())
    monkeypatch.setattr("astrbot_plugin_anamnesis.core.event_handler_modules.memory_reflection.get_persona_id",AsyncMock(return_value=None))
    response = SimpleNamespace(role="assistant",tools_call_name=None,tools_call_extra_content=None,completion_text="current reply")
    await worker.handle_memory_reflection(Event("B"),response)
    tasks = list(worker._storage_tasks)
    assert tasks, "sealed stretch should schedule a summary below the ten-round threshold"
    await asyncio.gather(*tasks)
    worker.memory_processor.process_conversation.assert_awaited_once()
    assert len(worker.memory_processor.process_conversation.await_args.kwargs["messages"]) == 2
    assert await manager.get_session_metadata(SESSION,"last_summarized_index") == 2
    assert await manager.store.get_message_count(SESSION) == 4

@pytest.mark.asyncio
async def test_active_short_stretch_without_boundary_keeps_normal_round_threshold(manager,monkeypatch):
    worker = reflection(manager)
    worker.message_utils = SimpleNamespace(enforce_message_limit=AsyncMock())
    monkeypatch.setattr("astrbot_plugin_anamnesis.core.event_handler_modules.memory_reflection.get_persona_id",AsyncMock(return_value=None))
    response = SimpleNamespace(role="assistant",tools_call_name=None,tools_call_extra_content=None,completion_text="current reply")
    await worker.handle_memory_reflection(Event("A"),response)
    assert not worker._storage_tasks
    worker.memory_processor.process_conversation.assert_not_awaited()
