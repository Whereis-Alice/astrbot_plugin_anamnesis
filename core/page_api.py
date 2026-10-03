"""
官方插件 Page API 适配层。

职责：
1. 为 AstrBot 官方插件页面注册原生 Web API。
2. 直接复用插件运行期组件，不再代理到旧 FastAPI WebUI。
3. 保留返回结构与旧前端尽量一致，降低页面迁移成本。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from quart import request

from .page_api_modules import (
    BackupHandler,
    ConsolidationHandler,
    GraphHandler,
    MemoryHandler,
    PageApiUtils,
    PromptHandler,
    RecallHandler,
    StatsHandler,
)

PLUGIN_NAME = "astrbot_plugin_anamnesis"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"


class PluginPageApi:
    """Anamnesis 官方插件页面 API（Facade）。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

        # 初始化工具类
        self.utils = PageApiUtils()

        # 初始化各个处理器
        self.stats_handler = StatsHandler(self.utils)
        self.memory_handler = MemoryHandler(self.utils)
        self.recall_handler = RecallHandler(self.utils)
        self.graph_handler = GraphHandler(self.utils)
        self.prompt_handler = PromptHandler(self.utils)
        self.consolidation_handler = ConsolidationHandler(self.utils)

        # BackupHandler 需要 data_dir，延迟初始化
        self._backup_handler = None

    @property
    def backup_handler(self) -> BackupHandler:
        """延迟初始化 BackupHandler"""
        if self._backup_handler is None:
            data_dir = (
                self.plugin.initializer.data_dir if self.plugin.initializer else ""
            )
            self._backup_handler = BackupHandler(self.utils, data_dir)
        return self._backup_handler

    def register_routes(self) -> None:
        """注册官方插件页面所需的原生 API。"""
        register = self.plugin.context.register_web_api
        register(
            f"{PAGE_API_PREFIX}/stats",
            self.get_stats,
            ["GET"],
            "Anamnesis Page stats",
        )
        register(
            f"{PAGE_API_PREFIX}/memories",
            self.list_memories,
            ["GET"],
            "Anamnesis Page memories",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/detail",
            self.get_memory_detail,
            ["GET"],
            "Anamnesis Page memory detail",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/update",
            self.update_memory,
            ["POST"],
            "Anamnesis Page update memory",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/resummarize",
            self.resummarize_memory,
            ["POST"],
            "Anamnesis Page resummarize memory source",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/export",
            self.export_memories,
            ["POST"],
            "Anamnesis Page export memories",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/import",
            self.import_memories,
            ["POST"],
            "Anamnesis Page import memories",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/batch-delete",
            self.batch_delete_memories,
            ["POST"],
            "Anamnesis Page batch delete memories",
        )
        register(
            f"{PAGE_API_PREFIX}/memories/batch-update",
            self.batch_update_memories,
            ["POST"],
            "Anamnesis Page batch update memories",
        )
        register(
            f"{PAGE_API_PREFIX}/profiles",
            self.list_profiles,
            ["GET"],
            "Anamnesis Page user profiles",
        )
        register(
            f"{PAGE_API_PREFIX}/profiles/delete",
            self.delete_profile,
            ["POST"],
            "Anamnesis Page delete user profile item",
        )
        register(
            f"{PAGE_API_PREFIX}/recall/test",
            self.test_recall,
            ["POST"],
            "Anamnesis Page recall test",
        )
        register(
            f"{PAGE_API_PREFIX}/graph/overview",
            self.get_graph_overview,
            ["GET"],
            "Anamnesis Page graph overview",
        )
        register(
            f"{PAGE_API_PREFIX}/graph/query",
            self.query_graph,
            ["POST"],
            "Anamnesis Page graph query",
        )
        register(
            f"{PAGE_API_PREFIX}/backups",
            self.list_backups,
            ["GET"],
            "Anamnesis Page backup list",
        )
        register(
            f"{PAGE_API_PREFIX}/prompts",
            self.list_prompts,
            ["GET"],
            "Anamnesis Page prompt list",
        )
        register(
            f"{PAGE_API_PREFIX}/prompts/detail",
            self.get_prompt_detail,
            ["GET"],
            "Anamnesis Page prompt detail",
        )
        register(
            f"{PAGE_API_PREFIX}/prompts/update",
            self.update_prompt,
            ["POST"],
            "Anamnesis Page update prompt",
        )
        register(
            f"{PAGE_API_PREFIX}/prompts/reset",
            self.reset_prompt,
            ["POST"],
            "Anamnesis Page reset prompt",
        )
        register(
            f"{PAGE_API_PREFIX}/prompts/default",
            self.get_prompt_default,
            ["GET"],
            "Anamnesis Page get prompt default content",
        )
        register(
            f"{PAGE_API_PREFIX}/consolidation/status",
            self.get_consolidation_status,
            ["GET"],
            "Anamnesis Page consolidation status",
        )
        register(
            f"{PAGE_API_PREFIX}/consolidation/run",
            self.run_consolidation,
            ["POST"],
            "Anamnesis Page run consolidation",
        )

    # ==================== 路由处理方法 ====================
    # 所有方法都委托给相应的处理器

    async def get_stats(self):
        """获取插件统计信息"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.stats_handler.get_stats(ready["memory_engine"])

    async def list_memories(self):
        """获取记忆列表（带分页和过滤）"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.list_memories(ready["memory_engine"])

    async def get_memory_detail(self):
        """获取单个记忆的完整详情"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.get_memory_detail(ready["memory_engine"])

    async def update_memory(self):
        """更新单个记忆的字段"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.update_memory(ready["memory_engine"])

    async def resummarize_memory(self):
        """Regenerate one memory from its retained source messages."""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.resummarize_memory(
            ready["memory_engine"], ready["memory_processor"]
        )

    async def export_memories(self):
        """Export all or selected memories."""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.export_memories(ready["memory_engine"])

    async def import_memories(self):
        """Preview or import portable memory data."""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.import_memories(
            ready["memory_engine"], ready["memory_processor"]
        )

    async def batch_delete_memories(self):
        """批量删除记忆"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.batch_delete_memories(
            ready["memory_engine"], getattr(self.plugin, "user_profile_manager", None)
        )

    async def batch_update_memories(self):
        """批量更新记忆字段"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.memory_handler.batch_update_memories(ready["memory_engine"])

    async def list_profiles(self):
        """List independent user-profile facts for the administrator WebUI."""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        profile_manager = getattr(self.plugin, "user_profile_manager", None)
        if profile_manager is None:
            return self.utils.error("用户档案组件尚未初始化")

        args = request.args
        try:
            limit = max(1, min(500, int(args.get("limit", 200))))
            offset = max(0, int(args.get("offset", 0)))
        except (TypeError, ValueError):
            return self.utils.error("limit 和 offset 必须是整数")
        try:
            data = await profile_manager.list_for_web(
                profile_scope=self.utils.optional_text(args.get("scope")),
                source_session_id=self.utils.optional_text(args.get("session_id")),
                key_query=self.utils.optional_text(args.get("key")),
                limit=limit,
                offset=offset,
            )
            return self.utils.ok(data)
        except Exception:
            logger.exception("读取用户档案失败")
            return self.utils.error("读取用户档案失败，请查看插件日志")

    async def delete_profile(self):
        """Delete one exact profile scope/key selected in the WebUI."""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        profile_manager = getattr(self.plugin, "user_profile_manager", None)
        if profile_manager is None:
            return self.utils.error("用户档案组件尚未初始化")
        payload = await request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return self.utils.error("请求内容必须是 JSON 对象")
        scope = self.utils.optional_text(payload.get("profile_scope"))
        key = self.utils.optional_text(payload.get("profile_key"))
        if not scope or not key:
            return self.utils.error("必须提供 profile_scope 和 profile_key")
        try:
            deleted = await profile_manager.delete_for_web(scope, key)
            if not deleted:
                return self.utils.error("档案条目不存在或已删除")
            return self.utils.ok({"deleted": True, "count": deleted})
        except Exception:
            logger.exception("删除用户档案失败")
            return self.utils.error("删除用户档案失败，请查看插件日志")

    async def test_recall(self):
        """测试记忆召回功能"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.recall_handler.test_recall(ready["memory_engine"])

    async def get_graph_overview(self):
        """获取图谱概览"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.graph_handler.get_graph_overview(ready["memory_engine"])

    async def query_graph(self):
        """查询图谱"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.graph_handler.query_graph(ready["memory_engine"])

    async def list_backups(self):
        """列出所有版本备份及其元数据"""
        return await self.backup_handler.list_backups()

    # ---- Prompt 管理路由 ----

    async def list_prompts(self):
        return await self.prompt_handler.list_prompts()

    async def get_prompt_detail(self):
        return await self.prompt_handler.get_prompt_detail()

    async def update_prompt(self):
        return await self.prompt_handler.update_prompt()

    async def reset_prompt(self):
        return await self.prompt_handler.reset_prompt()

    async def get_prompt_default(self):
        return await self.prompt_handler.get_prompt_default()

    # ---- 记忆整合路由 ----

    async def get_consolidation_status(self):
        """获取记忆整合配置与统计"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.consolidation_handler.get_status(
            ready["memory_engine"],
            ready["consolidation_manager"],
            ready["config_manager"],
        )

    async def run_consolidation(self):
        """手动触发一轮记忆整合"""
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        return await self.consolidation_handler.run(ready["consolidation_manager"])

    # ==================== 辅助方法 ====================

    async def _ensure_plugin_ready(self) -> tuple[dict[str, Any] | None, dict | None]:
        """
        确保插件已就绪

        Returns:
            (ready_dict, error_dict) 元组
            - ready_dict: 包含 memory_engine 等组件的字典
            - error_dict: 错误响应字典（如果有错误）
        """
        ready, message = await self.plugin._ensure_plugin_ready()
        if not ready:
            return None, self.utils.error(message or "插件尚未就绪")

        memory_engine = self.plugin.initializer.memory_engine
        if memory_engine is None:
            return None, self.utils.error("记忆引擎未初始化")

        return {
            "memory_engine": memory_engine,
            "conversation_manager": self.plugin.initializer.conversation_manager,
            "index_validator": self.plugin.initializer.index_validator,
            "memory_processor": getattr(
                self.plugin.initializer, "memory_processor", None
            ),
            "consolidation_manager": getattr(
                self.plugin.initializer, "consolidation_manager", None
            ),
            "config_manager": getattr(self.plugin, "config_manager", None)
            or getattr(self.plugin.initializer, "config_manager", None),
        }, None
