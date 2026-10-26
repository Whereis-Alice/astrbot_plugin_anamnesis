"""Authenticated native WebUI entrypoints for reviewable administration."""

from __future__ import annotations

import time

from quart import g, request

from ...storage.administration_store import AdministrationRecordError
from ..managers.memory_administration import MemoryAdministration


class AdministrationPageMixin:
    def register_administration_routes(self):
        prefix = "/astrbot_plugin_anamnesis/page/"
        routes = {
            "operations": ("GET", "list"), "operations/detail": ("GET", "detail"),
            "operations/delete": ("POST", "delete"), "operations/restore": ("POST", "restore"),
            "operations/purge": ("POST", "purge"), "operations/remove": ("POST", "remove"),
            "corrections/preview": ("POST", "preview"), "corrections/confirm": ("POST", "confirm"),
            "corrections/execute": ("POST", "execute"), "corrections/rollback": ("POST", "rollback"),
            "jobs/create": ("POST", "create_job"), "jobs/action": ("POST", "job_action"),
            "episodes": ("GET", "episodes"), "episodes/delete": ("POST", "delete_episode"),
            "projection": ("GET", "projection"), "projection/repair": ("POST", "repair_projection"),
        }
        for path, (method, action) in routes.items():
            async def endpoint(action=action):
                return await self.administration_request(action)
            endpoint.__name__ = "anam_admin_" + action
            self.plugin.context.register_web_api(prefix+path, endpoint, [method], "Anamnesis memory administration")

    async def _administration(self, ready):
        engine = ready["memory_engine"]
        service = getattr(engine, "memory_administration", None)
        if not isinstance(service, MemoryAdministration):
            service = MemoryAdministration(engine, ready["config_manager"], ready["memory_processor"])
            engine.memory_administration = service
        await service.initialize()
        return service

    async def administration_request(self, action):
        ready, error = await self._ensure_plugin_ready()
        if error:
            return error
        try:
            service = await self._administration(ready)
            # Authentication is performed by AstrBot's register_web_api middleware.
            # Actor is never taken from JSON, query parameters or model arguments.
            actor = "webui:" + str(getattr(g, "username", None) or getattr(g, "user", None) or "administrator")
            payload = await request.get_json(silent=True) or {} if request.method == "POST" else {}
            if not isinstance(payload, dict):
                raise TypeError("参数必须为 JSON 对象")
            operation_id = str(payload.get("operation_id") or request.args.get("operation_id") or "")
            if action == "list":
                data = await service.store.list(limit=100)
                for row in data:
                    if row["kind"] == "correction" and not row.get("corrupt_fields"):
                        try:
                            service._validate_correction_log(row)
                        except AdministrationRecordError:
                            row["corrupt_fields"] = ["correction_log"]
                result = [{key: value for key, value in row.items() if key in ("id", "kind", "status", "created_at", "expires_at", "revision")} | {
                    "result": {key: value for key, value in row["result"].items() if key != "after"},
                    "expired": row["expires_at"] <= time.time(),
                    **({"corrupt_fields": row["corrupt_fields"]} if row.get("corrupt_fields") else {}),
                } for row in data]
            elif action == "detail":
                operation = await service._owned(operation_id, actor, allow_expired=True)
                result = {key: value for key, value in operation.items() if key not in ("token_hash", "token_expires", "token_actor")}
                if operation["kind"] == "import":
                    result["chunks"] = [{"index": item["item_key"], "status": item["status"], "result": item["result"]} for item in await service.store.items(operation_id)]
                if operation["kind"] == "correction":
                    result["old_content"] = operation["payload"]["before"]["text"]
                    result["new_content"] = operation["payload"]["content"]
            elif action == "delete":
                result = await service.delete(payload.get("memory_ids"), actor=actor, reason=payload.get("reason", ""))
            elif action in ("restore", "purge"):
                result = await service.restore(operation_id, actor=actor, purge=action == "purge")
            elif action == "remove":
                result = await service.remove_operation(operation_id, actor=actor)
            elif action == "preview":
                result = await service.preview_correction(payload.get("memory_id"), payload.get("instruction"), actor=actor,
                                                          new_content=payload.get("new_content"))
            elif action == "confirm":
                result = await service.confirm(operation_id, actor=actor)
            elif action == "execute":
                result = await service.execute_correction(operation_id, payload.get("confirmation_token"), actor=actor)
            elif action == "rollback":
                result = await service.rollback_correction(operation_id, actor=actor)
            elif action == "create_job":
                result = await service.create_job(payload.get("kind"), payload.get("payload", {}), actor=actor)
            elif action == "job_action":
                result = await service.job_action(operation_id, payload.get("action"), actor=actor)
            elif action == "episodes":
                result = await service.list_episodes(session_id=self.utils.optional_text(request.args.get("session_id")),
                                                     persona_id=self.utils.optional_text(request.args.get("persona_id")))
            elif action == "delete_episode":
                async with service.store.connection(write=True) as db:
                    cursor = await db.execute("DELETE FROM memory_episodes WHERE id=? AND revision=?", (payload.get("episode_id"), payload.get("expected_revision")))
                    if cursor.rowcount != 1:
                        raise ValueError("事件已变化，请刷新")
                result = {"deleted": True}
            elif action == "projection":
                result = await service.projection.pending(100)
            else:
                service.require("projection")
                result = await service.create_job("projection", {"limit": 10}, actor=actor)
            return self.utils.ok(result)
        except (ValueError, TypeError, KeyError) as exc:
            return self.utils.error(str(exc))
        except Exception:  # noqa: BLE001 -- native API boundary logs provider and storage failures
            from astrbot.api import logger
            logger.exception("记忆管理操作失败")
            return self.utils.error("操作失败，请查看插件日志")
