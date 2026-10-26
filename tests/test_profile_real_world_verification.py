"""Automatic profiles require attributable, real-world statements before writing."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.base.config_manager import ConfigManager
from astrbot_plugin_anamnesis.core.managers import profile_fact_verifier
from astrbot_plugin_anamnesis.core.managers.user_profile_manager import (
    UserProfileManager,
)
from astrbot_plugin_anamnesis.storage.conversation_store import ConversationStore


def owner():
    return SimpleNamespace(
        unified_msg_origin="qq:GroupMessage:42", get_sender_id=lambda: "alice",
        get_sender_name=lambda: "alice", get_platform_name=lambda: "qq",
        get_group_id=lambda: "42", message_obj=SimpleNamespace(group=None),
    )


def statement(text, sender="alice", role="user"):
    return {"role": role, "sender_id": sender, "content": text}


def candidate(evidence="我有妻子", value="已婚（有妻子）", **kwargs):
    return {"key": "marital_status", "category": "identity", "value": value,
            "confidence": .99, "explicit": True, "evidence": evidence, **kwargs}


def verdict(index=0, ref="m1", **kwargs):
    return {"candidate_id": index, "message_ref": ref, "self_asserted": True,
            "real_world": True, "entailed": True, **kwargs}


class Processor:
    def __init__(self, facts, checks=None, failure=None):
        self.facts, self.checks, self.failure = facts, checks, failure
        self.calls = []

    async def _call_llm_with_retry(self, prompt, system_prompt, max_retries):
        self.calls.append((prompt, system_prompt, max_retries))
        if len(self.calls) == 1:
            return json.dumps({"facts": self.facts}, ensure_ascii=False)
        if self.failure:
            raise self.failure
        return json.dumps({"checks": self.checks}, ensure_ascii=False)


@pytest_asyncio.fixture
async def profiles(tmp_path):
    store = ConversationStore(str(tmp_path / "profiles.db"))
    await store.initialize()
    manager = UserProfileManager(
        ConfigManager({"user_profile": {"enabled": True}}), SimpleNamespace(store=store)
    )
    try:
        yield manager
    finally:
        await store.close()


async def extract(profiles, messages, processor, source=10):
    return await profiles.update_from_messages(
        owner(), messages, processor, source_memory_id=source
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "evidence"), [
    ("[引用: 我有妻子] 我在回复他", "我有妻子"),
    ("[图片: 我有妻子] 这是截图", "我有妻子"),
    ('他说：“我有妻子”。', "我有妻子"),
    ('> 我有妻子\n这只是他发的消息', "我有妻子"),
    ("假如我有妻子，我会去度假。", "我有妻子"),
    ("不是说我有妻子，别误会。", "我有妻子"),
    ("难道我有妻子吗？", "我有妻子"),
    ("[RP] 我有妻子", "我有妻子"),
    ("剧本台词：我有妻子", "我有妻子"),
    ("开玩笑的：我有妻子", "我有妻子"),
    ("我有妻子（开玩笑）", "我有妻子"),
    ("我有妻子，开玩笑的", "我有妻子"),
    ("假如我有妻子", "假如我有妻子"),
    ("不是说我有妻子", "不是说我有妻子"),
    ("难道我有妻子吗？", "难道我有妻子吗"),
    ("我没有妻子", "我没有妻子"),
])
async def test_obvious_nonfacts_cannot_enter_ledger_even_with_verifier_off(profiles, text, evidence):
    profiles.config_manager = ConfigManager({"user_profile": {
        "enabled": True, "verify_real_world_facts": False,
    }})
    processor = Processor([candidate(evidence)])
    assert await extract(profiles, [statement(text)], processor) == 0
    assert await profiles.get_profile(owner()) == []
    assert len(processor.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["我有妻子", "我现实中已婚", "我现实中已婚，老婆网名叫爱莉希雅"])
async def test_real_marriage_requires_and_passes_independent_verification(profiles, text):
    processor = Processor([candidate(text)], [verdict()])
    assert await extract(profiles, [statement(text)], processor) == 1
    assert len(processor.calls) == 2
    assert processor.calls[1][2] == 1
    assert "现实" in processor.calls[1][1]
    assert (await profiles.get_profile(owner()))[0]["authority"] == "direct_user"


@pytest.mark.asyncio
@pytest.mark.parametrize("checks", [
    None, [], [{"candidate_id": 0}], [verdict(real_world=False)],
    [verdict(self_asserted=False)], [verdict(entailed=False)],
    [verdict(real_world="true")], [verdict(index=True)], [verdict(ref="m2")],
    [verdict(), verdict()],
])
async def test_missing_ambiguous_or_malformed_verdict_cannot_create_first_fact(profiles, checks):
    processor = Processor([candidate()], checks)
    assert await extract(profiles, [statement("我有妻子")], processor) == 0
    assert await profiles.get_profile(owner()) == []


@pytest.mark.asyncio
async def test_role_wife_ambiguity_is_rejected_before_same_value_can_renew_evidence(profiles):
    await profiles.upsert_facts(owner(), [candidate()], source_memory_id=9)
    before = (await profiles.list_for_web())["items"][0]
    processor = Processor([candidate("给你看看我老婆")], [verdict(real_world=False)])
    assert await extract(profiles, [statement("给你看看我老婆，爱莉希雅可爱吧")], processor) == 0
    after = (await profiles.list_for_web())["items"][0]
    assert after["revision"] == before["revision"]
    assert after["evidence"] == before["evidence"]


@pytest.mark.asyncio
async def test_other_speakers_are_context_only_and_cannot_supply_evidence(profiles):
    processor = Processor([candidate()], [verdict(ref="m2")])
    messages = [statement("我有妻子", sender="bob"), statement("你说什么？"),
                statement("我有妻子", sender="bot", role="assistant")]
    assert await extract(profiles, messages, processor) == 0
    assert len(processor.calls) == 1
    assert "bob" in processor.calls[0][0]
    assert '"evidence_allowed": false' in processor.calls[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("facts", "count"), [
    ([candidate()], 0), ([candidate(message_ref="m2")], 1),
])
async def test_legacy_evidence_requires_unique_message_match(profiles, facts, count):
    processor = Processor(facts, [verdict(ref="m2")])
    assert await extract(profiles, [statement("我有妻子"), statement("我有妻子")], processor) == count


@pytest.mark.asyncio
async def test_evidence_cannot_be_joined_across_messages(profiles):
    processor = Processor([candidate("我是\n已婚用户")], [verdict()])
    assert await extract(profiles, [statement("我是"), statement("已婚用户")], processor) == 0


@pytest.mark.asyncio
async def test_oversized_message_is_not_tail_cut_into_false_real_world_fact(profiles):
    profiles.config_manager = ConfigManager({"user_profile": {
        "enabled": True, "extraction_max_chars": 1000,
    }})
    processor = Processor([candidate()], [verdict()])
    text = "角色扮演场景，以下是台词：" + "台词" * 500 + "我有妻子"
    assert await extract(profiles, [statement(text)], processor) == 0
    assert processor.calls == []


@pytest.mark.asyncio
async def test_previous_roleplay_frame_survives_message_boundary(profiles):
    profiles.config_manager = ConfigManager({"user_profile": {
        "enabled": True, "verify_real_world_facts": False,
    }})
    processor = Processor([candidate()])
    assert await extract(profiles, [statement("接下来我们开始角色扮演"), statement("我有妻子")], processor) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["我不是在开玩笑，我现实中已婚", '我现实中已婚，老婆网名叫“爱莉希雅”'])
async def test_real_clarification_and_quoted_nickname_are_valid(profiles, text):
    processor = Processor([candidate(text)], [verdict()])
    assert await extract(profiles, [statement(text)], processor) == 1


@pytest.mark.asyncio
async def test_conditional_address_to_reader_is_not_a_hypothetical_marriage(profiles):
    processor = Processor([candidate()], [verdict()])
    assert await extract(profiles, [statement("如果你想知道，我有妻子")], processor) == 1


@pytest.mark.asyncio
async def test_explicit_english_negative_marital_state_is_a_valid_real_fact(profiles):
    processor = Processor([candidate("I am not married", value="not married")], [verdict()])
    assert await extract(profiles, [statement("I am not married")], processor) == 1
    assert (await profiles.get_profile(owner()))[0]["value"] == "not married"


@pytest.mark.asyncio
async def test_absence_of_wife_cannot_be_inferred_as_unmarried(profiles):
    profiles.config_manager = ConfigManager({"user_profile": {
        "enabled": True, "verify_real_world_facts": False,
    }})
    processor = Processor([candidate("我没有妻子", value="未婚")])
    assert await extract(profiles, [statement("我没有妻子")], processor) == 0
    valid = Processor([candidate("我目前未婚", value="未婚")])
    assert await extract(profiles, [statement("我目前未婚")], valid) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("offline"), TimeoutError("timeout")])
async def test_verification_failure_is_closed_without_profile_write(profiles, failure):
    processor = Processor([candidate()], failure=failure)
    assert await extract(profiles, [statement("我有妻子")], processor) == 0
    assert await profiles.get_profile(owner()) == []


@pytest.mark.asyncio
async def test_delete_during_verification_fences_late_result(profiles):
    await profiles.upsert_facts(owner(), [candidate()], source_memory_id=9)
    processor = Processor([candidate()], [verdict()])
    started, release = asyncio.Event(), asyncio.Event()
    original = processor._call_llm_with_retry

    async def pause(prompt, system_prompt, max_retries):
        if processor.calls:
            started.set()
            await release.wait()
        return await original(prompt, system_prompt, max_retries)

    processor._call_llm_with_retry = pause
    task = asyncio.create_task(extract(profiles, [statement("我有妻子")], processor))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert await profiles.delete_profile(owner()) == 1
        release.set()
        assert await asyncio.wait_for(task, 2) == 0
        assert await profiles.get_profile(owner()) == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["delete_source", "disable", "change_scope"])
async def test_verification_ticket_checks_source_disable_and_scope_changes(profiles, mutation):
    await profiles.upsert_facts(owner(), [candidate()], source_memory_id=9)
    before = (await profiles.list_for_web())["items"][0]
    processor = Processor([candidate()], [verdict()])
    entered, release = asyncio.Event(), asyncio.Event()
    original = processor._call_llm_with_retry

    async def paused(prompt, system_prompt, max_retries):
        if processor.calls:
            entered.set()
            await release.wait()
        return await original(prompt, system_prompt, max_retries)

    processor._call_llm_with_retry = paused
    pending = asyncio.create_task(extract(profiles, [statement("我有妻子")], processor))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if mutation == "delete_source":
            await profiles.delete_by_source_memory(10)
        else:
            key, value = ("enabled", False) if mutation == "disable" else ("scope_mode", "user")
            profiles.config_manager._config["user_profile"][key] = value
        release.set()
        assert await asyncio.wait_for(pending, 2) == 0
        after = (await profiles.list_for_web())["items"][0]
        assert after["revision"] == before["revision"]
        assert after["evidence"] == before["evidence"]
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_timeout_cancels_verifier_and_does_not_write(profiles):
    profiles.config_manager._config["user_profile"]["verification_timeout"] = .02
    processor = Processor([candidate()], [verdict()])
    original = processor._call_llm_with_retry
    cancelled = False

    async def slow(prompt, system_prompt, max_retries):
        nonlocal cancelled
        if processor.calls:
            try:
                await asyncio.sleep(1)
            finally:
                cancelled = True
        return await original(prompt, system_prompt, max_retries)

    processor._call_llm_with_retry = slow
    assert await extract(profiles, [statement("我有妻子")], processor) == 0
    assert cancelled and await profiles.get_profile(owner()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("timeout", "expected"), [
    (True, 45), (False, 45), (None, 45), (float("nan"), 45),
    (float("inf"), 45), (-1, 45), (0, 45), ("bad", 45),
    pytest.param(10**400, 45, id="overflow"), (2.5, 2.5), (999, 180),
])
async def test_raw_timeout_values_are_bounded_without_losing_real_facts(monkeypatch, timeout, expected):
    processor = SimpleNamespace(_call_llm_with_retry=Processor([], [verdict()])._call_llm_with_retry)
    seen = []

    async def bounded(_coroutine, timeout):
        seen.append(timeout)
        _coroutine.close()
        return json.dumps({"checks": [verdict()]})

    monkeypatch.setattr(profile_fact_verifier.asyncio, "wait_for", bounded)
    fact = candidate(message_ref="m1")
    assert await profile_fact_verifier.verify_candidates(processor, [fact], [], timeout=timeout) == [fact]
    assert seen == [expected]
