"""Native transfer remains reversible and rejects invalid source numbers before writes."""

import csv
import io
import json

import pytest
from astrbot_plugin_anamnesis.core.memory_transfer import (
    CSV_SCALAR_ENCODING,
    CSV_SCALAR_ENCODING_FIELD,
)
from astrbot_plugin_anamnesis.core.page_api_modules.memory_handler import MemoryHandler
from astrbot_plugin_anamnesis.core.page_api_modules.utils import PageApiUtils
from quart import Quart

from tests.test_memory_transfer_runtime_safety import _engine

pytest_plugins = ["tests.test_memory_administration"]
ORIGIN = "qq:GroupMessage:1"
PREFIXES = (
    "'@quoted",
    "'=quoted",
    "'+quoted",
    "'-quoted",
    "'\tquoted-tab",
    "'\rquoted-cr",
    "''@double",
    "'plain quote",
    "@actual",
    "=actual",
    "+actual",
    "-actual",
    "plain text",
)


async def request_import(handler, app, engine, content, *, fmt="json", dry_run=False):
    async with app.test_request_context(
        "/memories/import",
        method="POST",
        json={
            "format": fmt,
            "content": content,
            "dry_run": dry_run,
        },
    ):
        return await handler.import_memories(engine, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["content", "session_id", "persona_id"])
async def test_native_csv_round_trip_preserves_every_literal_prefix_and_scope(
    administration,
    tmp_path,
    field,
):
    engine = administration.engine
    ids, expected = [], {}
    for index, text in enumerate(PREFIXES):
        content = text if field == "content" else f"fact {index}"
        scope = text if field == "session_id" else ORIGIN
        persona = text if field == "persona_id" else "p"
        memory_id = await engine.add_memory(
            content,
            session_id=scope,
            persona_id=persona,
            metadata={"source_session_id": scope, "key_facts": [content]},
            source_messages=[{"role": "user", "content": content}],
        )
        ids.append(memory_id)
        expected[content] = (scope, persona)
    handler, app = MemoryHandler(PageApiUtils()), Quart(__name__)
    target = await _engine(tmp_path / "target.db")
    try:
        async with app.test_request_context(
            "/memories/export", method="POST", json={"format": "csv", "memory_ids": ids}
        ):
            exported = await handler.export_memories(engine)
        assert exported["status"] == "ok" and exported["data"]["memory_count"] == len(
            ids
        )
        rows = list(csv.DictReader(io.StringIO(exported["data"]["content"])))
        assert all(
            row[CSV_SCALAR_ENCODING_FIELD] == CSV_SCALAR_ENCODING for row in rows
        )
        for row, original in zip(rows, PREFIXES, strict=True):
            if original.startswith(("'", "=", "+", "-", "@", "\t", "\r")):
                assert row[field] == "'" + original
            else:
                assert row[field] == original
        imported = await request_import(
            handler, app, target, exported["data"]["content"], fmt="csv"
        )
        assert imported["status"] == "ok"
        assert (
            imported["data"]["imported_count"] == len(ids)
            and imported["data"]["failed_count"] == 0
        )
        records = await target.get_memory_transfer_records()
        assert len(records) == len(expected)
        for record in records:
            assert (record["session_id"], record["persona_id"]) == expected[
                record["content"]
            ]
            assert record["metadata"]["key_facts"] == [record["content"]]
            assert record["source_messages"][0]["content"] == record["content"]
    finally:
        await target.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,expected",
    [
        ("'=old protected formula", "=old protected formula"),
        ("''@literal old text", "''@literal old text"),
    ],
)
async def test_unmarked_csv_retains_its_existing_decode_contract(
    administration, value, expected
):
    data = io.StringIO(newline="")
    writer = csv.DictWriter(data, fieldnames=["content", "session_id", "persona_id"])
    writer.writeheader()
    writer.writerow({"content": value, "session_id": ORIGIN, "persona_id": "p"})
    handler, app = MemoryHandler(PageApiUtils()), Quart(__name__)
    result = await request_import(
        handler, app, administration.engine, data.getvalue(), fmt="csv"
    )
    assert result["data"]["imported_count"] == 1
    current = await administration.snapshot(result["data"]["imported_ids"][0])
    assert current["text"] == expected


@pytest.mark.asyncio
async def test_unknown_csv_encoding_is_rejected_without_guessing_or_writing(
    administration,
):
    data = io.StringIO(newline="")
    writer = csv.DictWriter(data, fieldnames=[CSV_SCALAR_ENCODING_FIELD, "content"])
    writer.writeheader()
    writer.writerow({CSV_SCALAR_ENCODING_FIELD: "future-v2", "content": "'quoted"})
    result = await request_import(
        MemoryHandler(PageApiUtils()),
        Quart(__name__),
        administration.engine,
        data.getvalue(),
        fmt="csv",
    )
    assert result["status"] == "error" and "编码版本" in result["message"]
    assert await administration.engine.get_memory_transfer_records() == []


