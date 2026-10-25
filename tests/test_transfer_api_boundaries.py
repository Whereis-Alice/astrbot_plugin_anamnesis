"""Portable transfers validate inputs before changing durable SQLite state."""

import csv
import json

import pytest
from astrbot_plugin_anamnesis.core.memory_source import serialize_source_messages
from quart import Quart

from tests.test_memory_transfer_runtime_safety import _api, _engine

pytest_plugins = ["tests.test_memory_administration"]
APP = Quart(__name__)
SCOPE = "qq:GroupMessage:1"


async def request_transfer(engine, action, body):
    async with APP.test_request_context(
        f"/memories/{action}", method="POST", json=body
    ):
        return await getattr(_api(engine), f"{action}_memories")()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["import", "export"])
@pytest.mark.parametrize("body", [True, False, "invalid", "", [1], [], 1, 0])
async def test_non_object_json_request_is_an_api_error_without_writes(
    administration, action, body
):
    response = await request_transfer(administration.engine, action, body)
    assert response["status"] == "error" and "JSON 对象" in response["message"]
    assert await administration.engine.get_memory_transfer_records() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection",
    [[True], [False], [1.0], [1.9], [float("inf")], [10**100], [0], [-1], [{}], [[]]],
)
async def test_invalid_export_ids_do_not_select_a_different_memory(
    administration, selection
):
    engine = administration.engine
    memory_id = await engine.add_memory("exact fact", session_id=SCOPE)
    response = await request_transfer(engine, "export", {"memory_ids": selection})
    assert response["status"] == "error" and "正整数" in response["message"]
    assert [
        row["original_id"] for row in await engine.get_memory_transfer_records()
    ] == [memory_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [[1], ["1"], ["+1"], [" 1 "], ["001"]])
async def test_export_retains_compatible_integer_strings(administration, selection):
    engine = administration.engine
    memory_id = await engine.add_memory("exact fact", session_id=SCOPE)
    assert memory_id == 1
    response = await request_transfer(engine, "export", {"memory_ids": selection})
    assert response["status"] == "ok" and response["data"]["memory_count"] == 1
    assert (
        json.loads(response["data"]["content"])["memories"][0]["original_id"]
        == memory_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [[], [2**63 - 1]])
async def test_valid_empty_or_unmatched_export_selection_stays_empty(
    administration, selection
):
    await administration.engine.add_memory("fact", session_id=SCOPE)
    response = await request_transfer(
        administration.engine, "export", {"memory_ids": selection}
    )
    assert response["status"] == "ok" and response["data"]["memory_count"] == 0


