"""Import decisions must observe mutations made after their initial preview."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.memory_transfer import ImportEntry, memory_import_key
from astrbot_plugin_anamnesis.core.page_api_modules.memory_handler import MemoryHandler
from astrbot_plugin_anamnesis.core.page_api_modules.utils import PageApiUtils

from tests.test_memory_administration import add

pytest_plugins = ["tests.test_memory_administration"]
SCOPE = "qq:GroupMessage:1"
CONTENT = "喜欢咖啡"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["delete", "scope", "persona", "add"])
async def test_import_job_revalidates_duplicates_after_initial_snapshot(
    administration, monkeypatch, mutation
):
    engine = administration.engine
    old_id = None if mutation == "add" else await add(administration)
    reached, release = asyncio.Event(), asyncio.Event()
    check = administration._check_import_manifest

    async def paused_manifest(job):
        reached.set()
        await asyncio.wait_for(release.wait(), 2)
        return await check(job)

    monkeypatch.setattr(administration, "_check_import_manifest", paused_manifest)
    job = await administration.create_job(
        "import",
        {"format": "txt", "content": CONTENT, "session_id": SCOPE, "persona_id": "p"},
    )
    pending = administration._jobs[job["operation_id"]]
    try:
        await asyncio.wait_for(reached.wait(), 2)
        if mutation == "delete":
            assert await engine.delete_memory(old_id)
        elif mutation in ("scope", "persona"):
            field = "session_id" if mutation == "scope" else "persona_id"
            assert await engine.update_memory(
                old_id, {"metadata": {field: "elsewhere"}}
            )
        else:
            await add(administration)
        release.set()
        await asyncio.wait_for(asyncio.shield(pending), 2)
        result = (await administration.store.get(job["operation_id"]))["result"]
        records = await engine.get_memory_transfer_records()
        matches = [
            row
            for row in records
            if row["content"] == CONTENT
            and row["session_id"] == SCOPE
            and row["persona_id"] == "p"
        ]
        assert len(matches) == 1
        assert result["imported_count"] == (0 if mutation == "add" else 1)
        assert result["skipped_duplicate_count"] == (1 if mutation == "add" else 0)
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["delete", "add"])
async def test_direct_import_checks_after_summary_finishes(administration, mutation):
    engine = administration.engine
    old_id = await add(administration) if mutation == "delete" else None

    async def summarize(**kwargs):
        if old_id is not None:
            assert await engine.delete_memory(old_id)
        else:
            await add(administration)
        return CONTENT, {}, 0.5

    entry = ImportEntry(
        source_index=0,
        session_id=SCOPE,
        persona_id="p",
        source_messages=[
            {"role": "user", "content": "咖啡", "sender_id": "u", "session_id": SCOPE},
            {
                "role": "assistant",
                "content": "记住了",
                "sender_id": "bot",
                "session_id": SCOPE,
            },
        ],
    )
    processor = SimpleNamespace(process_conversation=AsyncMock(side_effect=summarize))
    handler = MemoryHandler(PageApiUtils())
    result = (await handler._import_entries(engine, processor, [entry], [], "skip"))[
        "data"
    ]
    assert result["failed_count"] == 0
    assert result["imported_count"] == (1 if mutation == "delete" else 0)
    assert result["skipped_duplicate_count"] == (0 if mutation == "delete" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy,expected", [("skip", 0), ("allow", 2)])
async def test_whitespace_duplicate_and_allow_strategy(
    administration, strategy, expected
):
    await add(administration, "喜欢  咖啡\t和\n茶")
    entry = ImportEntry(0, "喜欢 咖啡 和 茶", session_id=SCOPE, persona_id="p")
    handler = MemoryHandler(PageApiUtils())
    data = (
        await handler._import_entries(
            administration.engine, None, [entry, entry], [], strategy
        )
    )["data"]
    assert data["imported_count"] == expected
    assert data["skipped_duplicate_count"] == 2 - expected
    assert data["failed_count"] == 0
    assert not await administration.engine.has_memory_import_key(
        (entry.content, "elsewhere", "p")
    )
    assert not await administration.engine.has_memory_import_key(
        (entry.content, SCOPE, "other")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,persona", [(None, ""), ("", None), (0, False), (1, 2), (1.5, 2.5)]
)
async def test_current_key_matches_legacy_scope_normalization(
    administration, scope, persona
):
    engine = administration.engine
    await engine.add_memory(CONTENT, session_id=scope, persona_id=persona)
    key = memory_import_key(CONTENT, scope, persona)
    assert key in await engine.get_memory_import_keys()
    assert await engine.has_memory_import_key(key)


@pytest.mark.asyncio
async def test_current_key_handles_legacy_malformed_metadata(administration):
    engine = administration.engine
    memory_id = await add(administration)
    db = engine.db_connection
    # Old stores may lack modern JSON expression indexes and contain bad JSON.
    rows = await (
        await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='documents' AND sql LIKE '%json_extract%'"
        )
    ).fetchall()
    for row in rows:
        await db.execute(f'DROP INDEX "{row[0]}"')
    await db.execute("UPDATE documents SET metadata='invalid' WHERE id=?", (memory_id,))
    await db.commit()
    assert await engine.has_memory_import_key((CONTENT, "", ""))
    assert not await engine.has_memory_import_key((CONTENT, SCOPE, "p"))
