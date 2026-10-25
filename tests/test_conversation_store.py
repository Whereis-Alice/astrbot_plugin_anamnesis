"""
Tests for ConversationStore persistence behaviors.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from astrbot_plugin_anamnesis.core.models.conversation_models import Message
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_session", [False, True])
async def test_failed_add_message_rolls_back_before_next_write(tmp_path, existing_session):
    store = ConversationStore(str(tmp_path / "failed-add.db"))
    await store.initialize()
    try:
        if existing_session:
            await store.add_message(
                Message(
                    id=0,
                    session_id="failed",
                    role="user",
                    content="retained",
                    sender_id="original",
                )
            )
        await store.connection.execute(
            """
            CREATE TRIGGER reject_message_count BEFORE UPDATE OF message_count ON sessions
            WHEN NEW.session_id = 'failed'
            BEGIN SELECT RAISE(ABORT, 'count update failed'); END
            """
        )
        await store.connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="count update failed"):
            await store.add_message(
                Message(
                    id=0,
                    session_id="failed",
                    role="user",
                    content="must roll back",
                    sender_id="failed-sender",
                )
            )
        left_in_transaction = store.connection.in_transaction

        # A later successful write must not commit any part of the failed append.
        await store.add_message(
            Message(
                id=0,
                session_id="success",
                role="user",
                content="next write",
                sender_id="next-sender",
            )
        )
        messages = await store.get_messages("failed")
        assert [message.content for message in messages] == (
            ["retained"] if existing_session else []
        )
        session = await store.get_session("failed")
        if existing_session:
            assert session.message_count == 1
            assert session.participants == ["original"]
        else:
            assert session is None
        assert not left_in_transaction
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_timestamp_ties_preserve_latest_selection_and_page_boundaries(tmp_path):
    store = ConversationStore(str(tmp_path / "timestamp-ties.db"))
    await store.initialize()
    try:
        ids = []
        for index in range(6):
            ids.append(await store.add_message(Message(
                id=0,
                session_id="ties",
                role="user",
                content=f"same-timestamp-{index}",
                sender_id="sender",
                timestamp=1700000000.0,
            )))

        # Latest LIMIT is applied before reversing into conversation order.
        latest = await store.get_messages("ties", limit=3)
        by_sender = await store.get_messages("ties", limit=3, sender_id="sender")
        assert [message.id for message in latest] == ids[-3:]
        assert [message.id for message in by_sender] == ids[-3:]
        search = await store.search_messages("ties", "same-timestamp", limit=3)
        assert [message.id for message in search] == list(reversed(ids[-3:]))

        first = await store.get_messages_range("ties", offset=0, limit=3)
        second = await store.get_messages_range("ties", offset=3, limit=3)
        assert [message.id for message in first + second] == ids
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_conversation_store_crud(tmp_path: Path):
    db_path = tmp_path / "conversations.db"
    store = ConversationStore(str(db_path))
    await store.initialize()

    msg = Message(
        id=0,
        session_id="s1",
        role="user",
        content="hello",
        sender_id="u1",
        sender_name="tester",
        group_id=None,
        platform="test",
        metadata={},
    )
    mid = await store.add_message(msg)
    assert mid > 0

    session = await store.get_session("s1")
    assert session is not None
    assert session.message_count == 1

    msgs = await store.get_messages("s1", limit=10)
    assert len(msgs) == 1
    assert msgs[0].content == "hello"

    count = await store.get_message_count("s1")
    assert count == 1

    await store.update_message_metadata(mid, {"flag": True})
    msgs2 = await store.get_messages("s1", limit=10)
    assert msgs2[0].metadata["flag"] is True

    search = await store.search_messages("s1", "hell", limit=5)
    assert len(search) == 1

    deleted = await store.delete_session_messages("s1")
    assert deleted == 1
    assert await store.get_message_count("s1") == 0

    await store.close()


@pytest.mark.asyncio
async def test_add_message_normalizes_multimodal_content(tmp_path: Path):
    db_path = tmp_path / "conversations_multimodal.db"
    store = ConversationStore(str(db_path))
    await store.initialize()

    msg = Message(
        id=0,
        session_id="s1",
        role="user",
        content=[
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            {"type": "text", "text": "图片里的日程是下午三点"},
        ],
        sender_id="u1",
        sender_name="tester",
        group_id=None,
        platform="test",
        metadata={},
    )

    await store.add_message(msg)
    messages = await store.get_messages("s1", limit=10)

    assert messages[0].content == "图片里的日程是下午三点"
    assert "image_url" not in messages[0].content

    await store.close()


@pytest.mark.asyncio
async def test_trim_session_messages_respects_last_summarized_index(tmp_path: Path):
    db_path = tmp_path / "trim-safe.db"
    store = ConversationStore(str(db_path))
    await store.initialize()

    for index in range(5):
        msg = Message(
            id=0,
            session_id="s-trim",
            role="user",
            content=f"content-{index}",
            sender_id="u1",
            sender_name="tester",
            group_id=None,
            platform="test",
            metadata={},
        )
        await store.add_message(msg)

    await store.connection.execute(
        "UPDATE sessions SET metadata = ? WHERE session_id = ?",
        (json.dumps({"last_summarized_index": 2}), "s-trim"),
    )
    await store.connection.commit()

    deleted = await store.trim_session_messages("s-trim", 4)

    assert deleted == 2
    assert await store.get_message_count("s-trim") == 3

    remaining = await store.get_messages_range("s-trim", offset=0, limit=10)
    assert [message.content for message in remaining] == [
        "content-2",
        "content-3",
        "content-4",
    ]

    session = await store.get_session("s-trim")
    assert session is not None
    assert session.metadata["last_summarized_index"] == 0

    await store.close()


@pytest.mark.asyncio
async def test_trim_session_messages_skips_when_no_summary_marker(tmp_path: Path):
    db_path = tmp_path / "trim-no-marker.db"
    store = ConversationStore(str(db_path))
    await store.initialize()

    for index in range(3):
        msg = Message(
            id=0,
            session_id="s-no-marker",
            role="user",
            content=f"content-{index}",
            sender_id="u1",
            sender_name="tester",
            group_id=None,
            platform="test",
            metadata={},
        )
        await store.add_message(msg)

    deleted = await store.trim_session_messages("s-no-marker", 2)

    assert deleted == 0
    assert await store.get_message_count("s-no-marker") == 3
    remaining = await store.get_messages_range("s-no-marker", offset=0, limit=10)
    assert [message.content for message in remaining] == [
        "content-0",
        "content-1",
        "content-2",
    ]

    await store.close()


@pytest.mark.asyncio
async def test_conversation_store_ranges_and_stats(tmp_path: Path):
    db_path = tmp_path / "ranges.db"
    store = ConversationStore(str(db_path))
    await store.initialize()

    for i in range(5):
        msg = Message(
            id=0,
            session_id="s2",
            role="user" if i % 2 == 0 else "assistant",
            content=f"content-{i}",
            sender_id=f"u{i % 2}",
            sender_name=f"name-{i % 2}",
            group_id=None,
            platform="test",
            metadata={},
        )
        await store.add_message(msg)

    rng = await store.get_messages_range("s2", offset=1, limit=3)
    assert [m.content for m in rng] == ["content-1", "content-2", "content-3"]

    stats = await store.get_user_message_stats("s2")
    assert isinstance(stats, dict)
    assert sum(stats.values()) >= 1

    fixed = await store.sync_message_counts()
    assert isinstance(fixed, dict)

    await store.close()


@pytest.mark.asyncio
async def test_create_session_is_idempotent(tmp_path: Path):
    """重复创建同一会话应返回同一记录，不抛异常也不产生重复行。"""
    db_path = tmp_path / "create_session.db"
    store = ConversationStore(str(db_path))
    await store.initialize()

    first = await store.create_session("sess-dup", "test")
    second = await store.create_session("sess-dup", "test")

    assert first.session_id == "sess-dup"
    assert second.session_id == "sess-dup"
    assert first.id == second.id

    cursor = await store.connection.execute(
        "SELECT COUNT(*) FROM sessions WHERE session_id = ?", ("sess-dup",)
    )
    row = await cursor.fetchone()

    assert row[0] == 1

    await store.close()
