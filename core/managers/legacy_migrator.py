"""旧插件数据迁移器：astrbot_plugin_livingmemory → astrbot_plugin_anamnesis。

设计要点
--------
1. **旧目录全程只读**：迁移过程中绝不写入、重命名或删除旧插件数据目录中的任何
   内容。删除旧插件的动作留给用户在确认 `/anam migrate-verify` 通过之后手动执行。
2. **SQLite 安全拷贝**：先拷主库 `.db`，再拷 `-wal`（顺序相反会造成 wal 比主库
   "更旧"，SQLite 会拒绝或截断数据）；`-shm` **不拷**，它是纯共享内存索引，
   SQLite 会依据 wal 自动重建，拷贝陈旧的 -shm 反而可能触发校验失败。
   拷完后在**目标副本**上执行 `PRAGMA wal_checkpoint(TRUNCATE)` 把 wal 落盘。
3. **完整性校验**：每个目标库执行 `PRAGMA quick_check(1)`，普通文件用 blake2b
   摘要比对。任一环节失败 → 回滚（只删除本次新建的路径，绝不动旧目录）。
4. **跳过 backups/**：旧目录里 `backups/` 往往是数据本体的数倍（实测 515MB /
   823MB），迁移它毫无意义且极易撑爆磁盘。
5. **写入 .plugin_version**：迁移成功后立刻写入当前版本号，否则 BackupManager 会
   把 needs_backup() 判为 True 并立即再全量复制一份数据（实测约 290MB）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import logger

from .backup_manager import PLUGIN_VERSION

# 旧插件数据目录可能出现的名字（AstrBot 的 get_data_dir 用插件 id 建目录，
# 不同版本/手动安装可能出现大小写差异）。
LEGACY_PLUGIN_DIR_NAMES: tuple[str, ...] = (
    "astrbot_plugin_livingmemory",
    "astrbot_plugin_LivingMemory",
)

MIGRATION_REPORT_FILE = "legacy_migration_report.json"
_VERSION_FILE = ".plugin_version"

# 需要按 SQLite 规则特殊处理的库：{旧文件名: 新文件名}
SQLITE_FILE_MAP: dict[str, str] = {
    "livingmemory.db": "anamnesis.db",
    "livingmemory_graph_documents.db": "anamnesis_graph_documents.db",
    "conversations.db": "conversations.db",
}

# 普通文件（faiss 索引 / 状态 JSON）：{旧文件名: 新文件名}
PLAIN_FILE_MAP: dict[str, str] = {
    "livingmemory.index": "anamnesis.index",
    "livingmemory_graph.index": "anamnesis_graph.index",
    "decay_state.json": "decay_state.json",
}

# 需要整目录搬运的子目录：{旧目录名: 新目录名}
DIRECTORY_MAP: dict[str, str] = {
    "stopwords": "stopwords",
}

# 明确不迁移的条目（旧目录顶层名）
SKIPPED_ENTRIES: frozenset[str] = frozenset(
    {
        "backups",  # 历史备份，实测可占 62% 空间，迁移无意义
        "__pycache__",
        _VERSION_FILE,  # 由迁移器自己重写为当前版本
        MIGRATION_REPORT_FILE,
    }
)

# 主库文件名（用于判断"目标已被占用"）
_PRIMARY_DB_NAME = SQLITE_FILE_MAP["livingmemory.db"]

# 磁盘余量安全系数：拷贝需要 1.0 倍，checkpoint / 临时文件再留 15%
_DISK_HEADROOM_RATIO = 1.15

_HASH_CHUNK = 1 << 20  # 1MiB


def _human(num_bytes: int) -> str:
    """把字节数格式化成人类可读字符串。"""
    value = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.2f}{unit}"
        value /= 1024.0
    return f"{value:.2f}TB"


def _file_digest(path: Path) -> str:
    """计算文件的 blake2b 摘要（16 字节摘要足够做搬运校验）。"""
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _dir_size(path: Path) -> int:
    """递归统计目录占用（失败的条目按 0 计）。"""
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _entry_size(path: Path) -> int:
    try:
        if path.is_dir():
            return _dir_size(path)
        return path.stat().st_size
    except OSError:
        return 0


@dataclass(slots=True)
class MigrationItem:
    """一个待迁移条目。"""

    kind: str  # "sqlite" | "plain" | "directory"
    source: Path
    target: Path
    size_bytes: int
    sidecars: tuple[Path, ...] = ()  # SQLite 的 -wal（不含 -shm）
    exists_at_target: bool = False

    @property
    def label(self) -> str:
        if self.source.name == self.target.name:
            return self.source.name
        return f"{self.source.name} → {self.target.name}"


@dataclass
class MigrationPlan:
    """迁移计划（只读快照，可安全用于预览）。"""

    data_dir: Path
    legacy_dir: Path | None = None
    items: list[MigrationItem] = field(default_factory=list)
    skipped: list[tuple[str, int]] = field(default_factory=list)
    already_migrated: bool = False
    target_occupied: bool = False
    free_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.items)

    @property
    def skipped_bytes(self) -> int:
        return sum(size for _, size in self.skipped)

    @property
    def required_bytes(self) -> int:
        return int(self.total_bytes * _DISK_HEADROOM_RATIO)

    @property
    def disk_ok(self) -> bool:
        if self.free_bytes <= 0:
            return True  # 拿不到磁盘信息时不阻塞，交给拷贝阶段报错
        return self.free_bytes >= self.required_bytes

    @property
    def conflicts(self) -> list[MigrationItem]:
        return [item for item in self.items if item.exists_at_target]

    @property
    def is_actionable(self) -> bool:
        return bool(self.items)

    def render(self) -> str:
        """渲染中文预览文本。"""
        lines: list[str] = ["【Anamnesis 旧数据迁移预览】"]
        if self.legacy_dir is None:
            lines.append("未找到旧插件（astrbot_plugin_livingmemory）数据目录。")
            lines.append(f"已查找路径: {self.data_dir.parent}")
            return "\n".join(lines)

        lines.append(f"旧目录: {self.legacy_dir}")
        lines.append(f"新目录: {self.data_dir}")
        lines.append("")

        if not self.items:
            lines.append("旧目录中没有可迁移的数据文件。")
        else:
            lines.append(f"待迁移 {len(self.items)} 项，合计 {_human(self.total_bytes)}:")
            for item in self.items:
                kind_cn = {"sqlite": "数据库", "plain": "文件", "directory": "目录"}.get(
                    item.kind, item.kind
                )
                flag = "  [目标已存在]" if item.exists_at_target else ""
                extra = ""
                if item.sidecars:
                    extra = f" (含 {len(item.sidecars)} 个 -wal)"
                lines.append(
                    f"  · [{kind_cn}] {item.label}  {_human(item.size_bytes)}{extra}{flag}"
                )

        if self.skipped:
            lines.append("")
            lines.append(f"跳过 {len(self.skipped)} 项，省下 {_human(self.skipped_bytes)}:")
            for name, size in self.skipped:
                reason = "历史备份，无需迁移" if name == "backups" else "无需迁移"
                lines.append(f"  · {name}  {_human(size)}  ({reason})")

        lines.append("")
        if self.free_bytes > 0:
            lines.append(
                f"磁盘可用 {_human(self.free_bytes)}，需要约 {_human(self.required_bytes)}"
                f"（含 15% 余量）→ {'充足' if self.disk_ok else '不足'}"
            )
        if self.already_migrated:
            lines.append("注意: 已存在迁移报告，说明此前已迁移过一次。")
        if self.target_occupied:
            lines.append(
                f"注意: 新目录已有 {_PRIMARY_DB_NAME}，默认不会覆盖。"
                "确认要覆盖请用 /anam migrate force。"
            )
        if self.conflicts and not self.target_occupied:
            lines.append(f"注意: {len(self.conflicts)} 个目标文件已存在，将被覆盖。")

        lines.append("")
        lines.append("旧目录在整个过程中只读，不会被修改或删除。")
        lines.append("执行迁移: /anam migrate exec")
        return "\n".join(lines)


class LegacyMigrator:
    """把旧插件（LivingMemory）的数据目录整体搬运到新插件（Anamnesis）目录。"""

    def __init__(
        self,
        data_dir: str | Path,
        legacy_names: list[str] | tuple[str, ...] | str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.report_path = self.data_dir / MIGRATION_REPORT_FILE

        if isinstance(legacy_names, str):
            candidates = [legacy_names]
        elif legacy_names:
            candidates = [str(name) for name in legacy_names]
        else:
            candidates = []
        cleaned = [name.strip() for name in candidates if name and name.strip()]
        # 用户配置优先，内置候选兜底；去重且保持顺序
        self.legacy_names: tuple[str, ...] = tuple(
            dict.fromkeys(cleaned + list(LEGACY_PLUGIN_DIR_NAMES))
        )

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------

    def find_legacy_dir(self) -> Path | None:
        """在同级目录下寻找旧插件数据目录。"""
        parent = self.data_dir.parent
        try:
            self_resolved = self.data_dir.resolve()
        except OSError:
            self_resolved = self.data_dir

        for name in self.legacy_names:
            candidate = parent / name
            try:
                if candidate.is_dir() and candidate.resolve() != self_resolved:
                    return candidate
            except OSError:
                continue

        # 大小写不敏感兜底（Linux 上目录名大小写敏感，可能与配置写法不一致）
        lowered = {name.lower() for name in self.legacy_names}
        try:
            entries = sorted(parent.iterdir())
        except OSError:
            return None
        for entry in entries:
            try:
                if not entry.is_dir() or entry.name.lower() not in lowered:
                    continue
                if entry.resolve() != self_resolved:
                    return entry
            except OSError:
                continue
        return None

    def has_legacy_data(self) -> bool:
        """是否存在可迁移的旧数据（用于启动期快速判断，避免构建完整计划）。"""
        legacy = self.find_legacy_dir()
        if legacy is None:
            return False
        for name in (*SQLITE_FILE_MAP, *PLAIN_FILE_MAP):
            path = legacy / name
            try:
                if path.is_file() and path.stat().st_size > 0:
                    return True
            except OSError:
                continue
        return False

    def is_target_populated(self) -> bool:
        """新目录是否已有主库数据。"""
        primary = self.data_dir / _PRIMARY_DB_NAME
        try:
            return primary.is_file() and primary.stat().st_size > 0
        except OSError:
            return False

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------

    def _free_bytes(self) -> int:
        probe = self.data_dir if self.data_dir.exists() else self.data_dir.parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return 0

    def build_plan(self) -> MigrationPlan:
        """构建迁移计划（纯只读操作）。"""
        legacy = self.find_legacy_dir()
        plan = MigrationPlan(
            data_dir=self.data_dir,
            legacy_dir=legacy,
            already_migrated=self.report_path.exists(),
            target_occupied=self.is_target_populated(),
            free_bytes=self._free_bytes(),
        )
        if legacy is None:
            return plan

        recognized: set[str] = set()

        for src_name, dst_name in SQLITE_FILE_MAP.items():
            src = legacy / src_name
            recognized.add(src_name)
            recognized.add(src_name + "-wal")
            recognized.add(src_name + "-shm")
            try:
                if not src.is_file():
                    continue
                size = src.stat().st_size
            except OSError:
                continue
            if size <= 0:
                continue
            sidecars: list[Path] = []
            wal = legacy / (src_name + "-wal")
            try:
                if wal.is_file():
                    sidecars.append(wal)
                    size += wal.stat().st_size
            except OSError:
                pass
            target = self.data_dir / dst_name
            plan.items.append(
                MigrationItem(
                    kind="sqlite",
                    source=src,
                    target=target,
                    size_bytes=size,
                    sidecars=tuple(sidecars),
                    exists_at_target=target.exists(),
                )
            )

        for src_name, dst_name in PLAIN_FILE_MAP.items():
            src = legacy / src_name
            recognized.add(src_name)
            try:
                if not src.is_file():
                    continue
                size = src.stat().st_size
            except OSError:
                continue
            target = self.data_dir / dst_name
            plan.items.append(
                MigrationItem(
                    kind="plain",
                    source=src,
                    target=target,
                    size_bytes=size,
                    exists_at_target=target.exists(),
                )
            )

        for src_name, dst_name in DIRECTORY_MAP.items():
            src = legacy / src_name
            recognized.add(src_name)
            try:
                if not src.is_dir():
                    continue
            except OSError:
                continue
            target = self.data_dir / dst_name
            plan.items.append(
                MigrationItem(
                    kind="directory",
                    source=src,
                    target=target,
                    size_bytes=_dir_size(src),
                    exists_at_target=target.exists(),
                )
            )

        # 记录被跳过的顶层条目（含未识别项，方便用户发现遗漏）
        try:
            for entry in sorted(legacy.iterdir()):
                if entry.name in recognized:
                    continue
                plan.skipped.append((entry.name, _entry_size(entry)))
        except OSError:
            pass

        return plan

    def preview(self) -> str:
        """返回中文预览文本。"""
        return self.build_plan().render()

    async def preview_async(self) -> str:
        return await asyncio.to_thread(self.preview)

    # ------------------------------------------------------------------
    # 拷贝
    # ------------------------------------------------------------------

    @staticmethod
    def _remove_path(path: Path) -> None:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists() or path.is_symlink():
                path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"[LegacyMigrator] 清理路径失败 {path}: {exc}")

    def _rollback(self, created: list[Path]) -> None:
        """只删除本次迁移新建的目标路径，绝不触碰旧目录。"""
        if not created:
            return
        logger.warning(f"[LegacyMigrator] 迁移失败，正在回滚 {len(created)} 个新建路径 ...")
        for path in reversed(created):
            self._remove_path(path)

    def _copy_sqlite(self, item: MigrationItem, created: list[Path]) -> None:
        """安全拷贝 SQLite 库：先主库，再 -wal，最后在目标副本上 checkpoint。"""
        target = item.target
        target_wal = target.with_name(target.name + "-wal")
        target_shm = target.with_name(target.name + "-shm")

        # 目标侧的残留 wal/shm 必须先清掉，否则会和新拷来的主库对不上号
        for stale in (target_wal, target_shm):
            if stale.exists():
                self._remove_path(stale)

        if not target.exists():
            created.append(target)
        shutil.copy2(item.source, target)

        for sidecar in item.sidecars:
            # 仅拷 -wal；-shm 是共享内存索引，SQLite 会依据 wal 自动重建
            if not sidecar.name.endswith("-wal"):
                continue
            if not target_wal.exists():
                created.append(target_wal)
            shutil.copy2(sidecar, target_wal)

        # 在**目标副本**上把 wal 落盘，之后目标库自成一体
        self._checkpoint(target)

    @staticmethod
    def _checkpoint(db_path: Path) -> None:
        conn = None
        try:
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        except sqlite3.Error as exc:
            logger.warning(f"[LegacyMigrator] wal checkpoint 失败 {db_path.name}: {exc}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _copy_plain(self, item: MigrationItem, created: list[Path]) -> None:
        if not item.target.exists():
            created.append(item.target)
        shutil.copy2(item.source, item.target)
        src_digest = _file_digest(item.source)
        dst_digest = _file_digest(item.target)
        if src_digest != dst_digest:
            raise OSError(
                f"文件校验不一致: {item.source.name} ({src_digest} != {dst_digest})"
            )

    def _copy_directory(self, item: MigrationItem, created: list[Path]) -> None:
        if not item.target.exists():
            created.append(item.target)
        shutil.copytree(item.source, item.target, dirs_exist_ok=True)

    @staticmethod
    def _quick_check(db_path: Path) -> str | None:
        """返回 None 表示健康，否则返回错误描述。"""
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
            row = conn.execute("PRAGMA quick_check(1)").fetchone()
        except sqlite3.Error as exc:
            return str(exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        if not row:
            return "quick_check 未返回结果"
        value = str(row[0]).strip().lower()
        return None if value == "ok" else str(row[0])

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def migrate(self, force: bool = False) -> dict:
        """执行迁移。返回结构化结果字典。"""
        started = time.time()
        plan = self.build_plan()
        result: dict = {
            "ok": False,
            "reason": "",
            "message": "",
            "plan": plan,
            "migrated": [],
            "bytes": 0,
            "elapsed": 0.0,
            "report_path": None,
        }

        reason = ""
        if plan.legacy_dir is None:
            reason = "legacy_not_found"
        elif not plan.items:
            reason = "nothing_to_migrate"
        elif plan.already_migrated and not force:
            reason = "already_migrated"
        elif plan.target_occupied and not force:
            reason = "target_occupied"
        elif not plan.disk_ok:
            reason = "insufficient_disk"

        if reason:
            result["reason"] = reason
            result["message"] = _REASON_TEXT.get(reason, reason)
            return result

        self.data_dir.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        migrated: list[str] = []

        logger.info(
            f"[LegacyMigrator] 开始迁移旧数据: {plan.legacy_dir} → {self.data_dir} "
            f"({len(plan.items)} 项 / {_human(plan.total_bytes)})"
        )

        try:
            for item in plan.items:
                if item.kind == "sqlite":
                    self._copy_sqlite(item, created)
                elif item.kind == "directory":
                    self._copy_directory(item, created)
                else:
                    self._copy_plain(item, created)
                migrated.append(item.label)
                logger.info(
                    f"[LegacyMigrator] 已迁移 {item.label} ({_human(item.size_bytes)})"
                )
        except (OSError, shutil.Error) as exc:
            logger.error(f"[LegacyMigrator] 拷贝失败: {exc}")
            self._rollback(created)
            result["reason"] = "copy_failed"
            result["message"] = f"{_REASON_TEXT['copy_failed']}: {exc}"
            return result

        # 完整性校验
        failures: list[str] = []
        for item in plan.items:
            if item.kind != "sqlite":
                continue
            problem = self._quick_check(item.target)
            if problem:
                failures.append(f"{item.target.name}: {problem}")

        if failures:
            logger.error(f"[LegacyMigrator] 完整性校验失败: {failures}")
            self._rollback(created)
            result["reason"] = "integrity_failed"
            result["message"] = (
                f"{_REASON_TEXT['integrity_failed']}: " + "; ".join(failures)
            )
            return result

        # 关键：写入版本文件，避免 BackupManager 立即再全量复制一份数据
        self._write_version_file()

        elapsed = time.time() - started
        report = {
            "schema": 1,
            "plugin_version": PLUGIN_VERSION,
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "legacy_dir": str(plan.legacy_dir),
            "data_dir": str(self.data_dir),
            "forced": bool(force),
            "items": [
                {
                    "kind": item.kind,
                    "source": item.source.name,
                    "target": item.target.name,
                    "size_bytes": item.size_bytes,
                    "sidecars": [p.name for p in item.sidecars],
                }
                for item in plan.items
            ],
            "skipped": [
                {"name": name, "size_bytes": size} for name, size in plan.skipped
            ],
            "total_bytes": plan.total_bytes,
            "skipped_bytes": plan.skipped_bytes,
            "elapsed_seconds": round(elapsed, 3),
        }
        try:
            self.report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result["report_path"] = str(self.report_path)
        except OSError as exc:
            logger.warning(f"[LegacyMigrator] 写入迁移报告失败: {exc}")

        result.update(
            {
                "ok": True,
                "reason": "migrated",
                "message": (
                    f"迁移完成: {len(migrated)} 项 / {_human(plan.total_bytes)}，"
                    f"耗时 {elapsed:.1f}s；已跳过 {_human(plan.skipped_bytes)} 历史备份。"
                ),
                "migrated": migrated,
                "bytes": plan.total_bytes,
                "elapsed": elapsed,
            }
        )
        logger.info(f"[LegacyMigrator] {result['message']}")
        return result

    async def migrate_async(self, force: bool = False) -> dict:
        return await asyncio.to_thread(self.migrate, force)

    def _write_version_file(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / _VERSION_FILE).write_text(PLUGIN_VERSION, encoding="utf-8")
        except OSError as exc:
            logger.warning(f"[LegacyMigrator] 写入 {_VERSION_FILE} 失败: {exc}")

    # ------------------------------------------------------------------
    # 校验（迁移后对账）
    # ------------------------------------------------------------------

    @staticmethod
    def _open_ro(db_path: Path, *, readonly_only: bool = False) -> sqlite3.Connection:
        """以只读方式打开数据库。

        `readonly_only=True` 用于**旧库**：只读 URI 打开失败时直接抛错，绝不回退到
        可写连接——可写连接一旦建立，SQLite 关闭时会自动 checkpoint 并改写旧库文件，
        破坏"旧目录全程只读"的承诺。新库允许回退。
        """
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
            # mode=ro 打开 WAL 库时，真正的报错往往发生在第一次查询而非 connect
            conn.execute("SELECT 1").fetchone()
            return conn
        except sqlite3.Error:
            if readonly_only:
                raise
            return sqlite3.connect(str(db_path), timeout=30.0)

    @classmethod
    def _table_counts(
        cls, db_path: Path, *, readonly_only: bool = False
    ) -> tuple[dict[str, int], list[str]]:
        """返回 ({表名: 行数}, 读不到的表名列表)。"""
        counts: dict[str, int] = {}
        unreadable: list[str] = []
        # 打开失败要往上抛（readonly_only 时调用方需要据此降级），
        # 因此不能被下面 "统计失败" 的 except 吞掉。
        conn = cls._open_ro(db_path, readonly_only=readonly_only)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name"
            ).fetchall()
            for (name,) in rows:
                try:
                    quoted = name.replace('"', '""')
                    value = conn.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()
                    counts[name] = int(value[0]) if value else 0
                except sqlite3.Error:
                    # FTS5 影子表 / contentless 表可能不支持 COUNT(*)，不算失败
                    unreadable.append(name)
        except sqlite3.Error as exc:
            unreadable.append(f"<enumerate failed: {exc}>")
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        return counts, unreadable

    def verify(self) -> dict:
        """对账旧库与新库的表行数 + 普通文件摘要。"""
        legacy = self.find_legacy_dir()
        report: dict = {
            "ok": False,
            "legacy_dir": str(legacy) if legacy else None,
            "data_dir": str(self.data_dir),
            "databases": [],
            "files": [],
            "problems": [],
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        if legacy is None:
            report["problems"].append("未找到旧插件数据目录，无法对账。")
            report["message"] = self.render_verify(report)
            return report

        for src_name, dst_name in SQLITE_FILE_MAP.items():
            src = legacy / src_name
            dst = self.data_dir / dst_name
            if not src.is_file():
                continue
            entry: dict = {
                "legacy": src_name,
                "current": dst_name,
                "tables": [],
                "unreadable_tables": [],
                "matched": False,
            }
            if not dst.is_file():
                entry["error"] = "新库不存在"
                report["problems"].append(f"{dst_name}: 新库不存在")
                report["databases"].append(entry)
                continue

            try:
                old_counts, old_bad = self._table_counts(src, readonly_only=True)
            except sqlite3.Error as exc:
                # 旧库处于 WAL 状态且无法只读打开（通常是上次非正常退出留下了 -wal
                # 但没有 -shm）。此时宁可放弃行数对账，也不能用可写连接去改旧库。
                entry["legacy_readable"] = False
                entry["note"] = (
                    f"旧库无法以只读方式打开（{exc}），已跳过行数对账；"
                    "迁移时的 quick_check 与文件摘要校验仍然有效。"
                )
                report["databases"].append(entry)
                continue
            entry["legacy_readable"] = True
            new_counts, new_bad = self._table_counts(dst)
            entry["unreadable_tables"] = sorted(set(old_bad) | set(new_bad))

            mismatches = 0
            for table in sorted(set(old_counts) | set(new_counts)):
                old_n = old_counts.get(table)
                new_n = new_counts.get(table)
                same = old_n == new_n
                if not same:
                    mismatches += 1
                entry["tables"].append(
                    {"table": table, "legacy": old_n, "current": new_n, "match": same}
                )
            entry["matched"] = mismatches == 0
            if mismatches:
                report["problems"].append(f"{dst_name}: {mismatches} 张表行数不一致")
            report["databases"].append(entry)

        for src_name, dst_name in PLAIN_FILE_MAP.items():
            src = legacy / src_name
            dst = self.data_dir / dst_name
            if not src.is_file():
                continue
            entry = {"legacy": src_name, "current": dst_name, "match": False}
            if not dst.is_file():
                entry["error"] = "新文件不存在"
                report["problems"].append(f"{dst_name}: 新文件不存在")
                report["files"].append(entry)
                continue
            try:
                entry["match"] = _file_digest(src) == _file_digest(dst)
            except OSError as exc:
                entry["error"] = str(exc)
            if not entry["match"] and "error" not in entry:
                report["problems"].append(f"{dst_name}: 摘要不一致")
            report["files"].append(entry)

        report["ok"] = not report["problems"]
        report["message"] = self.render_verify(report)
        return report

    async def verify_async(self) -> dict:
        return await asyncio.to_thread(self.verify)

    @staticmethod
    def render_verify(report: dict) -> str:
        """把 verify() 结果渲染成中文文本。"""
        lines: list[str] = ["【Anamnesis 迁移对账报告】"]
        if not report.get("legacy_dir"):
            lines.append("未找到旧插件数据目录 —— 若已删除旧插件，这是正常的。")
            return "\n".join(lines)

        lines.append(f"旧目录: {report['legacy_dir']}")
        lines.append(f"新目录: {report['data_dir']}")

        for db in report.get("databases", []):
            lines.append("")
            head = f"◆ {db['legacy']} → {db['current']}"
            if db.get("error"):
                lines.append(f"{head}  ✗ {db['error']}")
                continue
            if db.get("legacy_readable") is False:
                lines.append(f"{head}  ⚠ 跳过对账")
                lines.append(f"    · {db.get('note', '旧库不可读')}")
                continue
            lines.append(f"{head}  {'✓ 一致' if db['matched'] else '✗ 存在差异'}")
            shown = 0
            for row in db.get("tables", []):
                if row["match"] and shown >= 12:
                    continue
                mark = "✓" if row["match"] else "✗"
                lines.append(
                    f"    {mark} {row['table']}: {row['legacy']} → {row['current']}"
                )
                shown += 1
            hidden = len(db.get("tables", [])) - shown
            if hidden > 0:
                lines.append(f"    ... 另有 {hidden} 张表一致（已折叠）")
            if db.get("unreadable_tables"):
                lines.append(
                    f"    · 无法统计的表（FTS 影子表等，不影响数据）: "
                    f"{len(db['unreadable_tables'])} 张"
                )

        if report.get("files"):
            lines.append("")
            lines.append("◆ 索引 / 状态文件")
            for item in report["files"]:
                if item.get("error"):
                    lines.append(f"    ✗ {item['current']}: {item['error']}")
                else:
                    mark = "✓" if item["match"] else "✗"
                    lines.append(f"    {mark} {item['current']}")

        lines.append("")
        if report.get("ok"):
            lines.append("结论: 全部一致，数据已完整迁移。")
            lines.append("现在可以安全删除旧插件及其数据目录了。")
        else:
            lines.append(f"结论: 发现 {len(report['problems'])} 处问题：")
            for problem in report["problems"]:
                lines.append(f"    · {problem}")
            lines.append("请勿删除旧插件数据，先排查上述问题。")
        return "\n".join(lines)


_REASON_TEXT: dict[str, str] = {
    "legacy_not_found": "未找到旧插件（astrbot_plugin_livingmemory）的数据目录，无需迁移。",
    "nothing_to_migrate": "旧插件目录存在，但里面没有可迁移的数据文件。",
    "already_migrated": (
        "此前已完成过迁移（存在 legacy_migration_report.json）。"
        "如需重新迁移请使用 /anam migrate force。"
    ),
    "target_occupied": (
        f"新插件目录已存在 {_PRIMARY_DB_NAME}，为避免覆盖现有记忆已中止。"
        "确认要覆盖请使用 /anam migrate force。"
    ),
    "insufficient_disk": "磁盘可用空间不足（需要数据量的 1.15 倍），已中止。",
    "copy_failed": "拷贝过程出错，已回滚本次新建的文件",
    "integrity_failed": "拷贝后的数据库完整性校验失败，已回滚",
}


__all__ = [
    "DIRECTORY_MAP",
    "LEGACY_PLUGIN_DIR_NAMES",
    "MIGRATION_REPORT_FILE",
    "PLAIN_FILE_MAP",
    "SQLITE_FILE_MAP",
    "LegacyMigrator",
    "MigrationItem",
    "MigrationPlan",
]
