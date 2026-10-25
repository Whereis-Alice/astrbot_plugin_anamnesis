"""Version-triggered data backup manager and backups retention policy.

Automatically backs up all plugin data files when the plugin version changes,
storing each backup under a version-tagged directory for easy recovery.

This module also owns the retention policy for everything under
"<data_dir>/backups", because that directory used to grow without any bound
(observed in production: 515MB of backups next to a 216MB live database).

Three configuration keys drive the policy (config group: backup_settings):
    max_keep                how many version dirs / daily db files to keep
    max_total_size_mb       hard cap for the whole backups tree
    skip_if_larger_than_mb  refuse to create a full backup for oversized data

Safety: every destructive operation is confined to "<data_dir>/backups".
Each candidate path is resolved and verified to live inside that directory
before removal; anything else is refused and logged as an error.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from astrbot.api import logger

# Match metadata.yaml - single source of truth for the plugin version.
# Keep in sync with the @register decorator in main.py.
PLUGIN_VERSION = "3.5.10"

_VERSION_FILE = ".plugin_version"
_BACKUP_INFO_FILE = "backup_info.json"
_BACKUPS_DIRNAME = "backups"

_MB = 1024 * 1024

# Version-change backups created by this manager: backups/v<previous version>/
_VERSION_DIR_GLOB = "v*"
# Daily backups created by storage/db_migration.py::create_backup(), e.g.
# anamnesis_backup_20260906_010819.db (also matches legacy livingmemory_* files).
_DAILY_BACKUP_GLOB = "*_backup_*.db"

# Files/patterns to include in a full backup (relative to data_dir).
_BACKUP_PATTERNS: list[str] = [
    "anamnesis.db",
    "anamnesis.index",
    "anamnesis_graph_documents.db",
    "anamnesis_graph.index",
    "conversations.db",
    "decay_state.json",
    "*.db-wal",
    "*.db-shm",
]

# Defaults mirror the backup_settings group of _conf_schema.json.
DEFAULT_ENABLED = True
DEFAULT_KEEP_DAYS = 7
DEFAULT_MAX_KEEP = 2
DEFAULT_MAX_TOTAL_SIZE_MB = 1024
DEFAULT_SKIP_IF_LARGER_THAN_MB = 512

_SETTING_DEFAULTS: dict[str, Any] = {
    "enabled": DEFAULT_ENABLED,
    "keep_days": DEFAULT_KEEP_DAYS,
    "max_keep": DEFAULT_MAX_KEEP,
    "max_total_size_mb": DEFAULT_MAX_TOTAL_SIZE_MB,
    "skip_if_larger_than_mb": DEFAULT_SKIP_IF_LARGER_THAN_MB,
}


class _Entry(NamedTuple):
    """A prunable backup artifact plus its modification time."""

    path: Path
    mtime: float


# ----------------------------------------------------------------------
# Configuration helpers
# ----------------------------------------------------------------------


def _as_bool(value: Any, default: bool) -> bool:
    """Coerce a config value to bool, falling back to default."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return default


