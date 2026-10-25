"""Only complete, restorable backups may be published or advance versions."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from astrbot_plugin_anamnesis.core.managers import backup_manager as module
from astrbot_plugin_anamnesis.core.managers.backup_manager import BackupManager


def _database(path, content="live"):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE records(content TEXT)")
        db.execute("INSERT INTO records VALUES (?)", (content,))


def _rows(path):
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        return db.execute("SELECT content FROM records").fetchall()


def _manager(tmp_path):
    _database(tmp_path / "anamnesis.db")
    (tmp_path / "anamnesis.index").write_bytes(b"live index")
    manager = BackupManager(
        str(tmp_path), {"backup_settings": {"max_keep": 0, "max_total_size_mb": 0}}
    )
    manager.version_file.write_text("1.0.0", encoding="utf-8")
    manager.prune_backups = Mock()
    return manager


def test_copy_failure_keeps_old_version_and_healthy_backup(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    healthy = manager.backups_dir / "v1.0.0"
    healthy.mkdir(parents=True)
    _database(healthy / "anamnesis.db", "healthy")
    copy = module.shutil.copy2

    def fail_index(source, destination):
        if Path(source).suffix == ".index":
            raise OSError("disk write failed")
        return copy(source, destination)

    monkeypatch.setattr(module.shutil, "copy2", fail_index)

    assert manager.backup_if_needed() is None
    assert manager.get_stored_version() == "1.0.0"
    manager.prune_backups.assert_not_called()
    assert _rows(healthy / "anamnesis.db") == [("healthy",)]
    assert list(manager.backups_dir.iterdir()) == [healthy]


def test_existing_healthy_version_backup_is_never_overwritten(tmp_path):
    manager = _manager(tmp_path)
    healthy = manager.backups_dir / "v1.0.0"
    healthy.mkdir(parents=True)
    _database(healthy / "anamnesis.db", "healthy")

    published = Path(manager.backup_if_needed())

    assert published != healthy
    assert _rows(healthy / "anamnesis.db") == [("healthy",)]
    assert _rows(published / "anamnesis.db") == [("live",)]
    assert json.loads((published / "backup_info.json").read_text())["files_copied"] == 2


@pytest.mark.parametrize(
    "label", ["../..\\outside", "2/../../outside", "C:\\outside", "..", "x:y"]
)
def test_invalid_version_label_cannot_create_a_backup_path(tmp_path, label):
    manager = _manager(tmp_path)
    manager.version_file.write_text(label, encoding="utf-8")

    assert manager.backup_if_needed() is None

    assert manager.get_stored_version() == label
    assert not manager.backups_dir.exists()
    manager.prune_backups.assert_not_called()


def test_invalid_database_is_a_failed_backup(tmp_path):
    manager = _manager(tmp_path)
    (tmp_path / "anamnesis.db").write_bytes(b"not a SQLite database")

    assert manager.backup_if_needed() is None

    assert manager.get_stored_version() == "1.0.0"
    manager.prune_backups.assert_not_called()
    assert not list(manager.backups_dir.iterdir())


def test_manifest_failure_does_not_publish_partial_data(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    write = Path.write_text

    def fail_manifest(path, *args, **kwargs):
        if path.name == "backup_info.json":
            raise OSError("manifest failed")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_manifest)

    assert manager.backup_if_needed() is None

    assert manager.get_stored_version() == "1.0.0"
    manager.prune_backups.assert_not_called()
    assert not list(manager.backups_dir.iterdir())


def test_version_persistence_failure_keeps_complete_backup_retryable(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    original_write = manager.write_current_version
    monkeypatch.setattr(
        manager, "write_current_version", Mock(side_effect=OSError("version failed"))
    )

    assert manager.backup_if_needed() is None

    assert manager.get_stored_version() == "1.0.0"
    manager.prune_backups.assert_not_called()
    completed = list(manager.backups_dir.iterdir())
    assert len(completed) == 1
    assert _rows(completed[0] / "anamnesis.db") == [("live",)]
    monkeypatch.setattr(manager, "write_current_version", original_write)
    retried = manager.backup_if_needed()
    assert retried is not None
    assert Path(retried) != completed[0]
    assert _rows(completed[0] / "anamnesis.db") == [("live",)]


def test_wal_backup_is_a_standalone_consistent_sqlite_snapshot(tmp_path):
    manager = _manager(tmp_path)
    source_path = tmp_path / "anamnesis.db"
    with sqlite3.connect(source_path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO records VALUES ('committed in WAL')")
        writer.commit()
        assert Path(str(source_path) + "-wal").stat().st_size > 0

        published = Path(manager.backup_if_needed())

        assert not list(published.glob("*-wal"))
        assert not list(published.glob("*-shm"))
        assert _rows(published / "anamnesis.db") == [("live",), ("committed in WAL",)]
