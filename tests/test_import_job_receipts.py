"""Retries recover only their own committed memory IDs and statistics."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from astrbot_plugin_anamnesis.core.managers.administration_jobs import (
    AdministrationJobsMixin,
)
from astrbot_plugin_anamnesis.core.memory_transfer import (
    ImportEntry,
    parse_transfer_content,
)
from astrbot_plugin_anamnesis.core.page_api_modules.memory_handler import MemoryHandler
from astrbot_plugin_anamnesis.core.page_api_modules.utils import PageApiUtils

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]

SCOPE = "qq:GroupMessage:1"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_retry_restores_own_committed_memory_receipt(
    administration, monkeypatch, cancel
):
    put_item = administration.store.put_item
    committed = asyncio.Event()

    async def fail_progress(*args, **kwargs):
        if kwargs.get("status") == "completed":
            committed.set()
            if cancel:
                await asyncio.Event().wait()
            raise OSError("progress unavailable after document commit")
        return await put_item(*args, **kwargs)

    monkeypatch.setattr(administration.store, "put_item", fail_progress)
    job = await administration.create_job(
        "import", {"format": "txt", "content": "我喜欢咖啡", "session_id": SCOPE}
    )
    worker = administration._jobs[job["operation_id"]]
    if cancel:
        await asyncio.wait_for(committed.wait(), 2)
        worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    record = (await administration.engine.get_memory_transfer_records())[0]
    original_id = record["original_id"]
    monkeypatch.setattr(administration.store, "put_item", put_item)
    await administration.job_action(job["operation_id"], "retry")
    await administration._jobs[job["operation_id"]]
    operation = await administration.store.get(job["operation_id"])
    item = (await administration.store.items(job["operation_id"]))[0]
    assert operation["status"] == "completed"
    assert operation["result"]["imported_count"] == 1
    assert operation["result"]["skipped_duplicate_count"] == 0
    assert item["result"]["imported_ids"] == [original_id]
    assert len(await administration.engine.get_memory_transfer_records()) == 1


@pytest.mark.asyncio
async def test_preexisting_duplicate_does_not_become_this_jobs_receipt(administration):
    await add(administration, "我喜欢咖啡")
    job = await administration.create_job(
        "import",
        {
            "format": "txt",
            "content": "我喜欢咖啡",
            "session_id": SCOPE,
            "persona_id": "p",
        },
    )
    await administration._jobs[job["operation_id"]]
    result = (await administration.store.get(job["operation_id"]))["result"]
    assert result["imported_count"] == 0 and result["skipped_duplicate_count"] == 1
    assert (await administration.store.items(job["operation_id"]))[0]["result"][
        "imported_ids"
    ] == []


@pytest.mark.asyncio
async def test_external_receipt_markers_are_provenance_and_cannot_claim_a_job(
    administration, monkeypatch
):
    launch = administration._launch_job
    monkeypatch.setattr(administration, "_launch_job", lambda operation_id: None)
    job = await administration.create_job(
        "import", {"format": "txt", "content": "外来知识", "session_id": SCOPE}
    )
    markers = {"import_operation_id": job["operation_id"], "import_item_key": "000000"}
    entries, errors = parse_transfer_content(
        json.dumps([{"content": "外来知识", "session_id": SCOPE, "metadata": markers}])
    )
    handler = MemoryHandler(PageApiUtils())
    response = await handler._import_entries(
        administration.engine, administration.processor, entries, errors, "skip"
    )
    assert response["data"]["imported_count"] == 1
    metadata = (await administration.engine.get_memory_transfer_records())[0][
        "metadata"
    ]
    assert "import_operation_id" not in metadata and "import_item_key" not in metadata
    assert all(
        metadata["import_provenance"][key] == value for key, value in markers.items()
    )
    monkeypatch.setattr(administration, "_launch_job", launch)
    launch(job["operation_id"])
    await administration._jobs[job["operation_id"]]
    result = (await administration.store.get(job["operation_id"]))["result"]
    assert result["imported_count"] == 0 and result["skipped_duplicate_count"] == 1


@pytest.mark.asyncio
async def test_own_receipt_does_not_convert_a_second_duplicate_item_to_imported(
    administration, monkeypatch
):
    put_item = administration.store.put_item
    fail_once = True

    async def fail_first_progress(*args, **kwargs):
        nonlocal fail_once
        if kwargs.get("status") == "completed" and fail_once:
            fail_once = False
            raise OSError("progress unavailable")
        return await put_item(*args, **kwargs)

    monkeypatch.setattr(administration.store, "put_item", fail_first_progress)
    entry = {"content": "知识", "session_id": SCOPE}
    job = await administration.create_job(
        "import", {"format": "json", "content": json.dumps([entry, entry])}
    )
    await administration._jobs[job["operation_id"]]
    await administration.job_action(job["operation_id"], "retry")
    await administration._jobs[job["operation_id"]]
    result = (await administration.store.get(job["operation_id"]))["result"]
    assert result["imported_count"] == 1 and result["skipped_duplicate_count"] == 1
    items = await administration.store.items(job["operation_id"])
    assert items[0]["result"]["imported_ids"]
    assert items[1]["result"]["imported_ids"] == []


@pytest.mark.asyncio
async def test_unrelated_malformed_metadata_does_not_block_receipt_recovery():
    metadata = {
        "session_id": SCOPE,
        "persona_id": None,
        "status": "active",
        "memory_origin": "memory_import",
        "import_operation_id": "job",
        "import_item_key": "000000",
    }
    entry = ImportEntry(
        source_index=0,
        content="有效知识",
        session_id=SCOPE,
        persona_id=None,
        importance=0.5,
        metadata={},
        source_messages=[],
    )
    # Legacy schemas may have no JSON expression indexes. Exercise the reader
    # with real SQLite rows without newer write-time metadata validation.
    async with aiosqlite.connect(":memory:") as connection:
        await connection.execute(
            "CREATE TABLE documents(id INTEGER PRIMARY KEY,metadata TEXT)"
        )
        await connection.executemany(
            "INSERT INTO documents VALUES(?,?)",
            [
                (1, json.dumps(metadata)),
                (2, "{broken"),
            ],
        )
        await connection.commit()
        engine = SimpleNamespace(
            db_connection=connection,
            get_memory=AsyncMock(
                return_value={"text": "有效知识", "metadata": metadata}
            ),
        )
        result = await AdministrationJobsMixin._recover_import_receipt(
            SimpleNamespace(engine=engine), "job", "000000", entry
        )
    assert result["imported_ids"] == [1]
    assert result["imported_count"] == 1
