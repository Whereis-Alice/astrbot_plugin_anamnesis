"""
记忆重要性衰减调度器
每日自动对记忆重要性进行衰减处理，并定期备份数据库

备份保留策略（份数上限 / 总体积上限 / 保留天数）统一复用
core.managers.backup_manager 的实现，避免两处删除逻辑各写一份。
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

from ..managers.backup_manager import prune_backups_in_async

if TYPE_CHECKING:
    from ...storage.db_migration import DBMigration
    from ..managers.memory_engine import MemoryEngine


class DecayScheduler:
    """
    记忆重要性衰减调度器

    功能：
    1. 每日凌晨自动执行衰减
    2. 启动时检查并补偿错过的衰减
    3. 防止同一天重复执行
    4. 定期自动备份数据库
    """

    def __init__(
        self,
        memory_engine: "MemoryEngine",
        decay_rate: float,
        data_dir: str,
        check_hour: int = 0,
        check_minute: int = 5,
        db_migration: "DBMigration | None" = None,
        backup_enabled: bool = True,
        backup_keep_days: int = 7,
        consolidation_manager=None,
        backup_max_keep: int = 2,
        backup_max_total_size_mb: int = 1024,
        backup_skip_if_larger_than_mb: int = 512,
    ):
        """
        初始化衰减调度器

        Args:
            memory_engine: 记忆引擎实例
            decay_rate: 每日衰减率 (0-1)
            data_dir: 数据目录，用于存储状态文件
            check_hour: 每日执行时间（小时）
            check_minute: 每日执行时间（分钟）
            db_migration: 数据库迁移管理器（用于备份）
            backup_enabled: 是否启用每日自动备份
            backup_keep_days: 备份保留天数，超期自动删除
            consolidation_manager: 记忆整合管理器（用于每日整合触发）
            backup_max_keep: backups/ 下保留的备份份数上限（版本目录与每日备份
                文件各自独立计数），<=0 表示不限
            backup_max_total_size_mb: backups/ 目录总体积上限（MB），超限从最旧
                的备份开始删除，<=0 表示不限
            backup_skip_if_larger_than_mb: 主库体积超过该值时跳过每日全量备份，
                <=0 表示不限
        """
        self.memory_engine = memory_engine
        self.decay_rate = decay_rate
        self.data_dir = Path(data_dir)
        self.check_hour = check_hour
        self.check_minute = check_minute
        self.db_migration = db_migration
        self.backup_enabled = backup_enabled
        self.backup_keep_days = backup_keep_days
        self.backup_max_keep = backup_max_keep
        self.backup_max_total_size_mb = backup_max_total_size_mb
        self.backup_skip_if_larger_than_mb = backup_skip_if_larger_than_mb
        self.consolidation_manager = consolidation_manager

        self._state_file = self.data_dir / "decay_state.json"
        self._task: asyncio.Task | None = None
        self._running = False

    async def _load_state(self) -> dict:
        """加载状态文件"""
        if not self._state_file.exists():
            return {}
        try:
            try:
                import aiofiles
            except ImportError:
                content = await asyncio.to_thread(
                    self._state_file.read_text,
                    encoding="utf-8",
                )
            else:
                async with aiofiles.open(self._state_file, encoding="utf-8") as f:
                    content = await f.read()
            return json.loads(content)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[衰减调度] 加载状态文件失败: {e}")
            return {}

    async def _save_state(self, state: dict) -> None:
        """保存状态文件"""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            content = json.dumps(state, ensure_ascii=False)
            try:
                import aiofiles
            except ImportError:
                await asyncio.to_thread(
                    self._state_file.write_text,
                    content,
                    encoding="utf-8",
                )
            else:
                async with aiofiles.open(self._state_file, "w", encoding="utf-8") as f:
                    await f.write(content)
        except OSError as e:
            logger.error(f"[衰减调度] 保存状态文件失败: {e}")

    async def _get_last_decay_date(self) -> str | None:
        """获取上次衰减日期 (格式: YYYY-MM-DD)"""
        state = await self._load_state()
        return state.get("last_decay_date")

    async def _set_last_decay_date(self, date_str: str) -> None:
        """设置上次衰减日期"""
        state = await self._load_state()
        state["last_decay_date"] = date_str
        state["last_decay_timestamp"] = time.time()
        await self._save_state(state)

    def _get_today_str(self) -> str:
        """获取今天日期字符串"""
        return datetime.now().strftime("%Y-%m-%d")

    async def _calculate_missed_days(self) -> int:
        """计算错过的衰减天数"""
        last_date_str = await self._get_last_decay_date()
        if not last_date_str:
            return 0

        try:
            last_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
            today = datetime.now().date()
            delta = (today - last_date).days
            return max(0, delta - 1)
        except ValueError:
            return 0

    async def _execute_decay(self, days: int = 1) -> bool:
        """
        执行衰减操作

        Args:
            days: 衰减天数（用于补偿错过的天数）

        Returns:
            是否执行成功
        """
        try:
            if self.decay_rate > 0:
                affected = await self.memory_engine.apply_daily_decay(
                    self.decay_rate, days
                )
                logger.info(
                    f"[衰减调度] 衰减完成，影响 {affected} 条记忆，衰减天数: {days}"
                )
            else:
                logger.info("[衰减调度] 衰减率为0，跳过衰减")

            # 每日衰减后可选执行一次旧记忆清理
            if self.memory_engine.config.get("auto_cleanup_enabled", True):
                try:
                    cleanup_days = self.memory_engine.config.get(
                        "cleanup_days_threshold", 30
                    )
                    cleanup_importance = self.memory_engine.config.get(
                        "cleanup_importance_threshold", 0.3
                    )
                    deleted = await self.memory_engine.cleanup_old_memories(
                        days_threshold=cleanup_days,
                        importance_threshold=cleanup_importance,
                    )
                    action = (
                        "归档"
                        if self.memory_engine.config.get(
                            "auto_archived_enabled", False
                        )
                        else "删除"
                    )
                    logger.info(
                        f"[衰减调度] 自动清理完成，{action} {deleted} 条旧记忆"
                    )
                except Exception as cleanup_err:
                    logger.error(
                        f"[衰减调度] 自动清理失败: {cleanup_err}", exc_info=True
                    )

            await self._set_last_decay_date(self._get_today_str())

            # 每日执行记忆整合
            if self.consolidation_manager is not None:
                try:
                    result = await self.consolidation_manager.maybe_run("daily")
                    if not result.get("skipped"):
                        logger.info(
                            f"[衰减调度] 记忆整合完成: 候选={result.get('candidates', 0)}, "
                            f"组数={result.get('groups', 0)}, "
                            f"合并={result.get('merged', 0)}"
                        )
                except Exception as consolidation_err:
                    logger.error(
                        f"[衰减调度] 记忆整合失败: {consolidation_err}", exc_info=True
                    )

            # 每日执行备份
            if self.backup_enabled and self.db_migration:
                await self._run_backup()

            try:
                maintenance_result = await self.memory_engine.maintain_storage()
                if maintenance_result.get("success"):
                    reclaimed = int(maintenance_result.get("bytes_reclaimed", 0))
                    logger.info(
                        f"[衰减调度] 存储维护完成，释放 {reclaimed / 1024 / 1024:.2f} MB"
                    )
                else:
                    logger.warning(
                        f"[衰减调度] 存储维护失败: {maintenance_result.get('error')}"
                    )
            except Exception as maintenance_err:
                logger.warning(
                    f"[衰减调度] 存储维护异常: {maintenance_err}",
                    exc_info=True,
                )

            return True
        except Exception as e:
            logger.error(f"[衰减调度] 执行衰减失败: {e}", exc_info=True)
            return False

    async def _check_and_execute(self) -> None:
        """检查并执行衰减（启动时调用）"""
        today_str = self._get_today_str()
        last_date_str = await self._get_last_decay_date()

        if last_date_str == today_str:
            logger.debug("[衰减调度] 今日已执行过衰减，跳过")
            return

        missed_days = await self._calculate_missed_days()
        total_days = missed_days + 1

        if missed_days > 0:
            logger.info(f"[衰减调度] 检测到错过 {missed_days} 天衰减，执行补偿")

        await self._execute_decay(total_days)

    def _should_skip_daily_backup(self) -> bool:
        """主库体积超过 skip_if_larger_than_mb 时跳过每日全量备份。

        线上实测：216MB 的主库配合 keep_days=7，最坏情况会在 backups/ 里堆出
        1.5GB，直接把小内存 / 小磁盘的宿主机压垮。
        """
        limit_mb = self.backup_skip_if_larger_than_mb
        if limit_mb <= 0 or not self.db_migration:
            return False

        db_path = Path(str(getattr(self.db_migration, "db_path", "") or ""))
        if not db_path.name:
            return False
        try:
            size = db_path.stat().st_size
        except OSError:
            # 拿不到体积就不拦截，交给 create_backup 自己处理
            return False
        if size <= limit_mb * 1024 * 1024:
            return False

        logger.warning(
            f"[衰减调度] 主库 {db_path.name} 体积 {size / 1024 / 1024:.1f}MB 超过 "
            f"backup_settings.skip_if_larger_than_mb ({limit_mb}MB)，已跳过本次"
            f"每日全量备份，避免备份文件占满磁盘。建议调小 backup_settings."
            f"max_keep / max_total_size_mb，或关闭每日自动备份"
            f"（backup_settings.enabled=false）并自行安排外部备份。"
        )
        return True

    async def _run_backup(self) -> None:
        """执行数据库备份并清理过期备份"""
        if not self.db_migration:
            return
        try:
            if self._should_skip_daily_backup():
                # 跳过备份也要清理：磁盘紧张时清理比新增备份更要紧
                await self._cleanup_old_backups()
                return

            backup_path = await self.db_migration.create_backup()
            if backup_path:
                logger.info(f"[衰减调度] 每日备份完成: {backup_path}")
            else:
                logger.warning("[衰减调度] 每日备份失败")
            await self._cleanup_old_backups()
        except Exception as e:
            logger.error(f"[衰减调度] 备份异常: {e}", exc_info=True)

    async def _cleanup_old_backups(self) -> None:
        """清理旧备份：保留天数 + 份数上限 + 总体积上限。

        统一委托 core.managers.backup_manager 的保留策略实现，因此：
        - 删除动作只发生在 <data_dir>/backups 内，且逐条做路径归属校验；
        - 除每日备份文件外，也会清理此前完全无人回收的 backups/v*/ 版本目录
          （线上实测其中单个目录就占 299MB）。
        """
        if not self.db_migration:
            return
        try:
            db_path = Path(self.db_migration.db_path)
            data_dir = db_path.parent
            if not (data_dir / "backups").exists():
                return

            stats = await prune_backups_in_async(
                data_dir,
                max_keep=self.backup_max_keep,
                max_total_size_mb=self.backup_max_total_size_mb,
                keep_days=self.backup_keep_days,
            )
            removed = len(stats.get("removed_dirs", [])) + len(
                stats.get("removed_files", [])
            )
            if removed:
                freed_mb = stats.get("bytes_freed", 0) / 1024 / 1024
                total_mb = stats.get("total_bytes_after", 0) / 1024 / 1024
                logger.info(
                    f"[衰减调度] 清理旧备份 {removed} 项，释放 {freed_mb:.2f} MB，"
                    f"备份目录当前 {total_mb:.2f} MB"
                    f"（保留 {self.backup_keep_days} 天 / 最多 "
                    f"{self.backup_max_keep} 份 / 上限 "
                    f"{self.backup_max_total_size_mb} MB）"
                )
        except Exception as e:
            logger.warning(f"[衰减调度] 清理旧备份失败: {e}")

    def _seconds_until_next_run(self) -> float:
        """计算距离下次执行的秒数"""
        now = datetime.now()
        target = now.replace(
            hour=self.check_hour,
            minute=self.check_minute,
            second=0,
            microsecond=0,
        )

        if now >= target:
            target += timedelta(days=1)

        return (target - now).total_seconds()

    async def _scheduler_loop(self) -> None:
        """调度器主循环"""
        while self._running:
            try:
                wait_seconds = self._seconds_until_next_run()
                logger.debug(f"[衰减调度] 下次执行在 {wait_seconds / 3600:.1f} 小时后")

                await asyncio.sleep(wait_seconds)

                if not self._running:
                    break

                await self._execute_decay(1)

            except asyncio.CancelledError:
                logger.info("[衰减调度] 调度器被取消")
                break
            except Exception as e:
                logger.error(f"[衰减调度] 循环异常: {e}", exc_info=True)
                await asyncio.sleep(3600)

    async def start(self) -> None:
        """启动调度器"""
        if self._running:
            logger.warning("[衰减调度] 调度器已在运行")
            return

        self._running = True

        await self._check_and_execute()

        self._task = asyncio.create_task(self._scheduler_loop())
        logger.info(
            f"[衰减调度] 调度器已启动 (衰减率: {self.decay_rate}, "
            f"执行时间: {self.check_hour:02d}:{self.check_minute:02d})"
        )

    async def stop(self) -> None:
        """停止调度器"""
        self._running = False

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._task = None
        logger.info("[衰减调度] 调度器已停止")
