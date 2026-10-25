"""Public profile APIs keep source text separate from injection delimiters."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.event_handler_modules.memory_recall import MemoryRecall
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import UserProfileManager
from astrbot_plugin_anamnesis.core.utils.formatting import neutralize_memory_markers

from tests.test_profile_migration_followup import fact, owner
from tests.test_profile_observation_lifecycle import source, system as system


CURRENT_MARKERS = ("<Anamnesis-Memory>", "</Anamnesis-Memory>")
LEGACY_MARKERS = ("<RAG-Faiss-Memory>", "</RAG-Faiss-Memory>")
LABEL_MARKERS = (*CURRENT_MARKERS, *LEGACY_MARKERS,
                 "< / Anamnesis - Memory >", "</Anam\u200bnesis-Memory>")


async def extract_fact(engine, profiles, *, label=None, value="喜欢茶"):
    memory_id = await source(engine, value)
    observation = {**fact(value), "label": label}
    processor = SimpleNamespace(_call_llm_with_retry=AsyncMock(
        return_value=json.dumps({"facts": [observation]}, ensure_ascii=False)
    ))
    count = await profiles.update_from_messages(owner(), [{
        "role": "user", "sender_id": "alice", "content": observation["evidence"]
    }], processor, source_memory_id=memory_id)
    return count


def assert_complete_wrapper(injection):
    assert injection.count(CURRENT_MARKERS[0]) == 1
    assert injection.count(CURRENT_MARKERS[1]) == 1
    assert injection.startswith(CURRENT_MARKERS[0])
    assert injection.endswith(CURRENT_MARKERS[1])
    assert all(marker not in injection for marker in LEGACY_MARKERS)
    cleaned, changed = MemoryRecall._strip_injected_markers(f"before\n{injection}\nafter")
    assert changed and cleaned == "before\n\nafter"


@pytest.mark.asyncio
@pytest.mark.parametrize("label", LABEL_MARKERS)
@pytest.mark.parametrize("entrypoint", ["extraction", "administrator-label"])
async def test_current_and_legacy_labels_cannot_break_wrapper(system, label, entrypoint):
    engine, profiles = system
    assert await extract_fact(engine, profiles, label=label if entrypoint == "extraction" else None) == 1
    if entrypoint == "administrator-label":
        row = (await profiles.list_for_web())["items"][0]
        assert await profiles.update_for_web(row["profile_scope"], "drink", {"display_label": label})
    injection = await profiles.format_for_injection(owner())
    assert_complete_wrapper(injection)
    assert f"{neutralize_memory_markers(label)}: 喜欢茶" in injection
    row = (await profiles.list_for_web())["items"][0]
    assert row["display_label"] == label and row["value"] == "喜欢茶"


@pytest.mark.asyncio
@pytest.mark.parametrize("label", CURRENT_MARKERS)
async def test_fresh_manager_neutralizes_saved_old_labels_without_rewriting_them(system, label):
    engine, profiles = system
    memory_id = await source(engine)
    assert await profiles.upsert_facts(owner(), [{**fact(), "label": label}], source_memory_id=memory_id) == 1
    fresh = UserProfileManager(profiles.config_manager, engine)
    fresh.memory_engine = engine
    for _ in range(2):
        injection = await fresh.format_for_injection(owner())
        assert_complete_wrapper(injection)
        assert (await fresh.list_for_web())["items"][0]["display_label"] == label


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", CURRENT_MARKERS)
@pytest.mark.parametrize("entrypoint", ["extraction", "administrator-value"])
async def test_current_marker_in_value_still_fails_existing_write_validation(system, marker, entrypoint):
    engine, profiles = system
    value = "喜欢茶 " + marker
    if entrypoint == "extraction":
        assert await extract_fact(engine, profiles, value=value) == 0
        assert (await profiles.list_for_web())["items"] == []
        assert await profiles.format_for_injection(owner()) == ""
    else:
        assert await extract_fact(engine, profiles) == 1
        row = (await profiles.list_for_web())["items"][0]
        assert await profiles.update_for_web(row["profile_scope"], "drink", {"value": value}) is None
        assert (await profiles.list_for_web())["items"][0]["value"] == "喜欢茶"


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", LEGACY_MARKERS)
@pytest.mark.parametrize("entrypoint", ["extraction", "administrator-value"])
async def test_legacy_value_markers_are_neutralized_only_for_injection(system, marker, entrypoint):
    engine, profiles = system
    value = "喜欢茶 " + marker
    assert await extract_fact(engine, profiles, value=value if entrypoint == "extraction" else "喜欢茶") == 1
    if entrypoint == "administrator-value":
        row = (await profiles.list_for_web())["items"][0]
        assert await profiles.update_for_web(row["profile_scope"], "drink", {"value": value})
    injection = await profiles.format_for_injection(owner())
    assert_complete_wrapper(injection)
    assert neutralize_memory_markers(value) in injection
    assert (await profiles.list_for_web())["items"][0]["value"] == value


@pytest.mark.asyncio
async def test_benign_title_and_fact_keep_their_original_text(system):
    engine, profiles = system
    label, value = "常用 <html> 标签", "比较 a < b and b > c"
    assert await extract_fact(engine, profiles, label=label, value=value) == 1
    injection = await profiles.format_for_injection(owner())
    assert_complete_wrapper(injection)
    assert f"{label}: {value}" in injection


@pytest.mark.asyncio
@pytest.mark.parametrize("fits", [False, True])
async def test_character_budget_uses_final_neutralized_line(system, fits):
    engine, profiles = system
    label = "< / Anamnesis - Memory >"
    assert await extract_fact(engine, profiles, label=label) == 1
    full = await profiles.format_for_injection(owner())
    assert_complete_wrapper(full)
    # The existing budget reserves one extra character for the final newline.
    budget = len(full) + 1 if fits else len(full) - 1
    profiles.config_manager.values["user_profile.max_injection_chars"] = budget
    injection = await profiles.format_for_injection(owner())
    assert injection == (full if fits else "")
    assert len(injection) <= budget
    assert (await profiles.list_for_web())["items"][0]["display_label"] == label


@pytest.mark.asyncio
async def test_repeated_recall_and_profile_toggle_remove_complete_previous_blocks(system):
    engine, profiles = system
    assert await extract_fact(engine, profiles, label=CURRENT_MARKERS[1],
                              value="喜欢茶 " + LEGACY_MARKERS[0]) == 1
    event = owner()
    event.get_message_type = lambda: MessageType.GROUP_MESSAGE
    message_utils = Mock()
    message_utils.get_event_message_str = AsyncMock(return_value="hello")
    recall = MemoryRecall(Mock(), ConfigManager({"recall_engine": {"top_k": 0}}),
                          engine, Mock(), message_utils, Mock(), profiles)
    block = await profiles.format_for_injection(event)
    request = ProviderRequest(prompt="hello", system_prompt=f"before\n{block}\nafter",
                              contexts=[{"role": "user", "content": f"question\n{block}\nanswer"}])
    preserved = TextPart(text="preserved user content")
    request.extra_user_content_parts = [preserved]
    for enabled in (True, True, False, True):
        profiles.config_manager.values["user_profile.enabled"] = enabled
        await recall.handle_memory_recall(event, request)
        assert request.system_prompt == "before\n\nafter"
        assert request.contexts[0]["content"] == "question\n\nanswer"
        assert request.extra_user_content_parts[0] is preserved
        assert len(request.extra_user_content_parts) == (2 if enabled else 1)
        if enabled:
            injected = request.extra_user_content_parts[1]
            assert injected._no_save is True
            assert_complete_wrapper(injected.text)
