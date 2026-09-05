"""备份保留策略测试（份数上限 / 总体积上限 / 超大数据跳过 / 路径安全）。

线上实测背景：数据目录共 823MB，其中 backups/ 独占 515MB
（版本目录 v2.6.0-beta.3 占 299MB + 单个每日备份 216MB），
而备份子系统此前完全没有份数与体积上限，是磁盘占用的头号问题。

本文件覆盖：
- backup_settings.enabled / skip_if_larger_than_mb / 磁盘不足 三条跳过分支
  （每条都必须写入 .plugin_version，否则每次启动都会重新尝试复制数百 MB）
- max_keep 对版本目录与每日备份文件的独立裁剪
- max_total_size_mb 从最旧开始删除
- 删除动作绝不越出 <data_dir>/backups（含符号链接场景）
- config=None 的向后兼容回归
- prune_backups 返回值字段完整性
- DecayScheduler 侧的接线（跳过超大主库 + 清理版本目录）
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_anamnesis.core.managers import backup_manager as bm
from astrbot_plugin_anamnesis.core.managers.backup_manager import (
    DEFAULT_KEEP_DAYS,
    DEFAULT_MAX_KEEP,
    DEFAULT_MAX_TOTAL_SIZE_MB,
    DEFAULT_SKIP_IF_LARGER_THAN_MB,
    PLUGIN_VERSION,
    BackupManager,
    prune_backups_in,
    prune_backups_in_async,
    resolve_backup_settings,
)
from astrbot_plugin_anamnesis.core.schedulers.decay_scheduler import DecayScheduler

_BACKUP_INFO = bm._BACKUP_INFO_FILE
_MB = 1024 * 1024

PRUNE_RESULT_KEYS = {
    "removed_dirs",
    "removed_files",
    "bytes_freed",
    "total_bytes_after",
}

BACKUP_INFO_FIELDS = (
    "plugin_version",
    "previous_version",
    "backup_timestamp",
    "backup_unix_time",
    "files_copied",
    "data_dir",
    "total_bytes",
    "skipped",
    "skip_reason",
)


# --------------------------------------------------------------------------
# 测试辅助
# --------------------------------------------------------------------------


def _set_age(path: Path, age_days: float) -> None:
    """把 path 的 mtime 设置为 age_days 天前。"""
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))


def _make_version_dir(
    data_dir: Path, name: str, *, size: int = 64, age_days: float = 0.0
) -> Path:
    """伪造一个 backups/v*/ 版本变更备份目录。"""
    path = data_dir / "backups" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "anamnesis.db").write_bytes(b"d" * size)
    (path / _BACKUP_INFO).write_text(
        json.dumps({"previous_version": name[1:], "files_copied": 1}),
        encoding="utf-8",
    )
    _set_age(path, age_days)
    return path


def _make_daily_file(
    data_dir: Path, name: str, *, size: int = 64, age_days: float = 0.0
) -> Path:
    """伪造一个 db_migration.create_backup() 产出的每日备份文件。"""
    backups = data_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    path = backups / name
    path.write_bytes(b"f" * size)
    _set_age(path, age_days)
    return path


def _names(data_dir: Path) -> list[str]:
    return sorted(child.name for child in (data_dir / "backups").iterdir())


def _make_scheduler(tmp_path: Path, **kwargs):
    """构造一个只用于备份路径的 DecayScheduler（返回 scheduler 与 mock migration）。"""
    engine = Mock()
    engine.apply_daily_decay = AsyncMock(return_value=0)
    migration = Mock()
    migration.db_path = str(tmp_path / "anamnesis.db")
    migration.create_backup = AsyncMock(
        return_value=str(tmp_path / "backups" / "anamnesis_backup_20260906_010819.db")
    )
    scheduler = DecayScheduler(
        memory_engine=engine,
        decay_rate=0.01,
        data_dir=str(tmp_path),
        db_migration=migration,
        **kwargs,
    )
    return scheduler, migration


# --------------------------------------------------------------------------
# 配置读取（向后兼容 + 健壮性）
# --------------------------------------------------------------------------


def test_module_defaults_match_config_contract() -> None:
    """模块默认值必须与 _conf_schema.json 的配置契约一致。"""
    assert DEFAULT_KEEP_DAYS == 7
    assert DEFAULT_MAX_KEEP == 2
    assert DEFAULT_MAX_TOTAL_SIZE_MB == 1024
    assert DEFAULT_SKIP_IF_LARGER_THAN_MB == 512


def test_resolve_settings_none_uses_defaults() -> None:
    assert resolve_backup_settings() == resolve_backup_settings(None)
    assert resolve_backup_settings(None) == {
        "enabled": True,
        "keep_days": 7,
        "max_keep": 2,
        "max_total_size_mb": 1024,
        "skip_if_larger_than_mb": 512,
    }


def test_resolve_settings_partial_group_fills_defaults() -> None:
    settings = resolve_backup_settings({"backup_settings": {"max_keep": 5}})
    assert settings["max_keep"] == 5
    assert settings["enabled"] is True
    assert settings["keep_days"] == 7
    assert settings["max_total_size_mb"] == 1024
    assert settings["skip_if_larger_than_mb"] == 512


def test_resolve_settings_missing_group_uses_defaults() -> None:
    assert resolve_backup_settings({"other_group": {"x": 1}}) == resolve_backup_settings(
        None
    )


def test_resolve_settings_accepts_bare_group() -> None:
    settings = resolve_backup_settings({"enabled": False, "keep_days": 3})
    assert settings["enabled"] is False
    assert settings["keep_days"] == 3
    assert settings["max_keep"] == 2


def test_resolve_settings_accepts_flat_dotted_keys() -> None:
    settings = resolve_backup_settings({"backup_settings.max_total_size_mb": 256})
    assert settings["max_total_size_mb"] == 256
    assert settings["max_keep"] == 2


def test_resolve_settings_coerces_strings() -> None:
    settings = resolve_backup_settings(
        {
            "backup_settings": {
                "enabled": "false",
                "max_keep": "3",
                "max_total_size_mb": "512.7",
            }
        }
    )
    assert settings["enabled"] is False
    assert settings["max_keep"] == 3
    assert settings["max_total_size_mb"] == 512


def test_resolve_settings_garbage_falls_back_to_defaults() -> None:
    settings = resolve_backup_settings(
        {"backup_settings": {"max_keep": "not a number", "keep_days": None}}
    )
    assert settings["max_keep"] == 2
    assert settings["keep_days"] == 7


def test_resolve_settings_accepts_config_object() -> None:
    class FakeAstrBotConfig:
        def __init__(self, data: dict) -> None:
            self._data = data

        def get(self, key, default=None):
            return self._data.get(key, default)

    config = FakeAstrBotConfig(
        {"backup_settings": {"max_keep": 1, "max_total_size_mb": 64}}
    )
    settings = resolve_backup_settings(config)
    assert settings["max_keep"] == 1
    assert settings["max_total_size_mb"] == 64
    assert settings["enabled"] is True


def test_config_none_keeps_documented_defaults(tmp_path: Path) -> None:
    """回归：config=None 时与旧版构造方式完全等价。"""
    mgr = BackupManager(str(tmp_path))
    assert mgr.enabled is True
    assert mgr.keep_days == 7
    assert mgr.max_keep == 2
    assert mgr.max_total_size_mb == 1024
    assert mgr.skip_if_larger_than_mb == 512
    assert mgr.backups_dir == tmp_path / "backups"


def test_config_none_still_creates_backup(tmp_path: Path) -> None:
    """回归：单参数构造（旧调用方式）仍能正常完成版本变更备份。"""
    (tmp_path / "anamnesis.db").write_text("legacy content", encoding="utf-8")
    (tmp_path / "conversations.db").write_text("legacy conv", encoding="utf-8")

    mgr = BackupManager(str(tmp_path))
    mgr.version_file.write_text("2.0.0", encoding="utf-8")

    backup_dir = mgr.backup_if_needed()
    assert backup_dir is not None
    assert "v2.0.0" in backup_dir
    copied = Path(backup_dir) / "anamnesis.db"
    assert copied.read_text(encoding="utf-8") == "legacy content"
    assert (Path(backup_dir) / "conversations.db").exists()
    assert mgr.get_stored_version() == PLUGIN_VERSION


# --------------------------------------------------------------------------
# backup_if_needed 的三条跳过分支
# --------------------------------------------------------------------------


def test_disabled_skips_backup_but_writes_version(tmp_path: Path) -> None:
    """enabled=false：不复制任何文件，但必须写入版本号避免每次启动重判。"""
    (tmp_path / "anamnesis.db").write_bytes(b"x" * 4096)

    mgr = BackupManager(str(tmp_path), {"backup_settings": {"enabled": False}})
    mgr.version_file.write_text("2.6.1", encoding="utf-8")
    assert mgr.enabled is False
    assert mgr.needs_backup() is True

    assert mgr.backup_if_needed() is None
    assert not (tmp_path / "backups").exists()
    assert mgr.get_stored_version() == PLUGIN_VERSION
    assert mgr.needs_backup() is False


def test_skip_if_larger_than_mb_skips_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """待备份数据超过阈值时跳过复制，写版本号并给出可操作的 warning。"""
    (tmp_path / "anamnesis.db").write_bytes(b"x" * (2 * _MB))

    mgr = BackupManager(
        str(tmp_path), {"backup_settings": {"skip_if_larger_than_mb": 1}}
    )
    mgr.version_file.write_text("2.6.1", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert mgr.backup_if_needed() is None

    assert not (tmp_path / "backups").exists()
    assert mgr.get_stored_version() == PLUGIN_VERSION
    messages = [record.getMessage() for record in caplog.records]
    assert any("skip_if_larger_than_mb" in message for message in messages)
    assert any("2.0MB" in message for message in messages)


def test_skip_if_larger_than_mb_zero_disables_guard(tmp_path: Path) -> None:
    """阈值 <=0 表示不限，超大数据也照常备份。"""
    (tmp_path / "anamnesis.db").write_bytes(b"x" * (2 * _MB))

    mgr = BackupManager(
        str(tmp_path), {"backup_settings": {"skip_if_larger_than_mb": 0}}
    )
    mgr.version_file.write_text("2.6.1", encoding="utf-8")

    backup_dir = mgr.backup_if_needed()
    assert backup_dir is not None
    assert (Path(backup_dir) / "anamnesis.db").stat().st_size == 2 * _MB


def test_low_disk_space_skips_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """磁盘剩余空间不足 数据体积*1.1 时跳过复制，但仍写版本号。"""
    (tmp_path / "anamnesis.db").write_bytes(b"x" * _MB)

    def fake_disk_usage(path):
        return SimpleNamespace(total=100 * _MB, used=100 * _MB, free=1024)

    monkeypatch.setattr(bm.shutil, "disk_usage", fake_disk_usage)

    mgr = BackupManager(
        str(tmp_path), {"backup_settings": {"skip_if_larger_than_mb": 0}}
    )
    mgr.version_file.write_text("2.6.1", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert mgr.backup_if_needed() is None

    assert not (tmp_path / "backups").exists()
    assert mgr.get_stored_version() == PLUGIN_VERSION
    assert any(
        "磁盘剩余空间" in record.getMessage() for record in caplog.records
    )


def test_unknown_free_space_does_not_block_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拿不到磁盘信息时不应误判为磁盘不足。"""
    (tmp_path / "anamnesis.db").write_bytes(b"x" * 1024)

    def boom(path):
        raise OSError("disk_usage unavailable")

    monkeypatch.setattr(bm.shutil, "disk_usage", boom)

    mgr = BackupManager(str(tmp_path))
    mgr.version_file.write_text("2.6.1", encoding="utf-8")
    assert mgr.backup_if_needed() is not None