def _as_int(value: Any, default: int) -> int:
    """Coerce a config value to int, falling back to default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _lookup(source: Any, key: str) -> Any:
    """Best-effort key lookup on dict-like or attribute-based config objects."""
    if source is None:
        return None
    getter = getattr(source, "get", None)
    if callable(getter):
        try:
            value = getter(key)
        except Exception:
            value = None
        if value is not None:
            return value
    try:
        return getattr(source, key)
    except Exception:
        return None


def resolve_backup_settings(config: Any = None) -> dict[str, Any]:
    """Read the backup_settings group from a plugin config, robustly.

    Accepts AstrBotConfig / plain dict / ConfigManager-like objects, a mapping
    that *is* the backup_settings group, or flat dotted keys. Anything missing
    or unparsable falls back to the module defaults, so config=None preserves
    the historical behaviour of this manager.
    """
    resolved = dict(_SETTING_DEFAULTS)
    if config is None:
        return resolved

    group = _lookup(config, "backup_settings")
    if group is None:
        if any(_lookup(config, key) is not None for key in _SETTING_DEFAULTS):
            # The caller handed us the group itself.
            group = config
        else:
            dotted = {
                key: _lookup(config, "backup_settings." + key)
                for key in _SETTING_DEFAULTS
            }
            if any(value is not None for value in dotted.values()):
                group = {k: v for k, v in dotted.items() if v is not None}
    if group is None:
        return resolved

    resolved["enabled"] = _as_bool(_lookup(group, "enabled"), DEFAULT_ENABLED)
    for key in (
        "keep_days",
        "max_keep",
        "max_total_size_mb",
        "skip_if_larger_than_mb",
    ):
        resolved[key] = _as_int(_lookup(group, key), _SETTING_DEFAULTS[key])
    return resolved


# ----------------------------------------------------------------------
# Filesystem helpers
# ----------------------------------------------------------------------


def _path_size(path: Path) -> int:
    """Bytes used by a file, or by every regular file under a directory."""
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return 0
    except OSError:
        return 0

    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            file_path = Path(root) / name
            try:
                if file_path.is_symlink():
                    continue
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def _path_mtime(path: Path) -> float:
    """Modification time, or 0.0 when unreadable (so it prunes first)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _is_inside(candidate: Path, root: Path) -> bool:
    """True only when candidate is a strict descendant of root."""
    if candidate == root:
        return False
    try:
        return candidate.is_relative_to(root)
    except (AttributeError, TypeError, ValueError):
        try:
            return os.path.commonpath([str(candidate), str(root)]) == str(root)
        except (OSError, ValueError):
            return False


def _safe_remove(entry: Path, root: Path, result: dict[str, Any]) -> tuple[bool, int]:
    """Delete one backup artifact, refusing anything outside root.

    Returns (removed, bytes_freed). Failures never raise: they are logged and
    reported so the caller can keep pruning the remaining entries.
    """
    try:
        resolved = entry.resolve()
    except OSError as exc:
        logger.error(f"[BackupManager] 无法解析路径，已跳过删除 {entry}: {exc}")
        return False, 0

    if not _is_inside(resolved, root):
        logger.error(
            f"[BackupManager] 安全检查未通过：{entry} 实际指向 {resolved}，"
            f"不在备份目录 {root} 之内，已拒绝删除"
        )
        return False, 0

    size = _path_size(entry)
    try:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
            result["removed_dirs"].append(str(entry))
        else:
            entry.unlink()
            result["removed_files"].append(str(entry))
    except OSError as exc:
        logger.warning(f"[BackupManager] 删除备份失败 {entry}: {exc}")
        return False, 0
    return True, size


def _collect_prunable(backups_dir: Path) -> tuple[list[_Entry], list[_Entry]]:
    """Split the backups dir into (version dirs, daily backup files)."""
    version_dirs: list[_Entry] = []
    daily_files: list[_Entry] = []
    try:
        children = list(backups_dir.iterdir())
    except OSError as exc:
        logger.warning(f"[BackupManager] 无法读取备份目录 {backups_dir}: {exc}")
        return version_dirs, daily_files

    for child in children:
        try:
            is_link = child.is_symlink()
            is_dir = child.is_dir()
            is_file = child.is_file()
        except OSError:
            continue
        name = child.name
        if fnmatch.fnmatch(name, _VERSION_DIR_GLOB) and (is_dir or is_link):
            version_dirs.append(_Entry(child, _path_mtime(child)))
        elif fnmatch.fnmatch(name, _DAILY_BACKUP_GLOB) and (is_file or is_link):
            daily_files.append(_Entry(child, _path_mtime(child)))
    return version_dirs, daily_files


