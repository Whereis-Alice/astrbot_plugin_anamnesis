"""Explicit deletions invalidate older extractions without blocking new messages."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.core.models.conversation_models import Message
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore


def event(sender="alice", session="qq:GroupMessage:42"):
    return SimpleNamespace(
        unified_msg_origin=session,
        get_sender_id=lambda: sender,
        get_sender_name=lambda: sender,
        get_platform_name=lambda: "qq",
        get_group_id=lambda: "42",
        message_obj=SimpleNamespace(group=None),
    )


def fact(key="drink", value="喜欢茶", quote="我喜欢喝茶"):
    return {
        "key": key,
        "value": value,
        "category": "preference",
        "explicit": True,
        "confidence": 0.95,
        "evidence": quote,
    }


@pytest_asyncio.fixture
async def manager(tmp_path):
    store = ConversationStore(str(tmp_path / "profiles.db"))
    await store.initialize()
    result = UserProfileManager(
        ConfigManager({"user_profile": {"enabled": True}}), SimpleNamespace(store=store)
    )
    try:
        yield result
    finally:
        await store.close()


def extraction(manager, owner=None, facts=None, source=11):
    owner = owner or event()
    facts = facts or [fact()]
    started, release = asyncio.Event(), asyncio.Event()

    async def llm(*_args, **_kwargs):
        started.set()
        await release.wait()
        return json.dumps({"facts": facts}, ensure_ascii=False)

    messages = [
        {
            "role": "user",
            "sender_id": owner.get_sender_id(),
            "content": "我喜欢喝茶。我喜欢吃面。",
        }
    ]
    task = asyncio.create_task(
        manager.update_from_messages(
            owner,
            messages,
            SimpleNamespace(_call_llm_with_retry=llm),
            source_memory_id=source,
        )
    )
    return task, started, release


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["web", "chat_key", "chat_all"])
async def test_deleted_profile_is_not_rebuilt_by_older_extraction(manager, kind):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    task, started, release = extraction(manager)
    try:
        await asyncio.wait_for(started.wait(), 2)
        if kind == "web":
            removed = await manager.delete_for_web(
                manager.scope_for_event(event()), "drink"
            )
        else:
            removed = await manager.delete_profile(
                event(), "drink" if kind == "chat_key" else None
            )
        assert removed == 1
        release.set()
        assert await asyncio.wait_for(task, 2) == 0
        assert await manager.get_profile(event()) == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_key_deletion_keeps_unrelated_facts_from_same_extraction(manager):
    food = fact("food", "喜欢面食", "我喜欢吃面")
    await manager.upsert_facts(event(), [fact(), food], source_memory_id=10)
    task, started, release = extraction(manager, facts=[fact(), food])
    try:
        await asyncio.wait_for(started.wait(), 2)
        await manager.delete_for_web(manager.scope_for_event(event()), "drink")
        release.set()
        assert await asyncio.wait_for(task, 2) == 1
        assert [row["profile_key"] for row in await manager.get_profile(event())] == [
            "food"
        ]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancellation_after_delete_commit_still_invalidates_old_extraction(
    manager,
):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    task, started, release = extraction(manager)
    connection = manager.store.connection
    commit = connection.commit
    committed, finish_commit = asyncio.Event(), asyncio.Event()
    deleting = None

    async def committed_then_delayed():
        await commit()
        committed.set()
        await finish_commit.wait()

    try:
        await asyncio.wait_for(started.wait(), 2)
        connection.commit = committed_then_delayed
        deleting = asyncio.create_task(
            manager.delete_for_web(manager.scope_for_event(event()), "drink")
        )
        await asyncio.wait_for(committed.wait(), 2)
        deleting.cancel()
        finish_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(deleting, 2)
        connection.commit = commit
        assert await manager.get_profile(event()) == []
        release.set()
        assert await asyncio.wait_for(task, 2) == 0
        assert await manager.get_profile(event()) == []
    finally:
        connection.commit = commit
        finish_commit.set()
        release.set()
        await asyncio.gather(
            *(item for item in (task, deleting) if item is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_failed_delete_commit_preserves_valid_inflight_extraction(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    task, started, release = extraction(manager)
    connection = manager.store.connection
    commit = connection.commit

    async def failed_commit():
        raise RuntimeError("commit failed")

    try:
        await asyncio.wait_for(started.wait(), 2)
        connection.commit = failed_commit
        with pytest.raises(RuntimeError, match="commit failed"):
            await manager.delete_for_web(manager.scope_for_event(event()), "drink")
        connection.commit = commit
        release.set()
        assert await asyncio.wait_for(task, 2) == 1
        row = (await manager.get_profile(event()))[0]
        assert {item["source_memory_id"] for item in row["evidence"]} == {10, 11}
    finally:
        connection.commit = commit
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "suppress"])
async def test_source_deleted_before_profile_write_cannot_add_late_evidence(
    manager, action
):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    task, started, release = extraction(manager, source=11)
    try:
        await asyncio.wait_for(started.wait(), 2)
        if action == "delete":
            await manager.delete_by_source_memory(11)
        else:
            await manager.set_source_suppressed([11], True)
        release.set()
        assert await asyncio.wait_for(task, 2) == 0
        row = (await manager.get_profile(event()))[0]
        assert [item["source_memory_id"] for item in row["evidence"]] == [10]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_new_extraction_after_deletion_can_record_new_self_report(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    old, started, release = extraction(manager)
    new = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        await manager.delete_profile(event())
        release.set()
        assert await asyncio.wait_for(old, 2) == 0
        new, new_started, new_release = extraction(manager, source=12)
        await asyncio.wait_for(new_started.wait(), 2)
        new_release.set()
        assert await asyncio.wait_for(new, 2) == 1
        assert (await manager.get_profile(event()))[0]["source_memory_id"] == 12
    finally:
        release.set()
        if new is not None:
            new_release.set()
        await asyncio.gather(
            *(task for task in (old, new) if task is not None), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("other", [event("bob"), event(session="qq:GroupMessage:43")])
async def test_deleting_other_scope_does_not_cancel_current_user_extraction(
    manager, other
):
    await manager.upsert_facts(other, [fact()], source_memory_id=10)
    task, started, release = extraction(manager)
    try:
        await asyncio.wait_for(started.wait(), 2)
        await manager.delete_profile(other)
        release.set()
        assert await asyncio.wait_for(task, 2) == 1
        assert len(await manager.get_profile(event())) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_failed_profile_schema_does_not_commit_orphan_claim_with_next_message(
    manager, error_type
):
    connection = manager.store.connection
    await connection.execute("""CREATE TABLE user_profiles (
        profile_scope TEXT NOT NULL,profile_key TEXT NOT NULL,category TEXT NOT NULL,
        value TEXT NOT NULL,confidence REAL NOT NULL,source_memory_id INTEGER,
        source_session_id TEXT,source_group_id TEXT,source_group_name TEXT,
        source_sender_name TEXT,updated_at REAL NOT NULL,expires_at REAL,
        PRIMARY KEY(profile_scope,profile_key))""")
    for key in ("drink", "food"):
        await connection.execute(
            "INSERT INTO user_profiles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                manager.scope_for_event(event()),
                key,
                "preference",
                "legacy",
                0.9,
                10,
                event().unified_msg_origin,
                None,
                None,
                "alice",
                1,
                None,
            ),
        )
    await connection.commit()
    insert = manager._insert_claim

    async def failed_insert(*args):
        await insert(*args)
        raise error_type()

    manager._insert_claim = failed_insert
    with pytest.raises(error_type):
        await manager._ensure_schema()
    transaction_left_open = connection.in_transaction
    await manager.store.add_message(
        Message(
            id=None,
            session_id=event().unified_msg_origin,
            role="user",
            content="下一条正常消息",
            sender_id="alice",
            platform="qq",
        )
    )
    async with connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='user_profile_claims'"
    ) as cursor:
        table_exists = (await cursor.fetchone())[0]
    failed_claims = 0
    if table_exists:
        async with connection.execute(
            "SELECT COUNT(*) FROM user_profile_claims"
        ) as cursor:
            failed_claims = (await cursor.fetchone())[0]
    manager._insert_claim = insert
    await manager._ensure_schema()
    async with connection.execute("SELECT COUNT(*) FROM user_profile_claims") as cursor:
        claims_after_retry = (await cursor.fetchone())[0]
    assert not transaction_left_open
    assert failed_claims == 0
    assert claims_after_retry == 2
