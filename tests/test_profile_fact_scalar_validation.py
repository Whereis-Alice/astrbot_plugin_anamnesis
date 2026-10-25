"""Malformed LLM facts cannot become permanent user-profile text."""

import json
from types import SimpleNamespace

import pytest

from tests.test_profile_migration_followup import fact, owner

pytest_plugins = ["tests.test_profile_observation_lifecycle"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", None),
        ("key", True),
        ("value", None),
        ("value", True),
        ("value", 123),
        ("value", ["tea"]),
        ("value", {"drink": "tea"}),
        ("confidence", True),
        pytest.param("confidence", 10**400, id="confidence-overflow"),
    ],
)
async def test_malformed_fact_does_not_hide_valid_fact_or_create_ledger(
    system, field, value
):
    engine, profiles = system
    valid = fact("喜欢读书")
    valid["key"] = "books"
    malformed = {**fact(), field: value}
    source_text = valid["evidence"] + "。" + malformed["evidence"]
    memory_id = await engine.add_memory(
        content=source_text, session_id=owner().unified_msg_origin, persona_id=None
    )

    async def llm(*_args, **_kwargs):
        return json.dumps({"facts": [malformed, valid]}, ensure_ascii=False)

    assert (
        await profiles.update_from_messages(
            owner(),
            [{"role": "user", "sender_id": "alice", "content": source_text}],
            SimpleNamespace(_call_llm_with_retry=llm),
            source_memory_id=memory_id,
        )
        == 1
    )
    rows = await profiles.get_profile(owner())
    assert [(row["profile_key"], row["value"]) for row in rows] == [
        ("books", "喜欢读书")
    ]
    for table in ("user_profiles", "user_profile_claims"):
        async with profiles.store.connection.execute(
            f"SELECT profile_key FROM {table}"
        ) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ["books"]
    injected = await profiles.format_for_injection(owner())
    assert "喜欢读书" in injected and "None" not in injected


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "confidence"), [("123", "0.95"), ("None", 0.95)])
async def test_literal_text_and_legacy_numeric_confidence_remain_valid(
    system, value, confidence
):
    engine, profiles = system
    observation = {**fact(value), "confidence": confidence, "key": " CHOICE "}
    memory_id = await engine.add_memory(
        content=observation["evidence"],
        session_id=owner().unified_msg_origin,
        persona_id=None,
    )
    assert (
        await profiles.upsert_facts(owner(), [observation], source_memory_id=memory_id)
        == 1
    )
    rows = await profiles.get_profile(owner())
    assert rows[0]["profile_key"] == "choice" and rows[0]["value"] == value
    assert rows[0]["confidence"] == 0.95


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("value", True),
        ("value", 123),
        ("value", ["tea"]),
        ("value", {"drink": "tea"}),
        ("confidence", True),
        pytest.param("confidence", 10**400, id="web-confidence-overflow"),
        ("expires_at", True),
        pytest.param("expires_at", 10**400, id="web-expiry-overflow"),
    ],
)
async def test_web_edit_rejects_invalid_scalars_without_changing_history(
    system, field, value
):
    engine, profiles = system
    observation = fact()
    memory_id = await engine.add_memory(
        content=observation["evidence"],
        session_id=owner().unified_msg_origin,
        persona_id=None,
    )
    await profiles.upsert_facts(owner(), [observation], source_memory_id=memory_id)
    before = await profiles.get_profile(owner())
    assert (
        await profiles.update_for_web(
            before[0]["profile_scope"],
            before[0]["profile_key"],
            {field: value, "expected_revision": before[0]["revision"]},
        )
        is None
    )
    assert await profiles.get_profile(owner()) == before
