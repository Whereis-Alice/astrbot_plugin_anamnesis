"""Every durable administration content path uses the same UTF-8 quota."""

import asyncio
import json

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.storage.administration_store import AdministrationStore


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


@pytest_asyncio.fixture
async def store(tmp_path):
    result = AdministrationStore(str(tmp_path / "admin.db"))
    await result.initialize()
    return result


@pytest.mark.asyncio
async def test_operation_payload_and_result_share_utf8_quota_with_items(store):
    payload = {"text": "中文😀" * 8}
    budget = size(payload) + size({}) + size({"chunk": "甲"}) + size({})
    store.max_bytes = budget
    await store.create("one", "import", "admin", payload)
    await store.put_item("one", "0", {"chunk": "甲"})
    assert (await store.storage_usage())["used_bytes"] == budget
    with pytest.raises(ValueError, match="容量"):
        await store.update("one", result={"extra": "乙"})
    with pytest.raises(ValueError, match="容量"):
        await store.create("two", "correction", "admin", {})
    assert await store.get("two") is None
    assert (await store.get("one"))["result"] == {}
    assert (await store.storage_usage())["used_bytes"] == budget


@pytest.mark.asyncio
async def test_operation_payload_replacement_reclaims_old_bytes(store):
    await store.create("one", "correction", "admin", {"text": "甲" * 50})
    before = await store.storage_usage()
    store.max_bytes = before["used_bytes"]
    await store.update("one", payload={"text": "短"})
    after = await store.storage_usage()
    assert after["used_bytes"] == size({"text": "短"}) + size({})
    await store.update("one", result={"changed": True})
    assert (await store.storage_usage())["used_bytes"] < before["used_bytes"]


@pytest.mark.asyncio
async def test_item_replacement_counts_delta_and_quota_failure_rolls_back(store):
    await store.create("one", "import", "admin", {})
    await store.put_item("one", "0", {"text": "中" * 50})
    store.max_bytes = (await store.storage_usage())["used_bytes"]
    with pytest.raises(ValueError, match="容量"):
        await store.put_item("one", "0", {"text": "中" * 51}, result={"new": True})
    assert (await store.items("one"))[0]["payload"] == {"text": "中" * 50}
    await store.put_item("one", "0", {})
    assert (await store.storage_usage())["used_bytes"] == 4 * size({})


@pytest.mark.asyncio
async def test_generated_episode_insert_update_delete_are_counted_even_via_direct_sql(store):
    await store.create("one", "episode", "admin", {})
    initial = (await store.storage_usage())["used_bytes"]
    title, summary, members = "标题", "事件内容😀", "[]"
    episode_bytes = len((title + summary + members).encode("utf-8"))
    store.max_bytes = initial + episode_bytes
    async with store.connection(write=True) as db:
        await db.execute("INSERT INTO memory_episodes(id,title,summary,members,created_at) VALUES(?,?,?,?,0)", ("event", title, summary, members))
    assert (await store.storage_usage())["used_bytes"] == initial + episode_bytes
    with pytest.raises(ValueError, match="容量"):
        async with store.connection(write=True) as db:
            await db.execute("UPDATE memory_episodes SET summary=summary||'更长' WHERE id='event'")
    async with store.connection(write=True) as db:
        await db.execute("DELETE FROM memory_episodes WHERE id='event'")
    assert (await store.storage_usage())["used_bytes"] == initial


@pytest.mark.asyncio
async def test_direct_operation_update_cannot_bypass_combined_budget(store):
    await store.create("one", "correction", "admin", {})
    store.max_bytes = (await store.storage_usage())["used_bytes"]
    with pytest.raises(ValueError, match="容量"):
        async with store.connection(write=True) as db:
            await db.execute("UPDATE memory_admin_operations SET result=? WHERE id='one'", (json.dumps({"text": "large"}),))
    assert (await store.get("one"))["result"] == {}


@pytest.mark.asyncio
async def test_snapshot_limit_checks_original_source_not_only_short_summary(store):
    store.max_snapshot_bytes = 100
    record = {"text": "短摘要", "source_messages": [{"content": "原文" * 100}]}
    with pytest.raises(ValueError, match="快照"):
        await store.create("one", "correction", "admin", {"before": record})
    assert await store.get("one") is None
    await store.create("two", "delete", "admin", {})
    with pytest.raises(ValueError, match="快照"):
        await store.put_item("two", "0", {"after": record})


@pytest.mark.asyncio
async def test_per_record_cap_applies_to_payload_result_combination(store):
    store.max_record_bytes = 60
    await store.create("one", "correction", "admin", {"text": "a" * 20})
    with pytest.raises(ValueError, match="容量"):
        await store.update("one", result={"text": "b" * 20})
    assert (await store.get("one"))["result"] == {}


@pytest.mark.asyncio
async def test_quota_lowered_below_existing_data_allows_status_and_cleanup(store):
    await store.create("one", "import", "admin", {"text": "甲" * 100}, status="preparing")
    store.max_bytes = 10
    await store.update("one", status="interrupted")
    assert (await store.get("one"))["status"] == "interrupted"
    with pytest.raises(ValueError):
        await store.put_item("one", "0", {"text": "more"})
    await store.update("one", payload={})
    await store.remove("one")
    assert (await store.storage_usage())["used_bytes"] == 0


@pytest.mark.asyncio
async def test_failed_transaction_restores_content_and_byte_counter(store):
    await store.create("one", "import", "admin", {})
    before = await store.storage_usage()
    with pytest.raises(RuntimeError):
        async with store.connection(write=True) as db:
            await db.execute("UPDATE memory_admin_operations SET payload=? WHERE id='one'", (json.dumps({"text": "甲"}),))
            raise RuntimeError("cancel before commit")
    assert (await store.get("one"))["payload"] == {}
    assert await store.storage_usage() == before


@pytest.mark.asyncio
async def test_concurrent_writers_cannot_both_spend_same_free_bytes(store):
    payload = {"text": "中" * 10}
    store.max_bytes = size(payload) + size({})
    results = await asyncio.gather(store.create("one", "import", "admin", payload), store.create("two", "import", "admin", payload), return_exceptions=True)
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert len(await store.list()) == 1
    assert (await store.storage_usage())["used_bytes"] == store.max_bytes


@pytest.mark.asyncio
async def test_reinitialize_recounts_existing_data_and_preparing_is_interrupted(store):
    await store.create("one", "import", "admin", {"text": "中文"}, status="preparing")
    await store.put_item("one", "0", {"text": "数据"})
    before = await store.storage_usage()
    fresh = AdministrationStore(store.db_path)
    await fresh.initialize()
    assert (await fresh.storage_usage())["used_bytes"] == before["used_bytes"]
    await fresh.interrupt_jobs()
    assert (await fresh.get("one"))["status"] == "interrupted"
