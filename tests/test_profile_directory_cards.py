"""Contact summaries stay truthful and use batch queries rather than per-user reads."""

import time

import pytest

from tests.test_user_profile_ledger import event, fact
from tests.test_user_profile_ledger import manager as manager  # noqa: PLC0414


@pytest.mark.asyncio
async def test_contact_preview_excludes_proposals_expired_and_suppressed_facts(manager):
    owner = event()
    await manager.upsert_facts(
        owner, [fact(key="drink"), fact("reader", key="books")], source_memory_id=10
    )
    await manager.upsert_facts(owner, [fact("coffee")], source_memory_id=11)
    await manager.upsert_facts(
        owner, [fact("old status", key="old", category="status")], source_memory_id=12
    )
    await manager.update_for_web(
        manager.scope_for_event(owner), "old", {"expires_at": time.time() - 1}
    )
    await manager.upsert_facts(
        owner, [fact("removed support", key="hidden")], source_memory_id=13
    )
    await manager.set_source_suppressed([13], True)
    user = (await manager.list_users_for_web())["users"][0]
    assert {row["value"] for row in user["fact_preview"]} == {"喜欢茶", "reader"}
    assert user["pending_count"] == 1 and user["active_fact_count"] == 2
    assert user["last_seen_at"] > 0 and user["last_group_name"] == "读书群"
    assert user["sender_name"] == owner.get_sender_name()


@pytest.mark.asyncio
async def test_contact_summary_deduplicates_equal_facts_without_merging_storage_scopes(
    manager,
):
    for index in range(5):
        await manager.upsert_facts(
            event(session=f"qq:GroupMessage:{index}"),
            [fact()],
            source_memory_id=10 + index,
        )
    user = (await manager.list_users_for_web())["users"][0]
    assert user["scope_count"] == 5 and user["fact_count"] == 5
    assert (
        len(user["fact_preview"]) == 1 and user["fact_preview"][0]["value"] == "喜欢茶"
    )
    assert (await manager.list_for_web(identity=user["identity"]))["total"] == 5


@pytest.mark.asyncio
async def test_contact_query_count_does_not_grow_with_each_person(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    trace = []
    await manager.store.connection.set_trace_callback(trace.append)
    try:
        await manager.list_users_for_web()
        first = len([sql for sql in trace if sql.lstrip().upper().startswith("SELECT")])
        for index in range(8):
            await manager.upsert_facts(
                event(sender=f"person{index}"), [fact()], source_memory_id=20 + index
            )
        trace.clear()
        result = await manager.list_users_for_web()
        second = len(
            [sql for sql in trace if sql.lstrip().upper().startswith("SELECT")]
        )
        assert result["total"] == 9 and second == first
        assert all(len(user["fact_preview"]) == 1 for user in result["users"])
    finally:
        await manager.store.connection.set_trace_callback(None)
