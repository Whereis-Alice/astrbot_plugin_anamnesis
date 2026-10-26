"""Administrator profile inputs cannot widen writes or corrupt review state."""

import pytest
import pytest_asyncio
from astrbot_plugin_anamnesis.core.page_api import PluginPageApi
from quart import Quart

from tests.test_page_api import FakePlugin
from tests.test_user_profile_ledger import current, event, fact

pytest_plugins = ["tests.test_user_profile_ledger"]


@pytest_asyncio.fixture
async def selected(manager):
    await manager.upsert_facts(event(), [fact()], source_memory_id=10)
    await manager.upsert_facts(
        event("bob"), [fact("other account")], source_memory_id=12
    )
    await manager.update_for_web(
        manager.scope_for_event(event()), "drink", {"display_label": "Readable label"}
    )
    await manager.upsert_facts(event(), [fact("coffee")], source_memory_id=11)
    return manager, await current(manager)


async def snapshot(manager):
    result = {}
    for table in (
        "user_profiles",
        "user_profile_claims",
        "user_profile_evidence",
        "user_profile_transitions",
    ):
        async with manager.store.connection.execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        ) as cursor:
            result[table] = [dict(row) for row in await cursor.fetchall()]
    return result


async def call_web(manager, endpoint, payload):
    plugin = FakePlugin(memory_engine=object())
    plugin.user_profile_manager = manager
    api = PluginPageApi(plugin)
    app = Quart(__name__)
    async with app.test_request_context(
        "/profiles/" + endpoint, method="POST", json=payload
    ):
        return await getattr(api, endpoint + "_profile")()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct", "api"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_scope", {"scope": "bad"}),
        ("profile_key", ["drink"]),
        ("action", {"accept": True}),
        ("claim_id", True),
        ("claim_id", 0),
        ("claim_id", -1),
        ("claim_id", 1.5),
        pytest.param("claim_id", 2**63, id="claim-sqlite-overflow"),
        pytest.param("claim_id", -(2**63) - 1, id="claim-negative-overflow"),
        ("expected_revision", True),
        ("expected_revision", 0),
        ("expected_revision", -1),
        ("expected_revision", 1.5),
        pytest.param("expected_revision", 10**100, id="revision-overflow"),
    ],
)
async def test_malformed_review_parameters_refuse_without_errors_or_writes(
    selected, route, field, value
):
    manager, row = selected
    payload = {
        "profile_scope": row["profile_scope"],
        "profile_key": "drink",
        "action": "accept",
        "claim_id": row["pending"][0]["claim_id"],
        "expected_revision": row["revision"],
        field: value,
    }
    before = await snapshot(manager)
    if route == "api":
        assert (await call_web(manager, "review", payload))["status"] == "error"
    else:
        assert (
            await manager.review_for_web(
                payload.pop("profile_scope"), payload.pop("profile_key"), **payload
            )
            is None
        )
    assert await snapshot(manager) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct", "api"])
@pytest.mark.parametrize("label", [True, 123, ["new label"], {"label": "new label"}])
async def test_invalid_label_cannot_clear_label_or_write_other_fields(
    selected, route, label
):
    manager, row = selected
    updates = {
        "display_label": label,
        "expires_at": 2000000000.0,
        "expected_revision": row["revision"],
    }
    before = await snapshot(manager)
    if route == "api":
        result = await call_web(
            manager,
            "update",
            {
                "profile_scope": row["profile_scope"],
                "profile_key": "drink",
                **updates,
            },
        )
        assert result["status"] == "error"
    else:
        assert (
            await manager.update_for_web(row["profile_scope"], "drink", updates) is None
        )
    assert await snapshot(manager) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct", "api"])
@pytest.mark.parametrize("endpoint", ["update", "delete"])
@pytest.mark.parametrize("key", [1, True])
async def test_selector_number_is_not_coerced_to_a_real_profile_key(
    selected, route, endpoint, key
):
    manager, row = selected
    await manager.upsert_facts(
        event(), [fact(key=str(key).casefold())], source_memory_id=13
    )
    before = await snapshot(manager)
    if route == "api":
        result = await call_web(
            manager,
            endpoint,
            {
                "profile_scope": row["profile_scope"],
                "profile_key": key,
                "display_label": "Bad write",
            },
        )
        assert result["status"] == "error"
    elif endpoint == "delete":
        assert await manager.delete_for_web(row["profile_scope"], key) == 0
    else:
        assert (
            await manager.update_for_web(
                row["profile_scope"], key, {"display_label": "Bad write"}
            )
            is None
        )
    assert await snapshot(manager) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct", "api"])
@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (None, None),
        ("", None),
        ("  Chosen label  ", "Chosen label"),
        ("None", "None"),
        ("unknown", "unknown"),
        ("N/A", "N/A"),
    ],
)
async def test_valid_label_text_and_explicit_clear_remain_available(
    selected, route, label, expected
):
    manager, row = selected
    updates = {
        "display_label": label,
        "expires_at": None,
        "expected_revision": row["revision"],
    }
    if route == "api":
        response = await call_web(
            manager,
            "update",
            {
                "profile_scope": row["profile_scope"],
                "profile_key": "drink",
                **updates,
            },
        )
        assert response["status"] == "ok"
        result = response["data"]
    else:
        result = await manager.update_for_web(row["profile_scope"], "drink", updates)
    assert result["display_label"] == expected and result["expires_at"] is None
    assert result["revision"] == row["revision"] + 1 and result["manual_override"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct", "api"])
async def test_valid_revision_and_claim_accept_the_exact_selected_proposal(
    selected, route
):
    manager, row = selected
    payload = {
        "profile_scope": row["profile_scope"],
        "profile_key": "drink",
        "action": "accept",
        "claim_id": row["pending"][0]["claim_id"],
        "expected_revision": row["revision"],
    }
    if route == "api":
        response = await call_web(manager, "review", payload)
        assert response["status"] == "ok"
        result = response["data"]
    else:
        result = await manager.review_for_web(
            payload.pop("profile_scope"), payload.pop("profile_key"), **payload
        )
    assert result["value"] == "coffee" and not result["pending"]
    assert result["revision"] == row["revision"] + 1
    assert (await current(manager, event("bob")))["value"] == "other account"