def test_backup_info_contains_retention_fields(tmp_path: Path) -> None:
    """backup_info.json 在原有字段之上追加 total_bytes / skipped / skip_reason。"""
    (tmp_path / "anamnesis.db").write_bytes(b"x" * 512)

    mgr = BackupManager(str(tmp_path))
    mgr.version_file.write_text("2.6.1", encoding="utf-8")
    backup_dir = mgr.backup_if_needed()

    info = json.loads(
        (Path(backup_dir) / _BACKUP_INFO).read_text(encoding="utf-8")
    )
    for field in BACKUP_INFO_FIELDS:
        assert field in info, field
    assert info["total_bytes"] >= 512
    assert info["skipped"] is False
    assert info["skip_reason"] is None
    assert info["plugin_version"] == PLUGIN_VERSION


def test_backup_if_needed_prunes_old_version_dirs(tmp_path: Path) -> None:
    """版本变更备份成功后立刻执行保留策略。"""
    (tmp_path / "anamnesis.db").write_bytes(b"x" * 512)
    _make_version_dir(tmp_path, "v1.0.0", age_days=30)
    _make_version_dir(tmp_path, "v2.0.0", age_days=20)
    _make_version_dir(tmp_path, "v3.0.0", age_days=10)

    mgr = BackupManager(str(tmp_path), {"backup_settings": {"max_keep": 2}})
    mgr.version_file.write_text("2.6.1", encoding="utf-8")

    backup_dir = mgr.backup_if_needed()
    assert backup_dir is not None
    assert _names(tmp_path) == ["v2.6.1", "v3.0.0"]


