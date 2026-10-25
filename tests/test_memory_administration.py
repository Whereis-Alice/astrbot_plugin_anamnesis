"""Real SQLite operation/plan/job tests using the existing vector test adapter."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.managers.administration_jobs import chunk_text
from astrbot_plugin_anamnesis.core.managers.memory_administration import (
    MemoryAdministration,
)

from tests.test_memory_transfer_runtime_safety import _engine


@pytest_asyncio.fixture
async def administration(tmp_path):
    engine = await _engine(tmp_path / "memory.db")
    engine.atom_enabled = False
    config = ConfigManager({"episodic_memory": {"enabled": True}})
    processor = SimpleNamespace(_call_llm_with_retry=AsyncMock(return_value=json.dumps({"content": "喜欢茶", "key_facts": ["喜欢茶"]})))
    service = MemoryAdministration(engine, config, processor)
    engine.memory_administration = service
    await service.initialize()
    try:
        yield service
    finally:
        await engine.close()


async def add(service, text="喜欢咖啡", scope="qq:GroupMessage:1"):
    return await service.engine.add_memory(content=text, session_id=scope, persona_id="p", metadata={
        "status": "active", "source_session_id": scope, "key_facts": [text]},
        source_messages=[{"role": "user", "content": text, "sender_id": "u"}])


@pytest.mark.asyncio
async def test_recovery_preserves_sources_and_detects_later_edits(administration):
    first, second = await add(administration), await add(administration, "喜欢书")
    original_source = await administration.engine.get_memory_source(first)
    deleted = await administration.delete([first, second], actor="webui:a", reason="误删测试")
    assert deleted["deleted_ids"] == [first, second]
    assert (await administration.engine.get_memory(first))["metadata"]["status"] == "archived"
    await administration.engine.update_memory(second, {"metadata": {"session_id": "another-scope"}})
    restored = await administration.restore(deleted["operation_id"], actor="webui:a")
    assert restored["ids"] == [first] and restored["conflicts"] == [second]
    assert await administration.engine.get_memory_source(first) == original_source
    assert (await administration.engine.get_memory(second))["metadata"]["status"] == "archived"


@pytest.mark.asyncio
async def test_tool_operation_cannot_restore_another_owner_and_purge_clears_snapshots(administration):
    memory_id = await add(administration)
    deleted = await administration.delete([memory_id], actor="tool:scope:user-a")
    with pytest.raises(ValueError):
        await administration.restore(deleted["operation_id"], actor="tool:scope:user-b")
    result = await administration.restore(deleted["operation_id"], actor="webui:admin", purge=True)
    assert result["ids"] == [memory_id] and await administration.engine.get_memory(memory_id) is None
    assert (await administration.store.items(deleted["operation_id"]))[0]["payload"] == {}


@pytest.mark.asyncio
async def test_failed_archive_reclaims_snapshot_without_deleting_active_source(administration, monkeypatch):
    memory_id = await add(administration)
    monkeypatch.setattr(administration.engine, "archive_memories", AsyncMock(side_effect=RuntimeError("archive interrupted")))
    deleted = await administration.delete([memory_id], actor="webui:a")
    assert deleted["failed"] and not deleted["deleted_ids"]
    result = await administration.restore(deleted["operation_id"], actor="webui:a", purge=True)
    assert result["ids"] == [memory_id] and not result["conflicts"]
    assert (await administration.snapshot(memory_id))["metadata"]["status"] == "active"
    assert (await administration.store.items(deleted["operation_id"]))[0]["payload"] == {}


@pytest.mark.asyncio
async def test_plan_requires_real_single_use_actor_bound_token(administration):
    memory_id = await add(administration)
    preview = await administration.preview_correction(memory_id, "改为喜欢茶", actor="webui:a")
    assert "confirmation_token" not in preview
    with pytest.raises(ValueError):
        await administration.execute_correction(preview["operation_id"], "true", actor="webui:a")
    token = await administration.confirm(preview["operation_id"], actor="webui:a")
    with pytest.raises(ValueError):
        await administration.execute_correction(preview["operation_id"], token["confirmation_token"], actor="webui:b")
    result = await administration.execute_correction(preview["operation_id"], token["confirmation_token"], actor="webui:a")
    assert await administration.engine.get_memory(memory_id) is None
    assert (await administration.engine.get_memory(result["new_memory_id"]))["text"] == "喜欢茶"
    with pytest.raises(ValueError):
        await administration.execute_correction(preview["operation_id"], token["confirmation_token"], actor="webui:a")


@pytest.mark.asyncio
async def test_plan_stale_source_is_rejected_before_changes(administration):
    memory_id = await add(administration)
    preview = await administration.preview_correction(memory_id, "改为茶", actor="webui:a", new_content="茶")
    token = await administration.confirm(preview["operation_id"], actor="webui:a")
    await administration.engine.save_memory_source(memory_id, [{"role": "user", "content": "later"}])
    with pytest.raises(ValueError, match="变化"):
        await administration.execute_correction(preview["operation_id"], token["confirmation_token"], actor="webui:a")
    assert (await administration.engine.get_memory(memory_id))["text"] == "喜欢咖啡"


@pytest.mark.asyncio
async def test_correction_rollback_preserves_original_source_and_refuses_later_edit(administration):
    memory_id = await add(administration)
    original_source = await administration.engine.get_memory_source(memory_id)
    preview = await administration.preview_correction(memory_id, "改为茶", actor="webui:a")
    token = await administration.confirm(preview["operation_id"], actor="webui:a")
    await administration.execute_correction(preview["operation_id"], token["confirmation_token"], actor="webui:a")
    restored = await administration.rollback_correction(preview["operation_id"], actor="webui:a")
    assert (await administration.engine.get_memory(restored["memory_id"]))["text"] == "喜欢咖啡"
    assert (await administration.engine.get_memory(restored["memory_id"]))["metadata"]["canonical_summary"] == "喜欢咖啡"
    assert (await administration.engine.get_memory(restored["memory_id"]))["metadata"]["persona_summary"] == "喜欢咖啡"
    assert await administration.engine.get_memory_source(restored["memory_id"]) == original_source


@pytest.mark.asyncio
async def test_rollback_does_not_grant_expired_fact_a_new_lifetime(administration):
    import time
    from astrbot_plugin_anamnesis.core.models.memory_atom import AtomType, MemoryAtom
    engine = administration.engine
    engine.atom_enabled = True
    atom = MemoryAtom(parent_memory_id=0, content="喜欢咖啡", atom_type=AtomType.PREFERENCE, expires_at=time.time()-10,
                      session_id="qq:GroupMessage:1", persona_id="p")
    memory_id = await engine.add_memory(content="喜欢咖啡", session_id="qq:GroupMessage:1", persona_id="p",
        metadata={"status": "active", "source_session_id": "qq:GroupMessage:1", "key_facts": ["喜欢咖啡"]},
        atoms=[atom], preserve_atom_lifecycle=True)
    preview = await administration.preview_correction(memory_id, "纠正为茶", actor="webui:a")
    token = await administration.confirm(preview["operation_id"], actor="webui:a")
    await administration.execute_correction(preview["operation_id"], token["confirmation_token"], actor="webui:a")
    restored = await administration.rollback_correction(preview["operation_id"], actor="webui:a")
    current = await engine.get_memory(restored["memory_id"])
    assert await engine.resolve_live_facts(restored["memory_id"], current["text"], current["metadata"]) is None


def test_foreign_operation_tokens_are_import_provenance_only():
    from astrbot_plugin_anamnesis.core.memory_transfer import isolate_import_provenance
    metadata = isolate_import_provenance({"session_id": "scope", "recovery_operation_id": "foreign-id"})
    assert "recovery_operation_id" not in metadata
    assert metadata["import_provenance"]["recovery_operation_id"] == "foreign-id"


@pytest.mark.asyncio
async def test_direct_correction_remains_recallable_when_original_has_atoms(administration):
    from astrbot_plugin_anamnesis.core.processors.atom_classifier import classify_atoms

    engine = administration.engine
    engine.atom_enabled = True
    memory_id = await engine.add_memory(content="喜欢咖啡", session_id="s", persona_id="p",
        metadata={"key_facts": ["喜欢咖啡"]}, atoms=classify_atoms(["喜欢咖啡"]))
    preview = await administration.preview_correction(memory_id, "纠正为茶", actor="webui:a", new_content="喜欢茶")
    token = await administration.confirm(preview["operation_id"], actor="webui:a")
    result = await administration.execute_correction(preview["operation_id"], token["confirmation_token"], actor="webui:a")
    current = await administration.snapshot(result["new_memory_id"])
    projection = await engine.resolve_live_facts(result["new_memory_id"], current["text"], current["metadata"])
    assert projection is not None and "喜欢茶" in str(projection) and "喜欢咖啡" not in str(projection)


@pytest.mark.asyncio
async def test_projection_intent_is_atomic_and_old_completion_does_not_clear_new(administration):
    memory_id = await add(administration)
    old = next(job for job in await administration.projection.pending() if job["memory_id"] == memory_id)
    db = administration.engine.db_connection
    await db.execute("UPDATE documents SET metadata=json_set(metadata,'$.persona_id','new') WHERE id=?", (memory_id,))
    await db.rollback()
    assert next(job for job in await administration.projection.pending() if job["memory_id"] == memory_id)["revision"] == old["revision"]
    await administration.engine.update_memory(memory_id, {"metadata": {"persona_id": "new"}})
    assert not await administration.projection.complete(memory_id, old["revision"])
    current = next(job for job in await administration.projection.pending() if job["memory_id"] == memory_id)
    await administration.engine._update_access_times_internal([memory_id])
    assert next(job for job in await administration.projection.pending() if job["memory_id"] == memory_id)["revision"] == current["revision"]


@pytest.mark.asyncio
async def test_import_job_retry_is_idempotent_and_has_chunk_progress(administration):
    job = await administration.create_job("import", {"format": "md", "content": "# 一\n咖啡\n\n# 二\n茶", "session_id": "qq:GroupMessage:1", "persona_id": "p"})
    await administration._jobs[job["operation_id"]]
    stored = await administration.store.get(job["operation_id"])
    assert stored["status"] == "completed"
    assert all(item["status"] == "completed" for item in await administration.store.items(job["operation_id"]))
    count = stored["result"]["imported_count"]
    await administration.store.update(job["operation_id"], status="interrupted")
    await administration.job_action(job["operation_id"], "retry")
    await administration._jobs[job["operation_id"]]
    assert (await administration.store.get(job["operation_id"]))["result"]["imported_count"] == count


@pytest.mark.asyncio
async def test_restart_marks_inflight_job_interrupted_without_repeating_llm(administration):
    operation_id = await administration._create("episode", "webui", {"memory_ids": [123]}, "running")
    fresh = MemoryAdministration(administration.engine, administration.config, administration.processor)
    await fresh.initialize()
    assert (await fresh.store.get(operation_id))["status"] == "interrupted"
    administration.processor._call_llm_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_source_change_redacts_generated_text_and_cross_scope_refused(administration):
    first, second = await add(administration), await add(administration, "茶")
    job = await administration.create_job("episode", {"memory_ids": [first, second], "use_llm": False})
    await administration._jobs[job["operation_id"]]
    episodes = await administration.list_episodes()
    assert len(episodes) == 1 and not episodes[0]["stale"]
    await administration.engine.archive_memories([first])
    episodes = await administration.list_episodes()
    assert episodes[0]["stale"] and episodes[0]["summary"] == ""
    assert await administration.episode_candidates("咖啡", [], session_id="qq:GroupMessage:1", persona_id="p") == []
    other = await add(administration, "其他", "qq:GroupMessage:2")
    job = await administration.create_job("episode", {"memory_ids": [second, other], "use_llm": False})
    await administration._jobs[job["operation_id"]]
    assert (await administration.store.get(job["operation_id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_episode_invalid_model_output_is_never_published(administration):
    memory_id = await add(administration)
    administration.processor._call_llm_with_retry.return_value = '{"title":"truncated"'
    job = await administration.create_job("episode", {"memory_ids": [memory_id]})
    await administration._jobs[job["operation_id"]]
    assert (await administration.store.get(job["operation_id"]))["status"] == "failed"
    assert not await administration.list_episodes()


def test_chunks_are_bounded_and_do_not_repeat_content():
    chunks = chunk_text("a"*2100 + "\n\n" + "b"*900, 1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == "a"*2100+"b"*900


@pytest.mark.asyncio
async def test_page_identity_ignores_payload_actor_and_hides_confirmation_secret(administration):
    from astrbot_plugin_anamnesis.core.page_api import PluginPageApi
    from quart import Quart, g

    from tests.test_page_api import FakePlugin
    memory_id = await add(administration)
    plugin = FakePlugin(memory_engine=administration.engine)
    plugin.config_manager = administration.config
    api = PluginPageApi(plugin)
    app = Quart(__name__)
    async with app.test_request_context("/operations/delete", method="POST", json={"memory_ids": [memory_id], "actor": "tool:other"}):
        g.username = "alice"
        response = await api.administration_request("delete")
    assert response["status"] == "ok"
    operation = await administration.store.get(response["data"]["operation_id"])
    assert operation["actor"] == "webui:alice"
    async with app.test_request_context("/operations/detail?operation_id=" + operation["id"], method="GET"):
        g.username = "alice"
        response = await api.administration_request("detail")
    assert "token_hash" not in response["data"] and "token_actor" not in response["data"]