def _trim_to_max_keep(
    entries: list[_Entry],
    max_keep: int,
    root: Path,
    result: dict[str, Any],
) -> tuple[list[_Entry], int]:
    """Keep the newest max_keep entries and delete the rest."""
    if max_keep <= 0 or len(entries) <= max_keep:
        return entries, 0

    ordered = sorted(
        entries, key=lambda item: (item.mtime, item.path.name), reverse=True
    )
    survivors = list(ordered[:max_keep])
    freed = 0
    for entry in ordered[max_keep:]:
        removed, size = _safe_remove(entry.path, root, result)
        if removed:
            freed += size
        else:
            # Undeletable (or refused): keep tracking it so totals stay honest.
            survivors.append(entry)
    return survivors, freed


def prune_backups_in(
    data_dir: str | Path,
    *,
    max_keep: int = DEFAULT_MAX_KEEP,
    max_total_size_mb: int = DEFAULT_MAX_TOTAL_SIZE_MB,
    keep_days: int = 0,
) -> dict[str, Any]:
    """Apply the retention policy to "<data_dir>/backups".

    Rules run in this order:
      1. keep_days - expire aged daily backup files (0 disables this step)
      2. max_keep - applied separately to version dirs and daily files
      3. max_total_size_mb - delete oldest-first across both kinds
    Any value <= 0 disables the corresponding rule.

    This is the single implementation of backup deletion: BackupManager and
    DecayScheduler both route through it, so the path-safety check cannot be
    bypassed by a duplicated copy of the logic.
    """
    result: dict[str, Any] = {
        "removed_dirs": [],
        "removed_files": [],
        "bytes_freed": 0,
        "total_bytes_after": 0,
    }

    backups_dir = Path(data_dir) / _BACKUPS_DIRNAME
    try:
        if not backups_dir.is_dir():
            return result
        root = backups_dir.resolve()
    except OSError as exc:
        logger.warning(f"[BackupManager] 无法访问备份目录 {backups_dir}: {exc}")
        return result

    version_dirs, daily_files = _collect_prunable(backups_dir)
    freed = 0

    # 1) keep_days - expire aged daily backup files.
    if keep_days > 0 and daily_files:
        cutoff = time.time() - keep_days * 86400
        survivors: list[_Entry] = []
        for entry in daily_files:
            if entry.mtime < cutoff:
                removed, size = _safe_remove(entry.path, root, result)
                if removed:
                    freed += size
                    continue
            survivors.append(entry)
        daily_files = survivors

    # 2) max_keep - version dirs and daily files are counted independently.
    version_dirs, dir_freed = _trim_to_max_keep(version_dirs, max_keep, root, result)
    daily_files, file_freed = _trim_to_max_keep(daily_files, max_keep, root, result)
    freed += dir_freed + file_freed

    # 3) max_total_size_mb - one oldest-first sequence across both kinds.
    limit = max_total_size_mb * _MB if max_total_size_mb > 0 else 0
    if limit > 0:
        total = _path_size(backups_dir)
        if total > limit:
            candidates = sorted(
                version_dirs + daily_files,
                key=lambda item: (item.mtime, item.path.name),
            )
            for entry in candidates:
                if total <= limit:
                    break
                removed, size = _safe_remove(entry.path, root, result)
                if removed:
                    freed += size
                    total -= size
            if total > limit:
                logger.warning(
                    f"[BackupManager] 备份目录仍超出上限 "
                    f"({total / _MB:.1f}MB > {max_total_size_mb}MB)，"
                    f"已无可自动清理的备份条目，请手动检查 {backups_dir}"
                )

    result["bytes_freed"] = freed
    result["total_bytes_after"] = _path_size(backups_dir)

    removed_dirs = len(result["removed_dirs"])
    removed_files = len(result["removed_files"])
    if removed_dirs or removed_files:
        total_after = result["total_bytes_after"]
        logger.info(
            f"[BackupManager] 备份保留策略已执行：删除版本目录 {removed_dirs} 个、"
            f"备份文件 {removed_files} 个，释放 {freed / _MB:.1f}MB，"
            f"备份目录当前占用 {total_after / _MB:.1f}MB"
        )
    return result


