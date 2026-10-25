"""Rollback preserves missing, null and numeric fields on old archived records."""

import asyncio

import pytest

from tests.test_memory_transfer_runtime_safety import _api, _import

pytest_plugins = ["tests.test_memory_administration"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["legacy_api", "native_import"])
@pytest.mark.parametrize("archive_marker", ["missing", None, 123.125])
async def test_cancelled_restore_preserves_entire_original_archive_snapshot(
    administration, monkeypatch, entrypoint, archive_marker
):
    engine = administration.engine
    metadata = {
        "status": "archived",
        "source_session_id": "qq:GroupMessage:1",
        "key_facts": ["喜欢咖啡"],
        "custom_null": None,
    }
    if archive_marker != "missing":
        metadata["archived_at"] = archive_marker
    if entrypoint == "legacy_api":
        memory_id = await engine.add_memory(
            "喜欢咖啡",
            session_id="qq:GroupMessage:1",
            persona_id="p",
            metadata=metadata,
            source_messages=[{"role": "user", "content": "我喜欢咖啡"}],
        )
    else:
        response = await _import(
            _api(engine),
            [
                {
                    "content": "喜欢咖啡",
                    "session_id": "qq:GroupMessage:1",
                    "persona_id": "p",
                    "metadata": metadata,
                    "atoms": [],
                    "source_messages": [{"role": "user", "content": "我喜欢咖啡"}],
                }
            ],
        )
        assert response["data"]["failed_count"] == 0
        memory_id = response["data"]["imported_ids"][0]
    before = await administration.snapshot(memory_id)
    if entrypoint == "legacy_api":
        assert ("archived_at" in before["metadata"]) == (archive_marker != "missing")
    committed, release = asyncio.Event(), asyncio.Event()
    commit = engine.db_connection.commit

    async def observed_commit():
        await commit()
        row = await (
            await engine.db_connection.execute(
                "SELECT json_extract(metadata,'$.status') FROM documents WHERE id=?",
                (memory_id,),
            )
        ).fetchone()
        if row[0] == "active" and not committed.is_set():
            committed.set()
            await release.wait()

    monkeypatch.setattr(engine.db_connection, "commit", observed_commit)
    restoration = asyncio.create_task(engine.restore_memory(memory_id))
    try:
        await asyncio.wait_for(committed.wait(), 5)
        restoration.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(restoration, 5)
        assert await administration.snapshot(memory_id) == before
        monkeypatch.setattr(engine.db_connection, "commit", commit)
        assert await engine.restore_memory(memory_id)
        current = await administration.snapshot(memory_id)
        assert current["metadata"]["status"] == "active"
        assert "archived_at" not in current["metadata"]
        assert current["source_messages"] == before["source_messages"]
    finally:
        release.set()
        if not restoration.done():
            restoration.cancel()
        await asyncio.gather(restoration, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_before_restore_commit_keeps_absent_archive_fields(
    administration, monkeypatch
):
    engine = administration.engine
    memory_id = await engine.add_memory(
        "旧归档",
        session_id="qq:GroupMessage:1",
        persona_id="p",
        metadata={"status": "archived"},
    )
    before = await administration.snapshot(memory_id)
    embedding_started = asyncio.Event()

    async def waiting_embedding(_content):
        embedding_started.set()
        await asyncio.Future()

    monkeypatch.setattr(
        engine.faiss_db.embedding_provider, "get_embedding", waiting_embedding
    )
    restoration = asyncio.create_task(engine.restore_memory(memory_id))
    try:
        await asyncio.wait_for(embedding_started.wait(), 5)
        restoration.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(restoration, 5)
        assert await administration.snapshot(memory_id) == before
    finally:
        if not restoration.done():
            restoration.cancel()
        await asyncio.gather(restoration, return_exceptions=True)
