"""Provider-boundary regression for persona overrides and later summaries."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_anamnesis.core.prompts import prompt_manager

from tests.test_memory_administration import add
from tests.test_memory_administration import (
    administration as administration,  # noqa: PLC0414
)
from tests.test_memory_processor import _make_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_custom_persona_keeps_expression_but_provider_receives_reality_rules(monkeypatch, group):
    # An already saved custom prompt remains in place across plugin upgrades.
    monkeypatch.setattr(prompt_manager, "get_prompt_manager", lambda: None)
    provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(
        completion_text=json.dumps({"summary": "张三在玩梗", "key_facts": ["张三用老婆称呼角色"]})
    )))
    context = SimpleNamespace(persona_manager=SimpleNamespace(
        get_persona=AsyncMock(return_value=SimpleNamespace(system_prompt="你是一位有妻子的虚构骑士"))
    ))
    processor = MemoryProcessor(llm_provider=provider, context=context)
    processor.private_chat_prompt = processor.group_chat_prompt = "已有自定义模板 {conversation}"
    await processor.process_conversation(_make_messages(), is_group_chat=group, persona_id="knight")
    call = provider.text_chat.call_args.kwargs
    assert "已有自定义模板" in call["prompt"]
    assert "虚构骑士" in call["system_prompt"]
    assert call["system_prompt"].index("事实与语境边界") > call["system_prompt"].index("虚构骑士")
    assert "不能推断现实婚姻" in call["system_prompt"]
    assert "canonical_summary、key_facts" in call["system_prompt"]


@pytest.mark.asyncio
async def test_merge_provider_receives_original_qualifiers_and_context_rules():
    processor = MemoryProcessor(llm_provider=SimpleNamespace())
    processor._call_llm_with_retry = AsyncMock(return_value=json.dumps({"summary": "张三玩梗", "key_facts": []}))
    await processor.merge_memories([{"id": 1, "content": "张三开玩笑说爱莉希雅是老婆", "metadata": {}}])
    prompt, system = processor._call_llm_with_retry.call_args.args
    assert "张三开玩笑说" in prompt
    assert "合并摘要都必须保留" in system
    assert "不得写‘张三已婚（有妻子）’" in system


@pytest.mark.asyncio
async def test_event_job_keeps_joke_evidence_and_applies_same_rules(administration):
    memory_id = await add(administration, "张三开玩笑说爱莉希雅是老婆")
    administration.processor._call_llm_with_retry.return_value = json.dumps({
        "title": "群聊玩梗", "summary": "张三用老婆称呼角色爱莉希雅", "memory_ids": [memory_id]
    })
    job = await administration.create_job("episode", {"memory_ids": [memory_id], "use_llm": True})
    await administration._jobs[job["operation_id"]]
    result = await administration.store.get(job["operation_id"])
    assert result["status"] == "completed"
    prompt, system = administration.processor._call_llm_with_retry.call_args.args
    assert "张三开玩笑说" in prompt
    assert "合并摘要都必须保留" in system
    assert "不能推断现实婚姻" in system


def test_real_world_verification_default_and_disabled_setting_survive_config_validation():
    assert ConfigManager().get("user_profile.verify_real_world_facts") is True
    assert ConfigManager().get("user_profile.verification_timeout") == 45.0
    config = ConfigManager({"user_profile": {"verify_real_world_facts": False, "verification_timeout": 12}})
    assert config.get("user_profile.verify_real_world_facts") is False
    assert config.get("user_profile.verification_timeout") == 12.0


@pytest.mark.parametrize("value,expected", [(True, 45.0), ("nan", 45.0), (999, 180.0), (-1, 1.0)])
def test_verification_timeout_normalization_preserves_unrelated_settings(value, expected):
    config = ConfigManager({"user_profile": {"verification_timeout": value, "max_items": 23}})
    assert config.get("user_profile.verification_timeout") == expected
    assert config.get("user_profile.max_items") == 23
