"""Group provenance for the dashboard, independent of recall scope."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

_GROUP_MARKER = ":GroupMessage:"
_MAX_GROUP_KEY_LENGTH = 512
# Use the same boundary whitespace as str.strip() on the independent SQLite connections.
_BOUNDARY_WHITESPACE = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
_SQL_WHITESPACE = "CHAR(" + ",".join(str(ord(char)) for char in _BOUNDARY_WHITESPACE) + ")"


def parse_group_key(value: object) -> tuple[str, str] | None:
    """Split an AstrBot group key into adapter instance ID and group ID."""
    if not isinstance(value, str) or len(value) > _MAX_GROUP_KEY_LENGTH:
        return None
    platform_id, separator, suffix = value.partition(":")
    group_id = suffix.removeprefix("GroupMessage:")
    if (
        separator
        and group_id != suffix
        and platform_id
        and group_id
        and platform_id == platform_id.strip(_BOUNDARY_WHITESPACE)
        and group_id == group_id.strip(_BOUNDARY_WHITESPACE)
        and "\x00" not in value
    ):
        return platform_id, group_id
    return None


def group_metadata_for_event(event: Any) -> dict[str, str]:
    """Capture the actual group ID before a per-sender session can obscure it."""
    parsed = parse_group_key(getattr(event, "unified_msg_origin", None))
    if not parsed:
        return {}
    platform_id, _ = parsed
    getter = getattr(event, "get_group_id", None)
    group = getattr(getattr(event, "message_obj", None), "group", None)
    group_id = getattr(group, "group_id", None)
    if not isinstance(group_id, (str, int)) and callable(getter):
        try:
            group_id = getter()
        except Exception:
            return {}
    if not isinstance(group_id, (str, int)) or isinstance(group_id, bool):
        return {}
    key = f"{platform_id}{_GROUP_MARKER}{str(group_id).strip()}"
    if not parse_group_key(key):
        return {}
    result = {"source_group": key}
    group_name = getattr(group, "group_name", None)
    if isinstance(group_name, str) and (group_name := group_name.strip()[:256]):
        result["source_group_name"] = group_name
    return result


def merge_group_metadata(records: Iterable[object]) -> dict[str, str | None]:
    """Keep one group and its latest available label from newest-first records."""
    merged: dict[str, str | None] = {}
    for metadata in records:
        if not isinstance(metadata, dict) or "source_group" not in metadata:
            continue
        key = metadata["source_group"]
        if key is None:
            return {"source_group": None}
        if not parse_group_key(key):
            continue
        if merged.setdefault("source_group", key) != key:
            return {"source_group": None}
        name = metadata.get("source_group_name")
        if isinstance(name, str) and name:
            merged.setdefault("source_group_name", name)
    return merged


def group_metadata_from_messages(messages: list[Any]) -> dict[str, str | None]:
    """Recover group data stored alongside a conversation window."""
    return merge_group_metadata(
        getattr(message, "metadata", None) for message in reversed(messages)
    )


def _origin_matches(origin: str, marker: str) -> str:
    return (
        f"({origin} GLOB '?*{marker}?*' "
        f"AND INSTR({origin}, ':') = INSTR({origin}, '{marker}'))"
    )


def _group_matches(origin: str) -> str:
    marker_start = f"INSTR({origin}, ':')"
    platform = f"SUBSTR({origin}, 1, {marker_start} - 1)"
    group = f"SUBSTR({origin}, {marker_start} + {len(_GROUP_MARKER)})"
    return (
        f"{_origin_matches(origin, _GROUP_MARKER)} "
        f"AND LENGTH({origin}) <= {_MAX_GROUP_KEY_LENGTH} "
        f"AND INSTR({origin}, CHAR(0)) = 0 "
        f"AND {platform} = TRIM({platform}, {_SQL_WHITESPACE}) "
        f"AND {group} = TRIM({group}, {_SQL_WHITESPACE})"
    )


def _source_group_sql() -> str:
    explicit_group = "json_extract(metadata, '$.source_group')"
    # The source fields win over the recall/storage scope, even for private chats.
    origins = (
        "json_extract(metadata, '$.source_session_id')",
        "json_extract(metadata, '$.source_window.session_id')",
        "json_extract(metadata, '$.import_provenance.source_window.session_id')",
        "json_extract(metadata, '$.session_id')",
    )
    branches = [
        # An explicit JSON null identifies a summary built from mixed groups.
        "WHEN json_type(metadata, '$.source_group') = 'null' THEN NULL",
        f"WHEN {_group_matches(explicit_group)} THEN {explicit_group}",
    ]
    for origin in origins:
        # AstrBot's older OneBot group sessions may encode numeric sender_group.
        # Only recognize a pair of numeric identifiers so opaque IDs keep their delimiters.
        prefix_length = f"INSTR({origin}, ':') + {len(_GROUP_MARKER) - 1}"
        sid = f"SUBSTR({origin}, {prefix_length} + 1)"
        separator = f"INSTR({sid}, '_')"
        sender = f"SUBSTR({sid}, 1, {separator} - 1)"
        group = f"SUBSTR({sid}, {separator} + 1)"
        legacy_group = (
            f"CASE WHEN {separator} > 1 AND {group} <> '' "
            f"AND {sender} NOT GLOB '*[^0-9]*' AND {group} NOT GLOB '*[^0-9]*' "
            f"THEN SUBSTR({origin}, 1, {prefix_length}) || {group} ELSE {origin} END"
        )
        branches.extend((
            f"WHEN {_group_matches(origin)} THEN {legacy_group}",
            f"WHEN {_origin_matches(origin, ':FriendMessage:')} "
            f"OR {_origin_matches(origin, ':OtherMessage:')} THEN NULL",
        ))
    return "CASE WHEN json_valid(metadata) THEN CASE " + " ".join(branches) + " END END"


# Shared by the expression index, dashboard queries, and transfer queries.
MEMORY_GROUP_SQL = _source_group_sql()