async def prune_backups_in_async(
    data_dir: str | Path,
    *,
    max_keep: int = DEFAULT_MAX_KEEP,
    max_total_size_mb: int = DEFAULT_MAX_TOTAL_SIZE_MB,
    keep_days: int = 0,
) -> dict[str, Any]:
    """Async wrapper around prune_backups_in (file I/O offloaded to a thread)."""
    return await asyncio.to_thread(
        prune_backups_in,
        data_dir,
        max_keep=max_keep,
        max_total_size_mb=max_total_size_mb,
        keep_days=keep_days,
    )


class BackupManager:
    """Detect version changes, create full data backups, enforce retention."""

    def __init__(self, data_dir: str, config: Any = None) -> None:
        """Create a manager for data_dir.

        Args:
            data_dir: plugin data directory.
            config: plugin config (dict-like). None keeps the legacy defaults.
        """
        self.data_dir = Path(data_dir)
        self.version_file = self.data_dir / _VERSION_FILE
        self.config = config
        self._backup_lock = threading.Lock()

        settings = resolve_backup_settings(config)
        self.settings = settings
        self.enabled: bool = settings["enabled"]
        self.keep_days: int = settings["keep_days"]
        self.max_keep: int = settings["max_keep"]
        self.max_total_size_mb: int = settings["max_total_size_mb"]
        self.skip_if_larger_than_mb: int = settings["skip_if_larger_than_mb"]

    @property
    def backups_dir(self) -> Path:
        """The one directory this manager is ever allowed to delete inside."""
        return self.data_dir / _BACKUPS_DIRNAME

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_stored_version(self) -> str | None:
        """Return the last-known plugin version, or None on first run."""
        if not self.version_file.exists():
            return None
        try:
            return self.version_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def write_current_version(self) -> None:
        """Persist the current plugin version."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.data_dir / f"{_VERSION_FILE}-{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(PLUGIN_VERSION, encoding="utf-8")
            os.replace(temporary, self.version_file)
        finally:
            temporary.unlink(missing_ok=True)

    def needs_backup(self) -> bool:
        """Return True when the plugin version has changed (or is fresh)."""
        stored = self.get_stored_version()
        if stored is None:
            return True  # first install - backup for safety
        return stored != PLUGIN_VERSION

    def backup_if_needed(self) -> str | None:
        """Create a full backup when the version changed.

        Returns the backup directory, or None when nothing was copied (no
        version change, backups disabled, data too large, or disk too full).

        Failed backups preserve the stored version for a later retry. Only
        explicit disabled/size-limit skips record the version without a backup.
        """
        with self._backup_lock:
            try:
                return self._backup_if_needed_locked()
            except Exception:
                logger.error(
                    "[BackupManager] 备份未完成，保留旧版本标记和已有备份，后续启动可重试",
                    exc_info=True,
                )
                return None

    def _backup_if_needed_locked(self) -> str | None:
        if not self.needs_backup():
            return None

        stored = self.get_stored_version()
        old_label = stored or "unknown"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", old_label):
            raise ValueError("备份版本标签包含非法路径字符")
        change_label = f"{old_label} -> {PLUGIN_VERSION}"

        if not self.enabled:
            logger.info(
                f"[BackupManager] 检测到版本变更 ({change_label})，但 "
                f"backup_settings.enabled 为 false，跳过版本变更全量备份；"
                f"已记录当前版本号，下次启动不再重复判定。"
            )
            self.write_current_version()
            return None

        source_files, total_bytes = self._collect_backup_sources()
        total_mb = total_bytes / _MB

        limit_mb = self.skip_if_larger_than_mb
        if limit_mb > 0 and total_bytes > limit_mb * _MB:
            logger.warning(
                f"[BackupManager] 检测到版本变更 ({change_label})，但待备份数据共 "
                f"{total_mb:.1f}MB，超过 backup_settings.skip_if_larger_than_mb "
                f"({limit_mb}MB)，已跳过自动全量备份以避免占满磁盘。"
                f"如需备份请手动复制数据目录 {self.data_dir}（建议先停止 AstrBot），"
                f"或调大该配置项（设为 0 可关闭此保护）。"
            )
            self.write_current_version()
            return None

        free_bytes = self._free_disk_bytes()
        required_bytes = int(total_bytes * 1.1)
        if free_bytes is not None and free_bytes < required_bytes:
            logger.warning(
                f"[BackupManager] 检测到版本变更 ({change_label})，但磁盘剩余空间仅 "
                f"{free_bytes / _MB:.1f}MB，低于备份所需的 "
                f"{required_bytes / _MB:.1f}MB（数据体积 {total_mb:.1f}MB 的 1.1 倍），"
                f"本次未执行备份，保留旧版本号；清理磁盘后下次启动可重试。"
            )
            return None

        self.backups_dir.mkdir(parents=True, exist_ok=True)
        root = self.backups_dir.resolve()
        if not _is_inside(root, self.data_dir.resolve()):
            raise ValueError("备份目录位于数据目录之外")
        staging = self.backups_dir / f".staging-{uuid.uuid4().hex}"
        if not _is_inside(staging.resolve(), root):
            raise ValueError("备份临时目录越出备份范围")
        staging.mkdir()

        logger.info(
            f"[BackupManager] 检测到版本变更 ({change_label})，"
            f"正在创建完整备份 / {total_mb:.1f}MB ..."
        )

        copied_count = 0
        try:
            for file_path in source_files:
                # WAL 内容由 SQLite backup API 合并进独立快照；不能逐文件复制
                # WAL/SHM，否则它们可能来自不同事务时间点。
                if file_path.name.endswith((".db-wal", ".db-shm")):
                    continue
                dest = staging / file_path.name
                if file_path.suffix == ".db":
                    self._backup_sqlite(file_path, dest)
                else:
                    shutil.copy2(file_path, dest)
                copied_count += 1
            info = {
                "plugin_version": PLUGIN_VERSION,
                "previous_version": old_label,
                "backup_timestamp": datetime.now(timezone.utc).isoformat(),
                "backup_unix_time": time.time(),
                "files_copied": copied_count,
                "data_dir": str(self.data_dir),
                "total_bytes": _path_size(staging),
                "source_bytes": total_bytes,
                "skipped": False,
                "skip_reason": None,
            }
            (staging / _BACKUP_INFO_FILE).write_text(
                json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            backup_dir = self.backups_dir / f"v{old_label}"
            # Never overlay an older backup: retries after marker-write failure
            # must preserve the already completed snapshot too.
            while backup_dir.exists() or backup_dir.is_symlink():
                backup_dir = self.backups_dir / f"v{old_label}-{uuid.uuid4().hex[:12]}"
            if not _is_inside(backup_dir.resolve(), root):
                raise ValueError("备份发布路径越出备份目录")
            staging.rename(backup_dir)
            self.write_current_version()
        except Exception:
            if staging.exists():
                _safe_remove(staging, root, {"removed_dirs": [], "removed_files": []})
            raise

        logger.info(f"[BackupManager] 备份完成: {copied_count} 个文件 -> {backup_dir}")

        # Enforce the retention policy so backups/ cannot grow without bound.
        try:
            self.prune_backups()
        except Exception as exc:  # never let cleanup break startup
            logger.warning(f"[BackupManager] 备份清理失败: {exc}", exc_info=True)

        return str(backup_dir)

    async def backup_if_needed_async(self) -> str | None:
        """异步版本：通过 asyncio.to_thread 将同步文件 I/O 卸载到线程池。"""
        return await asyncio.to_thread(self.backup_if_needed)

    def prune_backups(self) -> dict[str, Any]:
        """Apply this manager's retention policy to its backups directory."""
        return prune_backups_in(
            self.data_dir,
            max_keep=self.max_keep,
            max_total_size_mb=self.max_total_size_mb,
            keep_days=self.keep_days,
        )

    async def prune_backups_async(self) -> dict[str, Any]:
        """异步版本：通过 asyncio.to_thread 将同步文件 I/O 卸载到线程池。"""
        return await asyncio.to_thread(self.prune_backups)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _collect_backup_sources(self) -> tuple[list[Path], int]:
        """Return (files matched by _BACKUP_PATTERNS, their total byte size)."""
        files: list[Path] = []
        seen: set[Path] = set()
        total = 0
        for pattern in _BACKUP_PATTERNS:
            matches = sorted(self.data_dir.glob(pattern))
            for file_path in matches:
                if file_path in seen:
                    continue
                if not file_path.is_file():
                    raise OSError(f"备份来源不是普通文件: {file_path}")
                if not _is_inside(file_path.resolve(), self.data_dir.resolve()):
                    raise ValueError(f"备份来源越出数据目录: {file_path}")
                if file_path.name.endswith((".db-wal", ".db-shm")):
                    database = file_path.with_name(file_path.name[:-4])
                    if not database.is_file():
                        raise OSError(f"备份日志缺少主数据库: {file_path}")
                size = file_path.stat().st_size
                seen.add(file_path)
                files.append(file_path)
                total += size
        return files, total

    @staticmethod
    def _backup_sqlite(source: Path, destination: Path) -> None:
        """Create and validate a standalone snapshot, including committed WAL rows."""
        deadline = time.monotonic() + 30.0

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() > deadline:
                raise TimeoutError(f"SQLite 备份超过 30 秒: {source.name}")

        with closing(
            sqlite3.connect(
                source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0
            )
        ) as origin:
            with closing(sqlite3.connect(destination, timeout=30.0)) as target:
                origin.backup(target, pages=256, progress=progress, sleep=0.05)
                if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise sqlite3.DatabaseError(
                        f"SQLite 备份完整性检查失败: {source.name}"
                    )
                target.execute("PRAGMA journal_mode=DELETE")

    def _free_disk_bytes(self) -> int | None:
        """Free bytes on the data_dir filesystem, or None when unknown."""
        candidates = [self.data_dir, *self.data_dir.parents]
        for candidate in candidates:
            try:
                if not candidate.exists():
                    continue
                return shutil.disk_usage(candidate).free
            except OSError:
                continue
        return None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def list_backups(data_dir: str) -> list[dict]:
        """Enumerate existing version backups with their metadata."""
        backups_path = Path(data_dir) / _BACKUPS_DIRNAME
        if not backups_path.exists():
            return []

        result: list[dict] = []
        for backup_dir in sorted(backups_path.iterdir(), reverse=True):
            if (
                not backup_dir.name.startswith("v")
                or not backup_dir.is_dir()
                or not _is_inside(backup_dir.resolve(), backups_path.resolve())
            ):
                continue
            info_path = backup_dir / _BACKUP_INFO_FILE
            info: dict = {}
            if info_path.exists():
                try:
                    info = json.loads(info_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
            info.setdefault("directory", str(backup_dir))
            info.setdefault("name", backup_dir.name)
            files = [p.name for p in backup_dir.iterdir() if p.is_file()]
            info.setdefault("files", files)
            info.setdefault("file_count", len(files))
            info.setdefault("size_bytes", _path_size(backup_dir))
            result.append(info)

        return result


__all__ = [
    "BackupManager",
    "PLUGIN_VERSION",
    "prune_backups_in",
    "prune_backups_in_async",
    "resolve_backup_settings",
]
