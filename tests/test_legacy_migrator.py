"""旧数据迁移器（LivingMemory → Anamnesis）测试。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from astrbot_plugin_anamnesis.core.managers.legacy_migrator import (
    MIGRATION_REPORT_FILE,
    LegacyMigrator,
)


def _make_legacy(root: Path, *, with_wal: bool = True, rows: int = 120) -> Path:
    """造一个仿真的旧插件数据目录。"""
    legacy = root / "astrbot_plugin_livingmemory"
    legacy.mkdir(parents=True, exist_ok=True)

    db = legacy / "livingmemory.db"
    conn = sqlite3.connect(db)
    if with_wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, text TEXT)")
    conn.executemany(
        "INSERT INTO documents(text) VALUES(?)", [(f"mem-{i}",) for i in range(rows)]
    )
    conn.execute(
        "CREATE VIRTUAL TABLE livingmemory_memories_fts "
        "USING fts5(content, tokenize='unicode61')"
    )
    conn.execute("INSERT INTO livingmemory_memories_fts(content) VALUES('hello')")
    conn.commit()
    conn.close()

    graph = sqlite3.connect(legacy / "livingmemory_graph_documents.db")
    graph.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY)")
    graph.executemany("INSERT INTO documents(id) VALUES(?)", [(i,) for i in range(37)])
    graph.commit()
    graph.close()

    conv = sqlite3.connect(legacy / "conversations.db")
    conv.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY)")
    conv.executemany("INSERT INTO messages(id) VALUES(?)", [(i,) for i in range(9)])
    conv.commit()
    conv.close()

    (legacy / "livingmemory.index").write_bytes(os.urandom(4096))
    (legacy / "livingmemory_graph.index").write_bytes(os.urandom(2048))
    (legacy / "decay_state.json").write_text(
        json.dumps({"last_decay_date": "2026-09-05"}), encoding="utf-8"
    )
    (legacy / ".plugin_version").write_text("2.6.0", encoding="utf-8")

    stopwords = legacy / "stopwords"
    stopwords.mkdir(exist_ok=True)
    (stopwords / "zh.txt").write_text("的\n了\n", encoding="utf-8")

    backups = legacy / "backups" / "v2.6.0"
    backups.mkdir(parents=True, exist_ok=True)
    (backups / "old.db").write_bytes(b"x" * 8192)

    return legacy


@pytest.fixture()
def env(tmp_path: Path):
    """旧目录 + 新目录 + migrator。

    额外持有一个 open 连接并关闭 wal 自动 checkpoint，让旧库保留一个**非空 -wal**，
    以此复现真实服务器上的状态（实测 livingmemory.db-wal = 4.5MB）。
    Python 的 sqlite3 在最后一个连接 close() 时会 checkpoint 并删除 -wal，
    所以必须把连接留到测试结束。
    """
    legacy = _make_legacy(tmp_path)
    holder = sqlite3.connect(legacy / "livingmemory.db")
    holder.execute("PRAGMA wal_autocheckpoint=0")
    holder.execute("INSERT INTO documents(text) VALUES('wal-resident')")
    holder.commit()

    data_dir = tmp_path / "astrbot_plugin_anamnesis"
    data_dir.mkdir()
    try:
        yield legacy, data_dir, LegacyMigrator(data_dir)
    finally:
        holder.close()


def test_find_legacy_dir(env):
    legacy, _data_dir, migrator = env
    assert migrator.find_legacy_dir() == legacy
    assert migrator.has_legacy_data() is True
    assert migrator.is_target_populated() is False


def test_find_legacy_dir_absent(tmp_path: Path):
    data_dir = tmp_path / "astrbot_plugin_anamnesis"
    data_dir.mkdir()
    migrator = LegacyMigrator(data_dir)
    assert migrator.find_legacy_dir() is None
    assert migrator.has_legacy_data() is False
    assert migrator.migrate()["reason"] == "legacy_not_found"


def test_custom_legacy_name_is_preferred(tmp_path: Path):
    custom = tmp_path / "my_old_plugin"
    custom.mkdir()
    (custom / "livingmemory.db").write_bytes(b"")
    data_dir = tmp_path / "astrbot_plugin_anamnesis"
    data_dir.mkdir()
    migrator = LegacyMigrator(data_dir, legacy_names=["my_old_plugin"])
    assert migrator.legacy_names[0] == "my_old_plugin"
    assert migrator.find_legacy_dir() == custom


def test_plan_skips_backups_directory(env):
    _legacy, _data_dir, migrator = env
    plan = migrator.build_plan()
    names = {name for name, _size in plan.skipped}
    assert "backups" in names
    assert ".plugin_version" in names
    assert plan.skipped_bytes > 0
    # backups 不应出现在迁移项里
    assert all(item.source.name != "backups" for item in plan.items)
    assert all(item.target.name != "backups" for item in plan.items)


def test_preview_is_readonly_and_chinese(env):
    legacy, _data_dir, migrator = env
    before = sorted(p.name for p in legacy.rglob("*"))
    text = migrator.preview()
    after = sorted(p.name for p in legacy.rglob("*"))
    assert before == after
    assert "旧数据迁移预览" in text
    assert "只读" in text
    assert "/anam migrate exec" in text


def test_migrate_copies_everything_and_leaves_legacy_untouched(env):
    legacy, data_dir, migrator = env
    wal = legacy / "livingmemory.db-wal"
    assert wal.exists() and wal.stat().st_size > 0, "前置条件: 旧库应有未 checkpoint 的 wal"

    legacy_before = {
        str(p.relative_to(legacy)): (p.stat().st_size if p.is_file() else -1)
        for p in sorted(legacy.rglob("*"))
    }

    result = migrator.migrate()
    assert result["ok"] is True, result["message"]
    assert result["reason"] == "migrated"

    legacy_after = {
        str(p.relative_to(legacy)): (p.stat().st_size if p.is_file() else -1)
        for p in sorted(legacy.rglob("*"))
    }
    assert legacy_before == legacy_after, "旧目录必须保持只读"

    for name in (
        "anamnesis.db",
        "anamnesis_graph_documents.db",
        "conversations.db",
        "anamnesis.index",
        "anamnesis_graph.index",
        "decay_state.json",
    ):
        assert (data_dir / name).is_file(), name
    assert (data_dir / "stopwords" / "zh.txt").is_file()
    assert not (data_dir / "backups").exists()


def test_wal_content_survives_migration(env):
    """只存在于 -wal 里、还没落到主库的那条记录必须一起迁移过来。"""
    _legacy, data_dir, migrator = env
    assert migrator.migrate()["ok"] is True
    conn = sqlite3.connect(data_dir / "anamnesis.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 121
        assert conn.execute(
            "SELECT COUNT(*) FROM documents WHERE text = 'wal-resident'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_target_wal_is_checkpointed(env):
    """迁移后目标库应自成一体：-wal 已 TRUNCATE。"""
    _legacy, data_dir, migrator = env
    assert migrator.migrate()["ok"] is True
    target_wal = data_dir / "anamnesis.db-wal"
    assert not target_wal.exists() or target_wal.stat().st_size == 0


def test_shm_is_not_copied(env):
    """-shm 是共享内存索引，必须由 SQLite 自行重建，不能照搬旧文件。"""
    legacy, data_dir, migrator = env
    legacy_shm = legacy / "livingmemory.db-shm"
    assert legacy_shm.exists(), "前置条件: 活动连接应已创建 -shm"

    copied: list[str] = []
    original = shutil.copy2

    def spy(src, dst, *args, **kwargs):
        copied.append(str(src))
        return original(src, dst, *args, **kwargs)

    import astrbot_plugin_anamnesis.core.managers.legacy_migrator as mod

    mod.shutil.copy2 = spy
    try:
        assert migrator.migrate()["ok"] is True
    finally:
        mod.shutil.copy2 = original

    assert not any(name.endswith("-shm") for name in copied), copied
    assert any(name.endswith("livingmemory.db") for name in copied)
    assert any(name.endswith("livingmemory.db-wal") for name in copied)


def test_version_file_written_to_prevent_double_backup(env):
    _legacy, data_dir, migrator = env
    from astrbot_plugin_anamnesis.core.managers.backup_manager import (
        PLUGIN_VERSION,
        BackupManager,
    )

    assert migrator.migrate()["ok"] is True
    assert (data_dir / ".plugin_version").read_text(encoding="utf-8") == PLUGIN_VERSION
    assert BackupManager(str(data_dir)).needs_backup() is False


def test_report_is_written(env):
    _legacy, data_dir, migrator = env
    assert migrator.migrate()["ok"] is True
    report_path = data_dir / MIGRATION_REPORT_FILE
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema"] == 1
    assert report["total_bytes"] > 0
    assert report["skipped_bytes"] > 0
    assert {item["target"] for item in report["items"]} >= {"anamnesis.db"}


def test_second_migrate_refuses_without_force(env):
    _legacy, _data_dir, migrator = env
    assert migrator.migrate()["ok"] is True
    again = migrator.migrate()
    assert again["ok"] is False
    assert again["reason"] == "already_migrated"
    assert "force" in again["message"]


def test_force_overwrites(env):
    _legacy, _data_dir, migrator = env
    assert migrator.migrate()["ok"] is True
    forced = migrator.migrate(force=True)
    assert forced["ok"] is True, forced["message"]


def test_target_occupied_refuses(env):
    _legacy, data_dir, migrator = env
    (data_dir / "anamnesis.db").write_bytes(b"existing-memory-db")
    result = migrator.migrate()
    assert result["reason"] == "target_occupied"
    # 目标文件必须原样保留
    assert (data_dir / "anamnesis.db").read_bytes() == b"existing-memory-db"


def test_nothing_to_migrate(tmp_path: Path):
    legacy = tmp_path / "astrbot_plugin_livingmemory"
    legacy.mkdir()
    (legacy / "README.txt").write_text("hi", encoding="utf-8")
    data_dir = tmp_path / "astrbot_plugin_anamnesis"
    data_dir.mkdir()
    assert LegacyMigrator(data_dir).migrate()["reason"] == "nothing_to_migrate"


def test_integrity_failure_rolls_back(env, monkeypatch):
    _legacy, data_dir, migrator = env
    monkeypatch.setattr(
        LegacyMigrator, "_quick_check", staticmethod(lambda path: "database disk image is malformed")
    )
    result = migrator.migrate()
    assert result["ok"] is False
    assert result["reason"] == "integrity_failed"
    # 回滚后目标目录不应残留任何本次新建的文件
    leftovers = [p.name for p in data_dir.iterdir()]
    assert "anamnesis.db" not in leftovers
    assert MIGRATION_REPORT_FILE not in leftovers


def test_copy_failure_rolls_back(env, monkeypatch):
    _legacy, data_dir, migrator = env
    import shutil as _shutil

    real_copy = _shutil.copy2
    calls = {"n": 0}

    def flaky(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise OSError("disk full (simulated)")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(
        "astrbot_plugin_anamnesis.core.managers.legacy_migrator.shutil.copy2", flaky
    )
    result = migrator.migrate()
    assert result["ok"] is False
    assert result["reason"] == "copy_failed"
    assert not (data_dir / MIGRATION_REPORT_FILE).exists()


def test_insufficient_disk_refuses(env, monkeypatch):
    _legacy, _data_dir, migrator = env
    monkeypatch.setattr(LegacyMigrator, "_free_bytes", lambda self: 1024)
    result = migrator.migrate()
    assert result["reason"] == "insufficient_disk"


def test_verify_matches_after_migration(env):
    _legacy, _data_dir, migrator = env
    assert migrator.migrate()["ok"] is True
    report = migrator.verify()
    assert report["ok"] is True, report["message"]
    assert not report["problems"]
    assert "全部一致" in report["message"]
    tables = {
        row["table"]: row
        for db in report["databases"]
        if db["current"] == "anamnesis.db"
        for row in db["tables"]
    }
    assert tables["documents"]["legacy"] == tables["documents"]["current"] == 121


def test_verify_detects_row_mismatch(env):
    _legacy, data_dir, migrator = env
    assert migrator.migrate()["ok"] is True
    conn = sqlite3.connect(data_dir / "anamnesis.db")
    try:
        conn.execute("DELETE FROM documents WHERE id <= 10")
        conn.commit()
    finally:
        conn.close()
    report = migrator.verify()
    assert report["ok"] is False
    assert any("anamnesis.db" in problem for problem in report["problems"])
    assert "请勿删除旧插件数据" in report["message"]


def test_verify_without_legacy_dir(tmp_path: Path):
    data_dir = tmp_path / "astrbot_plugin_anamnesis"
    data_dir.mkdir()
    report = LegacyMigrator(data_dir).verify()
    assert report["ok"] is False
    assert "未找到旧插件数据目录" in report["message"]


@pytest.mark.asyncio
async def test_async_wrappers(env):
    _legacy, _data_dir, migrator = env
    assert "旧数据迁移预览" in await migrator.preview_async()
    assert (await migrator.migrate_async())["ok"] is True
    assert (await migrator.verify_async())["ok"] is True


def test_verify_never_opens_legacy_writable(env, monkeypatch):
    """verify() 绝不能用可写连接打开旧库（会触发 checkpoint 改写旧文件）。"""
    _legacy, _data_dir, migrator = env
    assert migrator.migrate()["ok"] is True

    real_connect = sqlite3.connect
    opened: list[tuple[str, bool]] = []

    def spy(target, *args, **kwargs):
        opened.append((str(target), bool(kwargs.get("uri"))))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(
        "astrbot_plugin_anamnesis.core.managers.legacy_migrator.sqlite3.connect", spy
    )
    migrator.verify()

    for target, is_uri in opened:
        if "astrbot_plugin_livingmemory" in target:
            assert is_uri and "mode=ro" in target, f"旧库被非只读方式打开: {target}"


def test_verify_skips_accounting_when_legacy_unreadable(env, monkeypatch):
    """旧库无法只读打开时应优雅降级，而不是回退成可写连接。"""
    _legacy, _data_dir, migrator = env
    assert migrator.migrate()["ok"] is True

    real_open = LegacyMigrator._open_ro

    def picky(db_path, *, readonly_only: bool = False):
        if readonly_only:
            raise sqlite3.OperationalError("unable to open database file")
        return real_open(db_path, readonly_only=readonly_only)

    monkeypatch.setattr(LegacyMigrator, "_open_ro", staticmethod(picky))
    report = migrator.verify()
    assert all(db.get("legacy_readable") is False for db in report["databases"])
    assert "跳过对账" in report["message"]


def test_legacy_dir_bytes_unchanged_by_verify(env):
    """再加一道保险：verify() 前后旧目录逐文件字节数不变。"""
    legacy, _data_dir, migrator = env
    assert migrator.migrate()["ok"] is True
    snapshot = {
        str(p.relative_to(legacy)): p.stat().st_size
        for p in sorted(legacy.rglob("*"))
        if p.is_file()
    }
    migrator.verify()
    after = {
        str(p.relative_to(legacy)): p.stat().st_size
        for p in sorted(legacy.rglob("*"))
        if p.is_file()
    }
    assert snapshot == after