# --------------------------------------------------------------------------
# max_keep
# --------------------------------------------------------------------------


def test_max_keep_trims_version_dirs(tmp_path: Path) -> None:
    """线上 backups/v2.6.0-beta.3 占 299MB 却无人回收，max_keep 负责裁掉它。"""
    _make_version_dir(tmp_path, "v1.0.0", age_days=30)
    _make_version_dir(tmp_path, "v2.0.0", age_days=20)
    kept_old = _make_version_dir(tmp_path, "v3.0.0", age_days=10)
    kept_new = _make_version_dir(tmp_path, "v4.0.0", age_days=1)

    result = prune_backups_in(
        tmp_path, max_keep=2, max_total_size_mb=0, keep_days=0
    )

    assert kept_old.exists()
    assert kept_new.exists()
    assert _names(tmp_path) == ["v3.0.0", "v4.0.0"]
    assert len(result["removed_dirs"]) == 2
    assert result["removed_files"] == []
    assert result["bytes_freed"] > 0


def test_max_keep_trims_daily_files_including_legacy_names(tmp_path: Path) -> None:
    """每日备份文件同样受 max_keep 约束；遗留的 livingmemory_backup_* 也能匹配。"""
    _make_daily_file(tmp_path, "anamnesis_backup_20260901_000000.db", age_days=4)
    _make_daily_file(tmp_path, "anamnesis_backup_20260902_000000.db", age_days=3)
    legacy = _make_daily_file(
        tmp_path, "livingmemory_backup_20260903_000000.db", age_days=2
    )
    newest = _make_daily_file(
        tmp_path, "anamnesis_backup_20260904_000000.db", age_days=1
    )

    result = prune_backups_in(
        tmp_path, max_keep=2, max_total_size_mb=0, keep_days=0
    )

    assert legacy.exists()
    assert newest.exists()
    assert len(result["removed_files"]) == 2
    assert result["removed_dirs"] == []