def source_payload(case):
    source = {"role": "user", "content": "exact source", "timestamp": 123.125}
    path = "source_messages[0].timestamp"
    if case == "timestamp_overflow":
        source["timestamp"] = "NUMERIC_OVERFLOW"
    elif case == "timestamp_string":
        source["timestamp"] = "Infinity"
    elif case == "timestamp_big_integer":
        source["timestamp"] = 10**400
    elif case == "nested_metadata":
        source["metadata"] = {"context": [{"weight": "NUMERIC_OVERFLOW"}]}
        path = "source_messages[0].metadata.context[0].weight"
    elif case == "nested_discarded_field":
        source["external"] = {"nested": ["NUMERIC_OVERFLOW"]}
        path = "source_messages[0].external.nested[0]"
    else:
        source["group_id"] = "NUMERIC_OVERFLOW"
        path = "source_messages[0].group_id"
    payload = [
        {"content": "valid fact", "session_id": ORIGIN, "persona_id": "p"},
        {
            "content": "invalid source fact",
            "session_id": ORIGIN,
            "persona_id": "p",
            "source_messages": [source],
        },
    ]
    return json.dumps(payload).replace('"NUMERIC_OVERFLOW"', "1e309"), path


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "case",
    [
        "timestamp_overflow",
        "timestamp_string",
        "timestamp_big_integer",
        "nested_metadata",
        "nested_discarded_field",
        "group_id",
    ],
)
async def test_import_rejects_nonfinite_source_at_its_row_and_field_before_writes(
    administration,
    dry_run,
    case,
):
    payload, path = source_payload(case)
    handler, app = MemoryHandler(PageApiUtils()), Quart(__name__)
    response = await request_import(
        handler, app, administration.engine, payload, dry_run=dry_run
    )
    assert response["status"] == "ok"
    result = response["data"]
    assert result["errors"][0]["index"] == 1 and path in result["errors"][0]["error"]
    assert (
        "非有限" in result["errors"][0]["error"]
        or "溢出" in result["errors"][0]["error"]
    )
    records = await administration.engine.get_memory_transfer_records()
    if dry_run:
        assert result["valid_count"] == 1 and result["invalid_count"] == 1
        assert result["planned_import_count"] == 1 and records == []
    else:
        assert result["failed_count"] == 1 and result["imported_count"] == 1
        assert [record["content"] for record in records] == ["valid fact"]
        memory_id = result["imported_ids"][0]
        deleted = await administration.delete([memory_id], actor="webui:a")
        assert deleted["deleted_ids"] == [memory_id]
        assert (await administration.restore(deleted["operation_id"], actor="webui:a"))[
            "ids"
        ] == [memory_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["json", "csv"])
async def test_finite_source_times_and_json_fields_keep_round_trip_and_recovery(
    administration,
    fmt,
):
    sources = [
        {
            "role": "user",
            "content": f"source {index}",
            "timestamp": timestamp,
            "id": index,
            "sender_id": "alice",
            "sender_name": "Alice",
            "group_id": "1",
            "platform": "qq",
            "metadata": {"is_bot_message": False, "finite_extra": {"weight": -0.25}},
        }
        for index, timestamp in enumerate((0, -1, 123.125, "123.125", "unknown"))
    ]
    payload = [
        {
            "content": "finite source fact",
            "session_id": ORIGIN,
            "persona_id": "p",
            "source_messages": sources,
        }
    ]
    handler, app = MemoryHandler(PageApiUtils()), Quart(__name__)
    response = await request_import(
        handler, app, administration.engine, json.dumps(payload)
    )
    assert (
        response["data"]["imported_count"] == 1
        and response["data"]["failed_count"] == 0
    )
    memory_id = response["data"]["imported_ids"][0]
    before = await administration.snapshot(memory_id)
    assert [source["timestamp"] for source in before["source_messages"]] == [
        0.0,
        -1.0,
        123.125,
        123.125,
        0.0,
    ]
    async with app.test_request_context(
        "/memories/export",
        method="POST",
        json={"format": fmt, "memory_ids": [memory_id]},
    ):
        exported = await handler.export_memories(administration.engine)
    assert exported["status"] == "ok"
    if fmt == "json":

        def invalid_constant(value):
            raise AssertionError(value)

        json.loads(exported["data"]["content"], parse_constant=invalid_constant)
    imported = await request_import(
        handler, app, administration.engine, exported["data"]["content"], fmt=fmt
    )
    assert (
        imported["data"]["skipped_duplicate_count"] == 1
        and imported["data"]["failed_count"] == 0
    )
    deleted = await administration.delete([memory_id], actor="webui:a")
    restored = await administration.restore(deleted["operation_id"], actor="webui:a")
    assert restored["ids"] == [memory_id] and restored["conflicts"] == []
    assert (await administration.snapshot(memory_id))["source_messages"] == before[
        "source_messages"
    ]
