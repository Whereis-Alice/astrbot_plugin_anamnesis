"""Public scope moves must preserve which atom evidence a parent authorizes."""

import time

import pytest
from astrbot_plugin_anamnesis.core.managers.scope_migrator import ScopeMigrator
from astrbot_plugin_anamnesis.core.models.graph_models import GraphEntry
from astrbot_plugin_anamnesis.core.models.memory_atom import MemoryAtom

from tests.test_memory_transfer_runtime_safety import _engine
from tests.test_scope_migration_runtime_safety import _graph_setup

SOURCE = "qq:GroupMessage:1"
TARGET = "qq:GroupMessage:2"
OTHER = "qq:GroupMessage:3"


async def create_source(engine, *, persona="p", foreign=None):
    atoms = [
        MemoryAtom(
            parent_memory_id=0, content="currentfact", expires_at=time.time() + 86400
        )
    ]
    if foreign:
        atoms.append(
            MemoryAtom(
                parent_memory_id=0,
                content="foreignsecret",
                expires_at=time.time() + 86400,
                session_id=TARGET if foreign == "scope" else SOURCE,
                persona_id="other" if foreign == "persona" else persona,
            )
        )
    return await engine.add_memory(
        "currentfact and historical details",
        session_id=SOURCE,
        persona_id=persona,
        atoms=atoms,
        preserve_atom_lifecycle=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("foreign", ["scope", "persona"])
@pytest.mark.parametrize("operation", ["apply", "backup"])
async def test_foreign_atom_provenance_refuses_migration_before_backup(
    tmp_path, archived, foreign, operation
):
    engine = await _engine(tmp_path / "foreign-evidence.db")
    await engine.atom_lifecycle_manager.stop()
    try:
        memory_id = await create_source(engine, foreign=foreign)
        source = await engine.get_memory(memory_id)
        assert await engine.resolve_live_facts(
            memory_id, source["text"], source["metadata"]
        ) == ["currentfact"]
        if archived:
            assert await engine.archive_memories([memory_id]) == 1
        original = await engine._replacement_snapshot(memory_id)
        original_atoms = await engine.snapshot_archived_atom_lifecycle(memory_id)
        migrator = ScopeMigrator(engine, tmp_path / "backups")
        preview = await migrator.preview(SOURCE, TARGET)
        with pytest.raises(ValueError, match="different parent scope"):
            await getattr(migrator, operation)(SOURCE, TARGET, preview["fingerprint"])
        assert not migrator.backup_dir.exists()
        assert await engine._replacement_snapshot(memory_id) == original
        assert (
            await engine.snapshot_archived_atom_lifecycle(memory_id) == original_atoms
        )
        assert (await migrator.preview(SOURCE, TARGET))["fingerprint"] == preview[
            "fingerprint"
        ]
    finally:
        await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", ["entry", "vector"])
@pytest.mark.parametrize("foreign", ["scope", "persona"])
async def test_foreign_graph_provenance_refuses_migration_without_mutation(
    tmp_path, artifact, foreign
):
    migrator, manager, store, metadata = await _graph_setup(tmp_path)
    scoped = {
        **metadata,
        "source_memory_id": 1,
        "session_id": "target" if foreign == "scope" else "source",
        "persona_id": "other" if foreign == "persona" else "p",
    }
    try:
        if artifact == "vector":
            await manager.graph_vector_retriever.add_entry("foreignsecret", scoped)
        else:
            await store.add_entries(
                [
                    GraphEntry(
                        entry_key="foreign-entry",
                        source_memory_id=1,
                        session_id=scoped["session_id"],
                        persona_id=scoped["persona_id"],
                        entry_type="fact",
                        content="foreignsecret",
                        metadata=scoped,
                    )
                ],
                {},
                {},
            )
        plan = await migrator.preview("source", "target")
        with pytest.raises(ValueError, match="different parent scope"):
            await migrator.apply("source", "target", plan["fingerprint"])
        assert not migrator.backup_dir.exists()
        assert (await migrator.preview("source", "target"))["fingerprint"] == plan[
            "fingerprint"
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", [None, "[%None]", "p"])
@pytest.mark.parametrize("archived", [False, True])
async def test_legacy_omitted_provenance_and_matching_persona_still_migrate(
    tmp_path, persona, archived
):
    engine = await _engine(tmp_path / "legacy-evidence.db")
    await engine.atom_lifecycle_manager.stop()
    try:
        memory_id = await create_source(engine, persona=persona)
        await engine.atom_store.insert(
            MemoryAtom(
                parent_memory_id=memory_id,
                content="legacyfact",
                expires_at=time.time() + 86400,
            ),
            preserve_lifecycle=True,
        )
        if archived:
            assert await engine.archive_memories([memory_id]) == 1
        migrator = ScopeMigrator(engine, tmp_path / "backups")
        preview = await migrator.preview(SOURCE, TARGET)
        assert (await migrator.apply(SOURCE, TARGET, preview["fingerprint"]))["applied"]
        if archived:
            assert await engine.restore_memory(memory_id)
        moved = await engine.get_memory(memory_id)
        assert moved["metadata"]["session_id"] == TARGET
        assert await engine.resolve_live_facts(
            memory_id, moved["text"], moved["metadata"]
        ) == ["currentfact", "legacyfact"]
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_foreign_children_of_an_unselected_source_do_not_block_migration(
    tmp_path,
):
    engine = await _engine(tmp_path / "unrelated-evidence.db")
    await engine.atom_lifecycle_manager.stop()
    try:
        selected_id = await create_source(engine)
        other_id = await engine.add_memory(
            "unrelated",
            session_id=OTHER,
            persona_id="p",
            atoms=[
                MemoryAtom(
                    parent_memory_id=0,
                    content="unrelatedfact",
                    session_id=TARGET,
                    expires_at=time.time() + 86400,
                )
            ],
            preserve_atom_lifecycle=True,
        )
        migrator = ScopeMigrator(engine, tmp_path / "backups")
        preview = await migrator.preview(SOURCE, TARGET)
        assert preview["document_count"] == 1
        await migrator.apply(SOURCE, TARGET, preview["fingerprint"])
        assert (await engine.get_memory(selected_id))["metadata"][
            "session_id"
        ] == TARGET
        assert (await engine.get_memory(other_id))["metadata"]["session_id"] == OTHER
    finally:
        await engine.close()