def test_max_keep_counts_dirs_and_files_independently(tmp_path: Path) -> None:
    """版本目录与每日文件各自独立计数，不会互相挤占份额。"""
    for index in (1, 2, 3):
        _make_version_dir(tmp_path, "v" + str(index) + ".0.0", age_days=10 - index)
        _make_daily_file(
            tmp_path,
            "anamnesis_backup_2026090" + str(index) + "_000000.db",
            age_days=10 - index,
        )

    prune_backups_in(tmp_path, max_keep=2, max_total_size_mb=0, keep_days=0)

    assert _names(tmp_path) == [
        "anamnesis_backup_20260902_000000.db",
        "anamnesis_backup_20260903_000000.db",
        "v2.0.0",
        "v3.0.0",
    ]


def test_legacy_vunknown_dir_is_prunable(tmp_path: Path) -> None:
    """线上遗留的 backups/vunknown/ 也在保留策略覆盖范围内。"""
    legacy = _make_version_dir(tmp_path, "vunknown", age_days=60)
    keep = _make_version_dir(tmp_path, "v3.0.0", age_days=1)

    prune_backups_in(tmp_path, max_keep=1, max_total_size_mb=0, keep_days=0)

    assert not legacy.exists()
    assert keep.exists()


def test_non_positive_limits_disable_pruning(tmp_path: Path) -> None:
    """三个规则全部 <=0 时表示不限，任何东西都不删。"""
    for index in (1, 2, 3, 4):
        _make_version_dir(tmp_path, "v" + str(index) + ".0.0", age_days=40 - index)
    stale = _make_daily_file(
        tmp_path, "anamnesis_backup_20250101_000000.db", age_days=400
    )

    result = prune_backups_in(
        tmp_path, max_keep=0, max_total_size_mb=0, keep_days=0
    )

    assert stale.exists()
    assert result["removed_dirs"] == []
    assert result["removed_files"] == []
    assert result["bytes_freed"] == 0
    assert result["total_bytes_after"] > 0


