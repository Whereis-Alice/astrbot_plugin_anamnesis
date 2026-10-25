"""Schema-backed configuration editor; saving never partially reloads runtime."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot.api import logger
from quart import request


class SettingsHandler:
    def __init__(self, utils):
        self.utils = utils
        self.root = Path(__file__).resolve().parents[2]

    @staticmethod
    def _lookup(data, path, default=None):
        for part in path.split("."):
            if not isinstance(data, dict):
                return default
            data = data.get(part)
        return data if data is not None else default

    def _schema(self):
        return json.loads((self.root / "_conf_schema.json").read_text(encoding="utf-8"))

    def _locale(self, language):
        name = {"en": "en-US", "en-US": "en-US", "ru": "ru-RU", "ru-RU": "ru-RU"}.get(language)
        if not name:
            return {}
        try:
            data = json.loads((self.root / ".astrbot-plugin" / "i18n" / (name + ".json")).read_text(encoding="utf-8"))
            return data.get("config", data)
        except (OSError, ValueError):
            return {}

    def describe(self, config_manager, language="zh"):
        schema = self._schema()
        locale = self._locale(language)
        config = config_manager.get_saved_settings()
        sections = []

        def flatten(items, prefix):
            fields = []
            for key, meta in items.items():
                if not isinstance(meta, dict):
                    continue
                path = f"{prefix}.{key}" if prefix else key
                if meta.get("type") == "object":
                    fields.extend(flatten(meta.get("items", {}), path))
                    continue
                if meta.get("type") not in {"bool", "int", "float", "string", "text", "list"}:
                    continue
                translated = self._lookup(locale, path, {})
                field = {
                    "path": path,
                    "label": translated.get("description", meta.get("description", key)),
                    "hint": translated.get("hint", meta.get("hint", "")),
                    "type": meta["type"],
                    "default": meta.get("default"),
                    "value": self._lookup(config, path, meta.get("default")),
                }
                options = meta.get("options")
                if isinstance(options, list):
                    field["options"] = options
                    labels = translated.get("labels")
                    field["option_labels"] = labels if isinstance(labels, list) else options
                field.update({k: v for k, v in meta.get("slider", {}).items() if k in {"min", "max", "step"}})
                fields.append(field)
            return fields

        for key, meta in schema.items():
            if not isinstance(meta, dict):
                continue
            translated = locale.get(key, {})
            fields = flatten(meta.get("items", {}), key) if meta.get("type") == "object" else flatten({key: meta}, "")
            if fields:
                sections.append({
                    "key": key,
                    "title": translated.get("description", meta.get("description", key)),
                    "hint": translated.get("hint", meta.get("hint", "")),
                    "fields": fields,
                })
        return {"sections": sections, "config": config, "reload_required": True}

    async def get_config(self, config_manager):
        try:
            return self.utils.ok(self.describe(config_manager, request.args.get("lang", "zh")))
        except Exception:
            logger.exception("读取配置页面失败")
            return self.utils.error("读取配置失败，请查看插件日志")

    async def update_config(self, config_manager):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("updates"), dict):
            return self.utils.error("必须提供 updates 配置对象")
        try:
            allowed = {field["path"] for section in self.describe(config_manager)["sections"] for field in section["fields"]}
            result = await asyncio.to_thread(config_manager.save_settings, payload["updates"], allowed)
            return self.utils.ok(result)
        except Exception as error:
            logger.warning("保存插件配置失败: %s", error)
            return self.utils.error(str(error))
