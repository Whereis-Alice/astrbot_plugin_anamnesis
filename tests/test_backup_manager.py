"""Tests for version-triggered backup manager."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from astrbot_plugin_anamnesis.core.managers.backup_manager import (
    _BACKUP_INFO_FILE,
    PLUGIN_VERSION,
    BackupManager,
)


def _write_db(path: Path, content: str) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE records(content TEXT)")
        db.execute("INSERT INTO records VALUES (?)", (content,))
        db.commit()


def _read_db(path: Path) -> str:
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        return db.execute("SELECT content FROM records").fetchone()[0]


def test_get_stored_version_first_run(tmp_path: Path) -> None:
    mgr = BackupManager(str(tmp_path))
    assert mgr.get_stored_version() is None


def test_write_and_read_version(tmp_path: Path) -> None:
    mgr = BackupManager(str(tmp_path))
    mgr.write_current_version()
    assert mgr.get_stored_version() == PLUGIN_VERSION


def test_needs_backup_first_run(tmp_path: Path) -> None:
    mgr = BackupManager(str(tmp_path))
    assert mgr.needs_backup() is True


def test_needs_backup_same_version(tmp_path: Path) -> None:
    mgr = BackupManager(str(tmp_path))
    mgr.write_current_version()
    assert mgr.needs_backup() is False


def test_needs_backup_different_version(tmp_path: Path) -> None:
    mgr = BackupManager(str(tmp_path))
    mgr.version_file.write_text("1.0.0", encoding="utf-8")
    assert mgr.needs_backup() is True


def test_backup_if_needed_no_change(tmp_path: Path) -> None:
    mgr = BackupManager(str(tmp_path))
    mgr.write_current_version()
    result = mgr.backup_if_needed()
    assert result is None


def test_backup_if_needed_creates_backup_dir(tmp_path: Path) -> None:
    # Create mock data files
    _write_db(tmp_path / "anamnesis.db", "database content")
    (tmp_path / "anamnesis.index").write_text("mock index")
    _write_db(tmp_path / "conversations.db", "conversations")
    (tmp_path / "decay_state.json").write_text('{"last_run": "2025-01-01"}')

    mgr = BackupManager(str(tmp_path))
    # Simulate previous version
    mgr.version_file.write_text("2.0.0", encoding="utf-8")

    backup_dir = mgr.backup_if_needed()
    assert backup_dir is not None
    assert os.path.isdir(backup_dir)
    assert "v2.0.0" in backup_dir

    # Verify files were copied
    backup_path = Path(backup_dir)
    assert (backup_path / "anamnesis.db").exists()
    assert (backup_path / "conversations.db").exists()
    assert (backup_path / "decay_state.json").exists()

    # Verify backup info was written
    info_path = backup_path / _BACKUP_INFO_FILE
    assert info_path.exists()
    info = json.loads(info_path.read_text(encoding="utf-8"))
    assert info["plugin_version"] == PLUGIN_VERSION
    assert info["previous_version"] == "2.0.0"
    assert info["files_copied"] >= 3

    # Verify version file was updated
    assert mgr.get_stored_version() == PLUGIN_VERSION


def test_backup_if_needed_first_install(tmp_path: Path) -> None:
    _write_db(tmp_path / "anamnesis.db", "fresh install")

    mgr = BackupManager(str(tmp_path))
    backup_dir = mgr.backup_if_needed()
    assert backup_dir is not None
    assert "unknown" in backup_dir

    # Version file should now be written
    assert mgr.get_stored_version() == PLUGIN_VERSION


def test_backup_handles_missing_files_gracefully(tmp_path: Path) -> None:
    # No data files exist
    mgr = BackupManager(str(tmp_path))
    mgr.version_file.write_text("1.0.0", encoding="utf-8")

    result = mgr.backup_if_needed()
    assert result is not None  # backup dir still created
    backup_path = Path(result)
    assert backup_path.exists()
    info = json.loads((backup_path / _BACKUP_INFO_FILE).read_text(encoding="utf-8"))
    assert info["files_copied"] == 0


def test_backup_preserves_file_content(tmp_path: Path) -> None:
    original_content = "original database content for verification"
    _write_db(tmp_path / "anamnesis.db", original_content)

    mgr = BackupManager(str(tmp_path))
    mgr.version_file.write_text("1.0.0", encoding="utf-8")
    backup_dir = mgr.backup_if_needed()

    backup_file = Path(backup_dir) / "anamnesis.db"
    assert _read_db(backup_file) == original_content


def test_list_backups_empty(tmp_path: Path) -> None:
    result = BackupManager.list_backups(str(tmp_path))
    assert result == []


def test_list_backups_with_data(tmp_path: Path) -> None:
    # Create a fake backup directory
    backup_root = tmp_path / "backups" / "v1.0.0"
    backup_root.mkdir(parents=True)
    (backup_root / "anamnesis.db").write_text("old backup")
    info = {
        "plugin_version": "2.2.12",
        "previous_version": "1.0.0",
        "backup_timestamp": "2025-01-01T00:00:00+00:00",
        "files_copied": 1,
    }
    (backup_root / _BACKUP_INFO_FILE).write_text(json.dumps(info, ensure_ascii=False))

    result = BackupManager.list_backups(str(tmp_path))
    assert len(result) == 1
    assert result[0]["name"] == "v1.0.0"
    assert result[0]["plugin_version"] == "2.2.12"
    assert result[0]["previous_version"] == "1.0.0"
    assert "anamnesis.db" in result[0]["files"]


def test_backup_includes_graph_files(tmp_path: Path) -> None:
    """图数据库文件应被通配符匹配并备份。"""
    _write_db(tmp_path / "anamnesis_graph_documents.db", "graph docs")
    (tmp_path / "anamnesis_graph.index").write_text("graph index")

    mgr = BackupManager(str(tmp_path))
    mgr.version_file.write_text("2.0.0", encoding="utf-8")
    backup_dir = mgr.backup_if_needed()

    backup_path = Path(backup_dir)
    assert (backup_path / "anamnesis_graph_documents.db").exists()
    assert (backup_path / "anamnesis_graph.index").exists()


def test_backup_includes_committed_wal_data(tmp_path: Path) -> None:
    """WAL 中已提交的数据应进入无需日志副文件的数据库快照。"""
    _write_db(tmp_path / "anamnesis.db", "main")
    with closing(sqlite3.connect(tmp_path / "anamnesis.db")) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("UPDATE records SET content='wal data'")
        db.commit()
        mgr = BackupManager(str(tmp_path))
        mgr.version_file.write_text("2.0.0", encoding="utf-8")
        backup_path = Path(mgr.backup_if_needed())
        assert not (backup_path / "anamnesis.db-wal").exists()
        assert not (backup_path / "anamnesis.db-shm").exists()
        assert _read_db(backup_path / "anamnesis.db") == "wal data"


def test_get_stored_version_oserror_returns_none(tmp_path: Path) -> None:
    """当版本文件无法读取时返回 None。"""
    mgr = BackupManager(str(tmp_path))
    # 创建一个目录而非文件，触发 read_text 的 OSError
    mgr.version_file.mkdir(parents=True, exist_ok=True)
    assert mgr.get_stored_version() is None


def test_backup_snapshots_multiple_databases(tmp_path: Path) -> None:
    """两个数据库都应创建可独立读取的快照。"""
    _write_db(tmp_path / "anamnesis.db", "ok")
    _write_db(tmp_path / "conversations.db", "ok")

    mgr = BackupManager(str(tmp_path))
    mgr.version_file.write_text("1.0.0", encoding="utf-8")

    backup_dir = mgr.backup_if_needed()
    assert backup_dir is not None
    backup_path = Path(backup_dir)
    assert (backup_path / "anamnesis.db").exists()
    assert (backup_path / "conversations.db").exists()


def test_list_backups_multiple_sorted_desc(tmp_path: Path) -> None:
    """多个备份应按版本号逆序排列。"""
    for ver in ("v1.0.0", "v2.0.0", "v2.5.0"):
        root = tmp_path / "backups" / ver
        root.mkdir(parents=True)
        (root / _BACKUP_INFO_FILE).write_text(
            json.dumps({"plugin_version": "2.2.12", "previous_version": ver[1:]}),
            encoding="utf-8",
        )
        (root / "anamnesis.db").write_text(ver)

    result = BackupManager.list_backups(str(tmp_path))
    assert len(result) == 3
    # 逆序：最新版本在前
    assert result[0]["name"] == "v2.5.0"
    assert result[1]["name"] == "v2.0.0"
    assert result[2]["name"] == "v1.0.0"


def test_list_backups_skips_non_directories(tmp_path: Path) -> None:
    """backups 目录下的非目录文件应被跳过。"""
    backups_root = tmp_path / "backups"
    backups_root.mkdir(parents=True)
    (backups_root / "some_file.txt").write_text("not a backup")
    (backups_root / "v1.0.0").mkdir()
    (backups_root / "v1.0.0" / _BACKUP_INFO_FILE).write_text(
        json.dumps({"plugin_version": "2.2.12"}), encoding="utf-8"
    )

    result = BackupManager.list_backups(str(tmp_path))
    assert len(result) == 1
    assert result[0]["name"] == "v1.0.0"


def test_list_backups_corrupt_info_json(tmp_path: Path) -> None:
    """损坏的 backup_info.json 不应导致 list_backups 崩溃，应回退到默认值。"""
    root = tmp_path / "backups" / "v1.0.0"
    root.mkdir(parents=True)
    (root / _BACKUP_INFO_FILE).write_text("{not valid json", encoding="utf-8")
    (root / "anamnesis.db").write_text("data")

    result = BackupManager.list_backups(str(tmp_path))
    assert len(result) == 1
    # 损坏 info 时回退到默认字段
    assert result[0]["name"] == "v1.0.0"
    assert "anamnesis.db" in result[0]["files"]


def test_write_current_version_creates_missing_dir(tmp_path: Path) -> None:
    """write_current_version 应在 data_dir 不存在时自动创建。"""
    nested = tmp_path / "deeply" / "nested" / "data"
    mgr = BackupManager(str(nested))
    mgr.write_current_version()
    assert mgr.version_file.exists()
    assert mgr.get_stored_version() == PLUGIN_VERSION


def test_backup_info_json_contains_all_fields(tmp_path: Path) -> None:
    """验证 backup_info.json 包含所有必要字段。"""
    _write_db(tmp_path / "anamnesis.db", "test")

    mgr = BackupManager(str(tmp_path))
    previous_version = "0.0.0"
    mgr.version_file.write_text(previous_version, encoding="utf-8")
    backup_dir = mgr.backup_if_needed()

    info = json.loads(
        (Path(backup_dir) / _BACKUP_INFO_FILE).read_text(encoding="utf-8")
    )
    required_fields = [
        "plugin_version",
        "previous_version",
        "backup_timestamp",
        "backup_unix_time",
        "files_copied",
        "data_dir",
    ]
    for field in required_fields:
        assert field in info, f"Missing field: {field}"
    assert info["previous_version"] == previous_version
    assert info["plugin_version"] == PLUGIN_VERSION
    assert isinstance(info["backup_unix_time"], float)


def test_pluigin_version_constant_matches_metadata() -> None:
    """Ensure PLUGIN_VERSION in backup_manager matches metadata.yaml."""
    import yaml

    metadata_path = Path(__file__).parent.parent / "metadata.yaml"
    if not metadata_path.exists():
        return  # skip if metadata.yaml not found (CI, etc.)

    with open(metadata_path, encoding="utf-8") as f:
        metadata = yaml.safe_load(f)
    assert PLUGIN_VERSION == metadata["version"], (
        f"PLUGIN_VERSION ({PLUGIN_VERSION}) must match metadata.yaml "
        f"({metadata['version']}). Update backup_manager.py."
    )