# --------------------------------------------------------------------------
# keep_days
# --------------------------------------------------------------------------


def test_keep_days_only_expires_daily_files(tmp_path: Path) -> None:
    """保持既有行为：keep_days 只作用于每日备份文件，版本目录交给 max_keep。"""
    stale_dir = _make_version_dir(tmp_path, "v1.0.0", age_days=90)
    stale_file = _make_daily_file(
        tmp_path, "anamnesis_backup_20250101_000000.db", age_days=90
    )
    fresh_file = _make_daily_file(
        tmp_path, "anamnesis_backup_20260906_000000.db", age_days=1
    )

    result = prune_backups_in(
        tmp_path, max_keep=0, max_total_size_mb=0, keep_days=7
    )

    assert stale_dir.exists()
    assert not stale_file.exists()
    assert fresh_file.exists()
    assert result["removed_files"] == [str(stale_file)]
    assert result["removed_dirs"] == []


# --------------------------------------------------------------------------
# max_total_size_mb
# --------------------------------------------------------------------------


def test_max_total_size_removes_oldest_first(tmp_path: Path) -> None:
    size = 600 * 1024
    oldest = _make_daily_file(
        tmp_path, "anamnesis_backup_20260901_000000.db", size=size, age_days=3
    )
    middle = _make_daily_file(
        tmp_path, "anamnesis_backup_20260903_000000.db", size=size, age_days=2
    )
    newest = _make_daily_file(
        tmp_path, "anamnesis_backup_20260905_000000.db", size=size, age_days=1
    )

    result = prune_backups_in(
        tmp_path, max_keep=0, max_total_size_mb=1, keep_days=0
    )

    assert not oldest.exists()
    assert not middle.exists()
    assert newest.exists()
    assert result["removed_files"] == [str(oldest), str(middle)]
    assert result["bytes_freed"] == 2 * size
    assert result["total_bytes_after"] == size


def test_max_total_size_mixes_dirs_and_files_in_one_timeline(tmp_path: Path) -> None:
    """总体积超限时，版本目录与每日文件放在同一个时间序列里统一排序。"""
    size = 600 * 1024
    old_dir = _make_version_dir(tmp_path, "v1.0.0", size=size, age_days=5)
    mid_file = _make_daily_file(
        tmp_path, "anamnesis_backup_20260903_000000.db", size=size, age_days=3
    )
    new_dir = _make_version_dir(tmp_path, "v2.0.0", size=size, age_days=1)

    result = prune_backups_in(
        tmp_path, max_keep=0, max_total_size_mb=1, keep_days=0
    )

    assert not old_dir.exists()
    assert not mid_file.exists()
    assert new_dir.exists()
    assert result["removed_dirs"] == [str(old_dir)]
    assert result["removed_files"] == [str(mid_file)]
    assert result["total_bytes_after"] <= 1 * _MB


def test_max_total_size_warns_when_still_over_limit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """无法自动清理到达标时给出明确 warning，而不是静默失败。"""
    backups = tmp_path / "backups"
    backups.mkdir(parents=True)
    (backups / "manual_dump.tar").write_bytes(b"x" * (2 * _MB))

    with caplog.at_level(logging.WARNING):
        result = prune_backups_in(
            tmp_path, max_keep=2, max_total_size_mb=1, keep_days=0
        )

    assert (backups / "manual_dump.tar").exists()
    assert result["removed_files"] == []
    assert any(
        "仍超出上限" in record.getMessage() for record in caplog.records
    )


# --------------------------------------------------------------------------
# 路径安全：删除动作绝不越出 backups/
# --------------------------------------------------------------------------