def invalid_number_record(case):
    record = {"content": "invalid fact", "session_id": SCOPE}
    if case == "importance_overflow":
        record["importance"] = 10**400
        path = "importance"
    elif case == "importance_nonfinite":
        record["importance"] = float("inf")
        path = "importance"
    elif case == "importance_string_nonfinite":
        record["importance"] = "Infinity"
        path = "importance"
    elif case == "metadata_nested":
        record["metadata"] = {"extra": {"weights": [float("inf")]}}
        path = "metadata.extra.weights[0]"
    elif case == "atom_metadata_nested":
        record["atoms"] = [
            {
                "content": "invalid fact",
                "expires_at": 100,
                "metadata": {"extra": [float("inf")]},
            }
        ]
        path = "atoms[0].metadata.extra[0]"
    else:
        value = {
            "create_time_nonfinite": float("inf"),
            "create_time_overflow": 10**400,
            "create_time_string_nonfinite": "Infinity",
        }[case]
        record["metadata"] = {"create_time": value}
        path = "metadata.create_time"
    return record, path


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "case",
    [
        "importance_overflow",
        "importance_nonfinite",
        "importance_string_nonfinite",
        "metadata_nested",
        "atom_metadata_nested",
        "create_time_nonfinite",
        "create_time_overflow",
        "create_time_string_nonfinite",
    ],
)
async def test_invalid_import_numbers_are_row_errors_before_durable_writes(
    administration, case, dry_run
):
    record, path = invalid_number_record(case)
    content = json.dumps(
        [{"content": "valid fact", "session_id": SCOPE}, record]
    ).replace(": Infinity", ": 1e309")
    response = await request_transfer(
        administration.engine, "import", {"content": content, "dry_run": dry_run}
    )
    assert response["status"] == "ok"
    result = response["data"]
    assert result["errors"][0]["index"] == 1 and path in result["errors"][0]["error"]
    records = await administration.engine.get_memory_transfer_records()
    if dry_run:
        assert result["valid_count"] == 1 and result["invalid_count"] == 1
        assert result["planned_import_count"] == 1 and records == []
    else:
        assert result["imported_count"] == 1 and result["failed_count"] == 1
        assert [row["content"] for row in records] == ["valid fact"]
        deleted = await administration.delete(result["imported_ids"], actor="webui:a")
        restored = await administration.restore(
            deleted["operation_id"], actor="webui:a"
        )
        assert restored["ids"] == result["imported_ids"] and restored["conflicts"] == []
    async with administration.engine.db_connection.execute(
        "SELECT COUNT(*) FROM memory_write_ops WHERE status IN ('pending','needs_repair')"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_import_keeps_legal_large_metadata_numbers_and_text_fallbacks(
    administration,
):
    metadata = {
        "create_time": "unknown",
        "extra": {"integer": 10**400, "finite": 1e308},
    }
    response = await request_transfer(
        administration.engine,
        "import",
        {
            "content": json.dumps(
                [
                    {
                        "content": "valid fact",
                        "session_id": SCOPE,
                        "importance": "unknown",
                        "metadata": metadata,
                    }
                ]
            ),
            "dry_run": False,
        },
    )
    assert (
        response["data"]["imported_count"] == 1
        and response["data"]["failed_count"] == 0
    )
    record = (await administration.engine.get_memory_transfer_records())[0]
    assert record["metadata"]["extra"] == metadata["extra"]
    assert record["importance"] == 0.5 and isinstance(
        record["metadata"]["create_time"], float
    )
    deleted = await administration.delete(
        response["data"]["imported_ids"], actor="webui:a"
    )
    assert (await administration.restore(deleted["operation_id"], actor="webui:a"))[
        "ids"
    ] == response["data"]["imported_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["metadata", "source_messages"])
@pytest.mark.parametrize("fmt", ["json", "csv"])
async def test_native_export_with_large_cells_imports_and_preserves_source(
    administration, tmp_path, field, fmt
):
    engine = administration.engine
    text = "x" * 140000
    memory_id = await engine.add_memory(
        "normal fact",
        session_id=SCOPE,
        metadata={"note": text} if field == "metadata" else {},
        source_messages=serialize_source_messages(
            [
                {
                    "role": "user",
                    "content": text,
                    "session_id": SCOPE,
                    "sender_id": "user",
                    "platform": "import",
                }
            ]
        )
        if field == "source_messages"
        else None,
    )
    original = (await engine.get_memory_transfer_records([memory_id]))[0]
    exported = await request_transfer(
        engine, "export", {"format": fmt, "memory_ids": [memory_id]}
    )
    assert exported["status"] == "ok"
    target = await _engine(tmp_path / "target.db")
    previous_limit = csv.field_size_limit()
    try:
        imported = await request_transfer(
            target,
            "import",
            {"format": fmt, "content": exported["data"]["content"], "dry_run": False},
        )
        assert (
            imported["data"]["imported_count"] == 1
            and imported["data"]["failed_count"] == 0
        )
        current = (await target.get_memory_transfer_records())[0]
        assert current["source_messages"] == original["source_messages"]
        assert current["metadata"].get("note") == original["metadata"].get("note")
        assert csv.field_size_limit() == previous_limit
    finally:
        await target.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content, expected",
    [
        ('content\n"unterminated', "CSV 解析失败"),
        ("anamnesis_csv_encoding,content\nfuture-v2,fact", "编码版本"),
    ],
)
async def test_csv_parse_error_restores_the_process_limit_and_does_not_write(
    administration, content, expected
):
    original_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(4096)
        response = await request_transfer(
            administration.engine,
            "import",
            {"format": "csv", "content": content, "dry_run": False},
        )
        assert response["status"] == "error" and expected in response["message"]
        assert csv.field_size_limit() == 4096
        assert await administration.engine.get_memory_transfer_records() == []
    finally:
        csv.field_size_limit(original_limit)
