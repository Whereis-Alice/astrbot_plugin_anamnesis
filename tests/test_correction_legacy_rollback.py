"""Rollback preserves unatomized text and the original atom lifecycle."""

import json
import time

import pytest
from astrbot_plugin_anamnesis.core.memory_transfer import parse_transfer_content
from astrbot_plugin_anamnesis.core.models.memory_atom import (
    AtomStatus,
    AtomType,
    MemoryAtom,
)
from astrbot_plugin_anamnesis.core.page_api_modules.memory_handler import MemoryHandler
from astrbot_plugin_anamnesis.core.page_api_modules.utils import PageApiUtils

pytest_plugins = ["tests.test_memory_administration"]

SCOPE = "qq:GroupMessage:1"
SOURCE = [{"role": "user", "content": "喜欢咖啡", "sender_id": "u"}]


async def correct_and_rollback(service, memory_id):
    preview = await service.preview_correction(
        memory_id, "纠正为茶", actor="webui:a", new_content="喜欢茶"
    )
    token = await service.confirm(preview["operation_id"], actor="webui:a")
    corrected = await service.execute_correction(
        preview["operation_id"], token["confirmation_token"], actor="webui:a"
    )
    current = await service.snapshot(corrected["new_memory_id"])
    assert await service.engine.resolve_live_facts(
        corrected["new_memory_id"], current["text"], current["metadata"]
    ) == ["喜欢茶"]
    restored = await service.rollback_correction(preview["operation_id"], actor="webui:a")
    return restored["memory_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["legacy_text", "legacy_facts", "portable_empty_atoms"])
async def test_unatomized_original_remains_recallable_after_rollback(administration, kind):
    engine = administration.engine
    engine.atom_enabled = True
    metadata = {"source_session_id": SCOPE}
    if kind != "legacy_text":
        metadata["key_facts"] = ["喜欢咖啡"]
    if kind == "portable_empty_atoms":
        entries, errors = parse_transfer_content(json.dumps([{
            "content": "喜欢咖啡", "session_id": SCOPE, "persona_id": "p",
            "metadata": metadata, "source_messages": SOURCE, "atoms": [],
        }]))
        assert errors == [] and entries[0].atoms == []
        result = await MemoryHandler(PageApiUtils())._import_entries(
            engine, administration.processor, entries, [], "keep"
        )
        assert result["data"]["failed_count"] == 0
        memory_id = result["data"]["imported_ids"][0]
    else:
        memory_id = await engine.add_memory(
            content="喜欢咖啡", session_id=SCOPE, persona_id="p",
            metadata=metadata, source_messages=SOURCE,
        )
    original = await administration.snapshot(memory_id)
    assert await engine.snapshot_archived_atom_lifecycle(memory_id) == []
    assert not original["metadata"].get("atom_types")
    assert await engine.resolve_live_facts(memory_id, original["text"], original["metadata"]) == ["喜欢咖啡"]
    restored_id = await correct_and_rollback(administration, memory_id)
    restored = await administration.snapshot(restored_id)
    assert restored["text"] == "喜欢咖啡"
    assert restored["source_messages"] == original["source_messages"]
    assert restored["metadata"]["create_time"] == original["metadata"]["create_time"]
    assert await engine.snapshot_archived_atom_lifecycle(restored_id) == []
    assert not restored["metadata"].get("atom_types")
    assert await engine.resolve_live_facts(restored_id, restored["text"], restored["metadata"]) == ["喜欢咖啡"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "expired", "forgotten", "superseded", "purged"])
async def test_atom_backed_original_keeps_its_lifecycle_after_rollback(administration, state):
    engine = administration.engine
    engine.atom_enabled = True
    now = time.time()
    atom = MemoryAtom(
        parent_memory_id=0, content="喜欢咖啡", atom_type=AtomType.PREFERENCE,
        status=AtomStatus.ACTIVE if state == "purged" else AtomStatus(state),
        created_at=now - 5 * 86400, last_accessed_at=now - 86400,
        last_reinforced_at=now - 2 * 86400, ttl_days=60,
        expires_at=now - 3600 if state == "expired" else now + 3600,
        reinforcement_count=3, session_id=SCOPE, persona_id="p",
    )
    memory_id = await engine.add_memory(
        content="喜欢咖啡", session_id=SCOPE, persona_id="p",
        metadata={"key_facts": ["喜欢咖啡"], "source_session_id": SCOPE},
        atoms=[atom], preserve_atom_lifecycle=True, source_messages=SOURCE,
    )
    if state == "purged":
        assert await engine.atom_store.delete_by_parent(memory_id) == 1
    original = await engine.snapshot_archived_atom_lifecycle(memory_id)
    before_rollback = time.time()
    restored_id = await correct_and_rollback(administration, memory_id)
    restored = await administration.snapshot(restored_id)
    lifecycle = await engine.snapshot_archived_atom_lifecycle(restored_id)
    assert restored["metadata"]["atom_types"] == ["preference"]
    if state == "purged":
        # A historical atom marker prevents a missing, cleaned atom from
        # regaining the legacy-text fallback, even with an empty snapshot.
        assert all(row["status"] == "forgotten" and row["expires_at"] <= before_rollback for row in lifecycle)
    else:
        assert len(lifecycle) == len(original) == 1
        fields = ("status", "created_at", "last_accessed_at", "last_reinforced_at",
                  "ttl_days", "expires_at", "reinforcement_count", "decay_type", "event_time")
        assert {field: lifecycle[0][field] for field in fields} == {
            field: original[0][field] for field in fields
        }
    assert await engine.resolve_live_facts(
        restored_id, restored["text"], restored["metadata"]
    ) == (["喜欢咖啡"] if state == "active" else None)