def test_safe_remove_refuses_path_outside_backups(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir(parents=True)
    outsider = tmp_path / "anamnesis.db"
    outsider.write_bytes(b"live database")

    result = {"removed_dirs": [], "removed_files": []}
    removed, freed = bm._safe_remove(outsider, root.resolve(), result)

    assert removed is False
    assert freed == 0
    assert outsider.exists()
    assert result == {"removed_dirs": [], "removed_files": []}


def test_safe_remove_refuses_backups_root_itself(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir(parents=True)

    result = {"removed_dirs": [], "removed_files": []}
    removed, _freed = bm._safe_remove(root, root.resolve(), result)

    assert removed is False
    assert root.exists()
    assert result["removed_dirs"] == []


def test_is_inside_rejects_sibling_and_parent(tmp_path: Path) -> None:
    root = (tmp_path / "backups").resolve()
    assert bm._is_inside((root / "v1.0.0"), root) is True
    assert bm._is_inside(root, root) is False
    assert bm._is_inside(tmp_path.resolve(), root) is False
    assert bm._is_inside((tmp_path / "backups_old" / "x").resolve(), root) is False


def test_prune_refuses_symlinked_version_dir(tmp_path: Path) -> None:
    """backups/v* 是指向外部目录的符号链接时，绝不能删掉链接目标。"""
    outside = tmp_path / "outside_data"
    outside.mkdir()
    important = outside / "important.db"
    important.write_bytes(b"x" * 256)

    backups = tmp_path / "backups"
    backups.mkdir()
    link = backups / "v0.0.1"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("当前环境不支持创建符号链接: " + str(exc))

    _set_age(outside, 60)
    _make_version_dir(tmp_path, "v9.0.0", age_days=2)
    _make_version_dir(tmp_path, "v9.1.0", age_days=1)

    result = prune_backups_in(
        tmp_path, max_keep=2, max_total_size_mb=0, keep_days=0
    )

    assert outside.exists()
    assert important.exists()
    assert important.read_bytes() == b"x" * 256
    assert result["removed_dirs"] == []
    assert str(link) not in result["removed_dirs"]


def test_prune_refuses_symlinked_daily_file(tmp_path: Path) -> None:
    """每日备份文件是符号链接时，同样不能删掉链接指向的外部文件。"""
    outside = tmp_path / "outside_data"
    outside.mkdir()
    real = outside / "real.db"
    real.write_bytes(b"y" * 256)

    backups = tmp_path / "backups"
    backups.mkdir()
    link = backups / "anamnesis_backup_19700101_000000.db"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("当前环境不支持创建符号链接: " + str(exc))

    _set_age(real, 90)
    _make_daily_file(tmp_path, "anamnesis_backup_20260905_000000.db", age_days=1)
    _make_daily_file(tmp_path, "anamnesis_backup_20260906_000000.db", age_days=0)

    result = prune_backups_in(
        tmp_path, max_keep=2, max_total_size_mb=0, keep_days=7
    )

    assert real.exists()
    assert real.read_bytes() == b"y" * 256
    assert result["removed_files"] == []


def test_unrelated_entries_in_backups_are_never_removed(tmp_path: Path) -> None:
    """不匹配两类备份模式的内容（例如用户手动放置的副本）不参与删除。"""
    backups = tmp_path / "backups"
    backups.mkdir(parents=True)
    readme = backups / "README.txt"
    readme.write_text("do not delete", encoding="utf-8")
    manual = backups / "manual_copy"
    manual.mkdir()
    (manual / "anamnesis.db").write_bytes(b"x" * 64)
    _set_age(readme, 999)
    _set_age(manual, 999)
    _make_version_dir(tmp_path, "v1.0.0", age_days=1)

    result = prune_backups_in(
        tmp_path, max_keep=1, max_total_size_mb=0, keep_days=1
    )

    assert readme.exists()
    assert manual.exists()
    assert (manual / "anamnesis.db").exists()
    assert result["removed_dirs"] == []
    assert result["removed_files"] == []


def test_prune_missing_backups_dir_is_noop(tmp_path: Path) -> None:
    result = prune_backups_in(tmp_path, max_keep=1, max_total_size_mb=1, keep_days=1)
    assert result == {
        "removed_dirs": [],
        "removed_files": [],
        "bytes_freed": 0,
        "total_bytes_after": 0,
    }


# --------------------------------------------------------------------------
# prune_backups 返回值 / 异步包装 / list_backups
# --------------------------------------------------------------------------


def test_prune_backups_result_shape(tmp_path: Path) -> None:
    removed_dir = _make_version_dir(tmp_path, "v1.0.0", size=2048, age_days=30)
    _make_version_dir(tmp_path, "v2.0.0", size=2048, age_days=1)
    removed_file = _make_daily_file(
        tmp_path, "anamnesis_backup_20250101_000000.db", size=1024, age_days=40
    )
    _make_daily_file(
        tmp_path, "anamnesis_backup_20260906_000000.db", size=1024, age_days=0
    )

    mgr = BackupManager(
        str(tmp_path),
        {
            "backup_settings": {
                "max_keep": 1,
                "max_total_size_mb": 0,
                "keep_days": 0,
            }
        },
    )
    result = mgr.prune_backups()

    assert set(result) == PRUNE_RESULT_KEYS
    assert isinstance(result["removed_dirs"], list)
    assert isinstance(result["removed_files"], list)
    assert isinstance(result["bytes_freed"], int)
    assert isinstance(result["total_bytes_after"], int)
    assert result["removed_dirs"] == [str(removed_dir)]
    assert result["removed_files"] == [str(removed_file)]
    assert result["bytes_freed"] > 0
    assert result["total_bytes_after"] == bm._path_size(tmp_path / "backups")


@pytest.mark.asyncio
async def test_prune_backups_async_wrappers(tmp_path: Path) -> None:
    _make_version_dir(tmp_path, "v1.0.0", age_days=30)
    _make_version_dir(tmp_path, "v2.0.0", age_days=1)

    mgr = BackupManager(
        str(tmp_path),
        {
            "backup_settings": {
                "max_keep": 1,
                "max_total_size_mb": 0,
                "keep_days": 0,
            }
        },
    )
    result = await mgr.prune_backups_async()
    assert set(result) == PRUNE_RESULT_KEYS
    assert len(result["removed_dirs"]) == 1

    _make_daily_file(tmp_path, "anamnesis_backup_20250101_000000.db", age_days=40)
    _make_daily_file(tmp_path, "anamnesis_backup_20260906_000000.db", age_days=0)
    result2 = await prune_backups_in_async(
        tmp_path, max_keep=1, max_total_size_mb=0, keep_days=0
    )
    assert set(result2) == PRUNE_RESULT_KEYS
    assert len(result2["removed_files"]) == 1


def test_list_backups_reports_size_bytes(tmp_path: Path) -> None:
    """list_backups 保留原有字段，仅追加 size_bytes。"""
    _make_version_dir(tmp_path, "v1.0.0", size=4096)

    entries = BackupManager.list_backups(str(tmp_path))

    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "v1.0.0"
    assert entry["directory"] == str(tmp_path / "backups" / "v1.0.0")
    assert entry["previous_version"] == "1.0.0"
    assert "anamnesis.db" in entry["files"]
    assert entry["file_count"] == 2
    assert entry["size_bytes"] >= 4096


# --------------------------------------------------------------------------
# DecayScheduler 接线
# --------------------------------------------------------------------------


def test_scheduler_backup_retention_defaults(tmp_path: Path) -> None:
    """新增的三个关键字参数必须有默认值，保持现有调用方式可用。"""
    scheduler, _migration = _make_scheduler(tmp_path)
    assert scheduler.backup_max_keep == 2
    assert scheduler.backup_max_total_size_mb == 1024
    assert scheduler.backup_skip_if_larger_than_mb == 512


@pytest.mark.asyncio
async def test_scheduler_cleanup_now_prunes_version_dirs(tmp_path: Path) -> None:
    """原实现只 glob 每日文件，导致 backups/v*/ 永远不被回收。"""
    _make_version_dir(tmp_path, "v1.0.0", age_days=30)
    _make_version_dir(tmp_path, "v2.0.0", age_days=20)
    keep = _make_version_dir(tmp_path, "v3.0.0", age_days=10)

    scheduler, _migration = _make_scheduler(
        tmp_path,
        backup_keep_days=7,
        backup_max_keep=1,
        backup_max_total_size_mb=0,
    )
    await scheduler._cleanup_old_backups()

    assert keep.exists()
    assert _names(tmp_path) == ["v3.0.0"]


@pytest.mark.asyncio
async def test_scheduler_cleanup_keeps_keep_days_behaviour(tmp_path: Path) -> None:
    """既有的 keep_days 行为不能退化。"""
    stale = _make_daily_file(
        tmp_path, "anamnesis_backup_20250101_000000.db", age_days=10
    )
    fresh = _make_daily_file(
        tmp_path, "anamnesis_backup_20260906_000000.db", age_days=1
    )

    scheduler, _migration = _make_scheduler(
        tmp_path,
        backup_keep_days=7,
        backup_max_keep=0,
        backup_max_total_size_mb=0,
    )
    await scheduler._cleanup_old_backups()

    assert not stale.exists()
    assert fresh.exists()


@pytest.mark.asyncio
async def test_scheduler_cleanup_applies_total_size_cap(tmp_path: Path) -> None:
    size = 600 * 1024
    oldest = _make_daily_file(
        tmp_path, "anamnesis_backup_20260901_000000.db", size=size, age_days=3
    )
    newest = _make_daily_file(
        tmp_path, "anamnesis_backup_20260905_000000.db", size=size, age_days=1
    )

    scheduler, _migration = _make_scheduler(
        tmp_path,
        backup_keep_days=0,
        backup_max_keep=0,
        backup_max_total_size_mb=1,
    )
    await scheduler._cleanup_old_backups()

    assert not oldest.exists()
    assert newest.exists()


@pytest.mark.asyncio
async def test_scheduler_skips_daily_backup_for_oversized_db(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """216MB 主库 * keep_days=7 会堆出 1.5GB，必须在 create_backup 之前拦住。"""
    (tmp_path / "anamnesis.db").write_bytes(b"x" * (2 * _MB))
    stale = _make_daily_file(
        tmp_path, "anamnesis_backup_20250101_000000.db", age_days=30
    )

    scheduler, migration = _make_scheduler(
        tmp_path,
        backup_keep_days=7,
        backup_max_keep=2,
        backup_max_total_size_mb=0,
        backup_skip_if_larger_than_mb=1,
    )

    with caplog.at_level(logging.WARNING):
        await scheduler._run_backup()

    migration.create_backup.assert_not_called()
    assert not stale.exists()
    assert any(
        "skip_if_larger_than_mb" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_scheduler_runs_daily_backup_when_db_is_small(tmp_path: Path) -> None:
    (tmp_path / "anamnesis.db").write_bytes(b"x" * 1024)
    (tmp_path / "backups").mkdir()

    scheduler, migration = _make_scheduler(
        tmp_path, backup_skip_if_larger_than_mb=512
    )
    await scheduler._run_backup()

    migration.create_backup.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_skip_guard_disabled_when_zero(tmp_path: Path) -> None:
    (tmp_path / "anamnesis.db").write_bytes(b"x" * (2 * _MB))
    (tmp_path / "backups").mkdir()

    scheduler, migration = _make_scheduler(
        tmp_path, backup_skip_if_larger_than_mb=0
    )
    await scheduler._run_backup()

    migration.create_backup.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_cleanup_without_migration_is_noop(tmp_path: Path) -> None:
    scheduler, _migration = _make_scheduler(tmp_path)
    scheduler.db_migration = None
    await scheduler._cleanup_old_backups()
    await scheduler._run_backup()
    assert not (tmp_path / "backups").exists()

def test_prune_refuses_injected_out_of_scope_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """跨平台版安全断言：即使候选列表被污染成 backups/ 之外的路径也绝不删除。

    符号链接测试在无权限的 Windows 上会 skip，这里直接注入伪造候选，保证
    "删除动作必须落在 backups/ 内" 这条硬性要求在任何平台上都被验证到。
    """
    live_db = tmp_path / "anamnesis.db"
    live_db.write_bytes(b"live database")
    sibling = tmp_path / "backups_old"
    sibling.mkdir()
    (sibling / "anamnesis.db").write_bytes(b"another live copy")

    kept = _make_version_dir(tmp_path, "v3.0.0", age_days=1)
    real_daily = _make_daily_file(
        tmp_path, "anamnesis_backup_20260906_000000.db", age_days=0
    )

    original = bm._collect_prunable

    def polluted(backups_dir: Path):
        version_dirs, daily_files = original(backups_dir)
        version_dirs.append(bm._Entry(sibling, 0.0))
        daily_files.append(bm._Entry(live_db, 0.0))
        return version_dirs, daily_files

    monkeypatch.setattr(bm, "_collect_prunable", polluted)

    with caplog.at_level(logging.ERROR):
        result = prune_backups_in(
            tmp_path, max_keep=1, max_total_size_mb=0, keep_days=0
        )

    assert live_db.exists()
    assert live_db.read_bytes() == b"live database"
    assert sibling.exists()
    assert (sibling / "anamnesis.db").exists()
    assert kept.exists()
    assert real_daily.exists()
    assert result["removed_dirs"] == []
    assert result["removed_files"] == []
    assert result["bytes_freed"] == 0
    errors = [record.getMessage() for record in caplog.records]
    assert any("安全检查未通过" in message for message in errors)
    assert any("已拒绝删除" in message for message in errors)
