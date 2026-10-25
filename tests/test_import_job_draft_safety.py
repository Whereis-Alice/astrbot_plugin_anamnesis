"""Retry the exact persisted import draft after progress write failures."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

pytest_plugins = ["tests.test_memory_administration"]


def payload():
    return {
        "format": "json",
        "content": json.dumps(
            [
                {
                    "session_id": "qq:GroupMessage:1",
                    "persona_id": "p",
                    "source_messages": [
                        {"role": "user", "content": "喜欢咖啡"},
                        {"role": "assistant", "content": "记住了"},
                    ],
                }
            ]
        ),
    }


def processor(service):
    service.processor.process_conversation = AsyncMock(
        side_effect=[
            ("第一次总结", {"key_facts": ["喜欢咖啡"]}, 0.5),
            ("第二次措辞不同的总结", {"key_facts": ["喜欢咖啡"]}, 0.5),
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_progress_failure_retries_the_same_draft_without_second_llm(
    administration, monkeypatch, cancel
):
    processor(administration)
    put_item = administration.store.put_item
    written = asyncio.Event()

    async def fail_progress(*args, **kwargs):
        if kwargs.get("status") == "completed":
            written.set()
            if cancel:
                await asyncio.Event().wait()
            raise OSError("progress unavailable")
        return await put_item(*args, **kwargs)

    monkeypatch.setattr(administration.store, "put_item", fail_progress)
    job = await administration.create_job("import", payload())
    worker = administration._jobs[job["operation_id"]]
    if cancel:
        await asyncio.wait_for(written.wait(), 5)
        worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    monkeypatch.setattr(administration.store, "put_item", put_item)
    await administration.job_action(job["operation_id"], "retry")
    await administration._jobs[job["operation_id"]]

    records = await administration.engine.get_memory_transfer_records()
    assert len(records) == 1 and records[0]["content"] == "第一次总结"
    assert administration.processor.process_conversation.await_count == 1
    operation = await administration.store.get(job["operation_id"])
    assert operation["status"] == "completed"


@pytest.mark.asyncio
async def test_draft_storage_failure_cannot_write_an_unrecoverable_import(
    administration, monkeypatch
):
    processor(administration)
    put_item = administration.store.put_item

    async def fail_draft(*args, **kwargs):
        if kwargs.get("status") == "prepared":
            raise OSError("draft storage unavailable")
        return await put_item(*args, **kwargs)

    monkeypatch.setattr(administration.store, "put_item", fail_draft)
    job = await administration.create_job("import", payload())
    await administration._jobs[job["operation_id"]]
    assert (await administration.store.get(job["operation_id"]))["status"] == "failed"
    assert await administration.engine.get_memory_transfer_records() == []
