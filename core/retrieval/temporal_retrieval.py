"""Scoped direct time lookup and bounded time/semantic candidate fusion.

The direct path queries the main SQLite documents table before selecting a
candidate pool. It shares the engine's current-state and atom TTL checks.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from os import PathLike

import aiosqlite
from astrbot.api import logger

from ...storage.sqlite_utils import configure_sqlite_connection, sqlite_connect_kwargs
from ..recall_persona import (
    NO_PERSONA,
    atom_persona_sql_match,
    exact_null_persona_scope,
    persona_matches,
    persona_sql_filter,
)
from .hybrid_retriever import HybridResult
from .temporal_query import (
    TimeRange,
    memory_time_bounds,
    parse_time_range,
    parse_timestamp,
    range_overlaps,
    speaker_matches,
)


def _option(config, engine, name, default):
    if config is not None:
        return config.get("recall_engine." + name, default)
    return (getattr(engine, "config", None) or {}).get(name, default)


def _metadata(value):
    if isinstance(value, dict):
        return value
    try:
        value = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _time_only_query(query, time_range):
    if not query:
        return True
    if (
        not time_range
        or not time_range.expression
        or time_range.expression == "explicit"
    ):
        return False
    residual = query.replace(time_range.expression, "")
    for phrase in (
        "讨论了什么",
        "发生了什么",
        "聊了什么",
        "说了什么",
        "有哪些事情",
        "有哪些事",
        "重要事情",
        "重要的事情",
        "有什么",
        "做了什么",
        "这个群",
        "本群",
        "群里",
        "我们",
        "大家",
        "帮我",
        "请",
        "回忆一下",
        "回忆",
        "看看",
        "的",
        "内容",
        "聊天",
        "记录",
        "总结一下",
        "总结",
    ):
        residual = residual.replace(phrase, "")
    return not re.sub(r"[\s，。！？?!.：:、]", "", residual)


def _calendar_recall_intent(query, time_range):
    """Don't turn '今天吃什么' into a search limited to today's memories."""
    if time_range.expression not in (
        "今天",
        "明天",
        "后天",
        "本周",
        "这周",
        "这个月",
        "本月",
        "今年",
    ):
        return True
    return any(
        word in query
        for word in (
            "聊",
            "说",
            "讨论",
            "回忆",
            "记忆",
            "发生",
            "做过",
            "去过",
            "安排",
            "计划",
            "约定",
            "记录",
            "历史",
            "之前",
            "曾经",
        )
    )


@asynccontextmanager
async def _read_connection(engine):
    path = str(getattr(engine, "db_path", "") or "")
    if path == ":memory:":
        connection = getattr(engine, "db_connection", None)
        if connection is None:
            raise ValueError("temporal database is not initialized")
        yield connection
        return
    if not path:
        raise ValueError("temporal database is not available")
    options = getattr(engine, "config", None) or {}
    async with aiosqlite.connect(path, **sqlite_connect_kwargs(options)) as connection:
        await configure_sqlite_connection(connection, options)
        yield connection


def _sql_timestamp(expression):
    # JSON numeric strings occur in legacy imports. Date strings must pass
    # through SQLite's UTC-aware date parser rather than CAST('2026-...' AS REAL).
    return (
        f"CASE WHEN typeof({expression}) IN ('integer','real') THEN {expression} "
        f"WHEN typeof({expression})='text' AND {expression} GLOB '[0-9]*' "
        f"AND {expression} NOT GLOB '*[^0-9.]*' THEN CAST({expression} AS REAL) "
        f"ELSE CAST(strftime('%s',{expression}) AS REAL) END"
    )


async def temporal_atom_evidence(
    engine, memory_id: int, time_range: TimeRange, metadata: dict
) -> dict | None:
    """Recheck currently live event atoms for a range-selected parent."""
    clauses = [
        "parent_memory_id=?",
        "status='active'",
        "CAST(expires_at AS REAL)>?",
        "CAST(expires_at AS REAL)<1e308",
        "typeof(event_time) IN ('real','integer')",
        "event_time>=0",
        "event_time<1e11",
    ]
    values = [int(memory_id), time.time()]
    if metadata.get("session_id") is not None:
        clauses.append("(session_id IS NULL OR session_id=?)")
        values.append(metadata["session_id"])
    clauses.append(atom_persona_sql_match("persona_id", "?"))
    values.extend([metadata.get("persona_id"), metadata.get("persona_id")])
    if time_range.start is not None:
        clauses.append("event_time>=?")
        values.append(time_range.start)
    if time_range.end is not None:
        clauses.append("event_time<?")
        values.append(time_range.end)
    parent_conditions = [
        "d.id=memory_atoms.parent_memory_id",
        "json_valid(d.metadata)",
        "COALESCE(json_extract(d.metadata,'$.status'),'active')='active'",
    ]
    for key in ("session_id", "persona_id"):
        parent_conditions.append(f"json_extract(d.metadata,'$.{key}') IS ?")
        values.append(metadata.get(key))
    clauses.append(
        "EXISTS (SELECT 1 FROM documents d WHERE "
        + " AND ".join(parent_conditions)
        + ")"
    )
    async with _read_connection(engine) as connection:
        async with connection.execute("PRAGMA table_info(memory_atoms)") as cursor:
            atom_type_column = (
                "atom_type" if any(row[1] == "atom_type" for row in await cursor.fetchall())
                else "NULL"
            )
        async with connection.execute(
            f"SELECT id,content,event_time,{atom_type_column} FROM memory_atoms WHERE "
            + " AND ".join(clauses)
            + " ORDER BY event_time DESC,id DESC",
            values,
        ) as cursor:
            rows = await cursor.fetchall()
    valid = [
        row
        for row in rows
        if isinstance(row[2], (int, float)) and math.isfinite(row[2])
    ]
    if not valid:
        return None
    evidence = {
        "content": "\n".join(dict.fromkeys(str(row[1]) for row in valid)),
        "retrieved_atom_ids": [int(row[0]) for row in valid],
        "temporal_atom_ids": [int(row[0]) for row in valid],
        "temporal_time_start": min(row[2] for row in valid),
        "temporal_time_end": max(row[2] for row in valid),
        "temporal_time_basis": "atom_event_time",
    }
    # Match the final range-selected content, rather than the parent-wide
    # lifecycle projection. Missing legacy types retain their existing policy.
    if all(row[3] is not None for row in valid):
        evidence["atom_types"] = list(dict.fromkeys(str(row[3]).casefold() for row in valid))
    return evidence


async def direct_time_candidates(
    engine,
    time_range: TimeRange,
    *,
    k: int,
    session_id=None,
    persona_id=None,
    persona_filtered=False,
    speaker=None,
    exclude_ids=None,
) -> list[HybridResult]:
    """Select matching ranges in SQLite, independently of semantic ranking.

    Each candidate is reloaded after the query, so deletion/archival or scope
    changes during selection cannot expose a stale row. Atom TTL filtering is
    performed before allocating the final Top-K in search_with_time.
    """
    excluded = list(dict.fromkeys(exclude_ids or []))
    expressions = {
        key: _sql_timestamp(f"json_extract(m,'$.{key}')")
        for key in ("event_time", "source_time_start", "source_time_end", "create_time")
    }
    start = "COALESCE(event_time,source_time_start,create_time)"
    end = "COALESCE(event_time,source_time_end,source_time_start,create_time)"
    conditions, values = ["COALESCE(json_extract(m,'$.status'),'active')='active'"], []
    if session_id is not None:
        conditions.append("json_extract(m,'$.session_id')=?")
        values.append(session_id)
    if persona_filtered or persona_id == NO_PERSONA:
        with exact_null_persona_scope(persona_id in (None, NO_PERSONA)):
            condition, args = persona_sql_filter(
                "json_extract(m,'$.persona_id')", persona_id
            )
        conditions.append(condition)
        values.extend(args)
    elif persona_id is not None:
        conditions.append("json_extract(m,'$.persona_id')=?")
        values.append(persona_id)
    time_conditions = []
    if time_range.start is not None:
        conditions.append(f"{end}>=?")
        values.append(time_range.start)
        time_conditions.append(f"{end}>=?")
    if time_range.end is not None:
        conditions.append(f"{start}<?")
        values.append(time_range.end)
        time_conditions.append(f"{start}<?")
    if excluded:
        conditions.append(f"id NOT IN ({','.join('?' for _ in excluded)})")
        values.extend(excluded)
    # Speaker matching is expressed inside SQL so unrelated speakers don't
    # consume the range candidate budget. json_each supports legacy string
    # participants and array/object identity representations.
    if speaker:
        conditions.append("""(
          EXISTS (SELECT 1 FROM json_each(m,'$.participants') p
                  WHERE lower(CAST(p.value AS TEXT))=lower(?))
          OR EXISTS (SELECT 1 FROM json_tree(m,'$.participant_identities') p
                     WHERE p.key IN ('sender_id','user_id','name','display_name','identity_key')
                     AND lower(CAST(p.value AS TEXT))=lower(?))
        )""")
        values.extend([speaker, speaker])
    statement = f"""
      WITH raw AS (
        SELECT id,text,CASE WHEN json_valid(metadata) THEN metadata ELSE '{{}}' END AS m
        FROM documents
      ), timed AS (
        SELECT id,text,m,{",".join(value + " AS " + key for key, value in expressions.items())}
        FROM raw
      )
      SELECT id,text,m FROM timed WHERE {" AND ".join(conditions)}
      ORDER BY {start} DESC,id DESC LIMIT ?
    """
    values.append(int(k))
    async with _read_connection(engine) as connection:
        async with connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_atoms'"
        ) as cursor:
            has_atoms = await cursor.fetchone() is not None
        if has_atoms and getattr(engine, "atom_enabled", True):
            live_clause = f"""(
              EXISTS (SELECT 1 FROM memory_atoms a WHERE a.parent_memory_id=timed.id
                AND a.status='active' AND CAST(a.expires_at AS REAL)>? AND CAST(a.expires_at AS REAL)<1e308
                AND (a.session_id IS NULL OR json_extract(m,'$.session_id') IS NULL
                     OR a.session_id=json_extract(m,'$.session_id'))
                AND {atom_persona_sql_match("a.persona_id", "json_extract(m,'$.persona_id')")})
              OR (NOT EXISTS (SELECT 1 FROM memory_atoms a WHERE a.parent_memory_id=timed.id)
                  AND COALESCE(json_extract(m,'$.atom_types'),'') IN ('','[]'))
            )"""
            statement = statement.replace(
                "ORDER BY", "AND " + live_clause + " ORDER BY", 1
            )
            # The lifecycle predicate is inserted after all other filters and
            # before LIMIT. Expired rows cannot consume the candidate quota.
            values.insert(-1, time.time())
        async with connection.execute(statement, values) as cursor:
            rows = await cursor.fetchall()
        atom_rows = []
        if has_atoms and getattr(engine, "atom_enabled", True):
            # Actual event times may live on facts rather than the parent
            # window: a message written today can describe last week's event.
            atom_conditions = [
                condition
                for condition in conditions
                if condition not in time_conditions
            ]
            atom_values = []
            value_index = 0
            for condition in conditions:
                count = condition.count("?")
                if condition not in time_conditions:
                    atom_values.extend(values[value_index : value_index + count])
                value_index += count
            atom_conditions.extend(
                [
                    "a.status='active'",
                    "CAST(a.expires_at AS REAL)>?",
                    "CAST(a.expires_at AS REAL)<1e308",
                    "typeof(a.event_time) IN ('real','integer')",
                    "a.event_time>=0",
                    "a.event_time<1e11",
                    "(a.session_id IS NULL OR json_extract(m,'$.session_id') IS NULL OR a.session_id=json_extract(m,'$.session_id'))",
                    atom_persona_sql_match("a.persona_id", "json_extract(m,'$.persona_id')"),
                ]
            )
            atom_values.append(time.time())
            if time_range.start is not None:
                atom_conditions.append("a.event_time>=?")
                atom_values.append(time_range.start)
            if time_range.end is not None:
                atom_conditions.append("a.event_time<?")
                atom_values.append(time_range.end)
            # Qualified document IDs avoid ambiguity with atom IDs.
            atom_conditions = [
                condition.replace("id NOT IN", "d.id NOT IN")
                for condition in atom_conditions
            ]
            atom_statement = f"""WITH raw AS (
              SELECT id,text,CASE WHEN json_valid(metadata) THEN metadata ELSE '{{}}' END AS m FROM documents
            ) SELECT d.id,d.text,d.m,MIN(a.event_time),MAX(a.event_time) FROM raw d
              JOIN memory_atoms a ON a.parent_memory_id=d.id
              WHERE {" AND ".join(atom_conditions)} GROUP BY d.id
              ORDER BY MAX(a.event_time) DESC,d.id DESC LIMIT ?"""
            atom_values.append(int(k))
            async with connection.execute(atom_statement, atom_values) as cursor:
                atom_rows = await cursor.fetchall()
    atom_ids = {int(row[0]) for row in atom_rows}
    rows = [row for row in rows if int(row[0]) not in atom_ids] + atom_rows
    candidates = []
    for position, row in enumerate(rows):
        memory_id = int(row[0])
        loader = getattr(engine, "get_memory", None)
        stored = (
            await loader(memory_id)
            if callable(loader)
            else {"text": row[1], "metadata": row[2]}
        )
        if not stored:
            continue
        metadata = _metadata(stored.get("metadata"))
        if len(row) > 3:
            metadata = dict(
                metadata,
                temporal_time_start=row[3],
                temporal_time_end=row[4],
                temporal_time_basis="atom_event_time",
            )
        if not _matches(
            metadata, time_range, session_id, persona_id, persona_filtered, speaker
        ):
            continue
        score = 1.0 / (1.0 + position)
        candidates.append(
            HybridResult(
                memory_id,
                score,
                0.0,
                None,
                None,
                str(stored.get("text") or ""),
                metadata,
                {"temporal_rank": position + 1, "temporal_score": score},
            )
        )
    candidates.sort(
        key=lambda item: (retrieval_time_bounds(item.metadata)[0] or 0, item.doc_id),
        reverse=True,
    )
    return [
        replace(
            item,
            final_score=1.0 / rank,
            score_breakdown={"temporal_rank": rank, "temporal_score": 1.0 / rank},
        )
        for rank, item in enumerate(candidates[:k], 1)
    ]


def retrieval_time_bounds(metadata):
    if metadata.get("temporal_time_basis") == "atom_event_time":
        return (
            metadata.get("temporal_time_start"),
            metadata.get("temporal_time_end"),
            "atom_event_time",
        )
    return memory_time_bounds(metadata)


def _matches(metadata, time_range, session_id, persona_id, persona_filtered, speaker):
    if metadata.get("status", "active") != "active":
        return False
    if session_id is not None and metadata.get("session_id") != session_id:
        return False
    if (persona_filtered or persona_id is not None) and not persona_matches(
        metadata.get("persona_id"), persona_id
    ):
        return False
    if not speaker_matches(metadata, speaker):
        return False
    if time_range is not None:
        start, end, _ = retrieval_time_bounds(metadata)
        return range_overlaps(start, end, time_range.start, time_range.end)
    return True


async def _semantic_candidates(engine, query, cap, scope, persona, *, record_access):
    if record_access:
        return await engine.search_memories(
            query=query, k=cap, session_id=scope, persona_id=persona
        )
    # Evaluation reads through the same retrievers and policies, while
    # bypassing cache/access bookkeeping and migration scheduling.
    retriever = getattr(engine, "dual_route_retriever", None) or getattr(
        engine, "hybrid_retriever", None
    )
    if retriever is None:
        raise ValueError("read-only retrieval is not initialized")
    results = await retriever.search(query, cap, scope, persona)
    policy = getattr(engine, "_filter_by_retrieval_policy", None)
    if callable(policy):
        results = policy(results)
    atom_merge = getattr(engine, "merge_atom_recall_candidates", None)
    if callable(atom_merge):
        results = await atom_merge(query, results, cap, scope, persona)
    lifecycle = getattr(engine, "filter_recall_lifecycle", None)
    if callable(lifecycle):
        results = await lifecycle(results)
        if callable(policy):
            results = policy(results)
    recent = getattr(engine, "_merge_recent_memories", None)
    if callable(recent):
        results = await recent(results, cap, scope, persona)
    return results


async def search_with_time(
    memory_engine,
    query: str,
    k: int = 5,
    session_id=None,
    persona_id=None,
    *,
    config_manager=None,
    mode: str = "auto",
    start_time=None,
    end_time=None,
    speaker=None,
    exclude_ids=None,
    timeout=None,
    now: datetime | None = None,
    timezone_name: str | None = None,
    time_query: str | None = None,
    candidate_limit: int | None = None,
    record_access: bool = True,
    persona_filtered: bool | None = None,
) -> tuple[list[HybridResult], dict]:
    """Retrieve using semantic/time/hybrid/auto, with one optional total timeout.

    Callers remain responsible for event access authorization. Explicit scope
    and persona arguments must come from the event/admin selection, never an
    unconstrained model parameter. Empty semantic text is allowed only with a
    resolved time range. Candidate caps are reported to the caller.
    """
    if mode not in ("auto", "semantic", "time", "hybrid"):
        raise ValueError("mode must be auto, semantic, time or hybrid")
    if not isinstance(query, str) or len(query) > 2000:
        raise ValueError("query must contain at most 2000 characters")
    query = query.strip()
    enabled = bool(
        _option(config_manager, memory_engine, "temporal_retrieval_enabled", True)
    )
    zone = (
        timezone_name
        or _option(config_manager, memory_engine, "temporal_timezone", "")
        or "Asia/Shanghai"
    )
    after = parse_timestamp(start_time, timezone_name=zone)
    before = parse_timestamp(end_time, timezone_name=zone)
    time_range = (
        TimeRange(after, before, "explicit", zone)
        if after is not None or before is not None
        else None
    )
    if time_range is None and enabled and mode != "semantic":
        try:
            time_range = parse_time_range(
                query if time_query is None else time_query, now=now, timezone_name=zone
            )
        except ValueError:
            if mode != "auto":
                raise
            logger.debug("无法确定查询的时间范围，继续语义检索", exc_info=True)
        if (
            mode == "auto"
            and time_range
            and not _calendar_recall_intent(
                query if time_query is None else time_query, time_range
            )
        ):
            time_range = None
    if time_range and time_range.start is not None and time_range.end is not None:
        if time_range.start >= time_range.end:
            raise ValueError("start_time must be before end_time")
        max_days = int(
            _option(config_manager, memory_engine, "temporal_max_range_days", 3660)
        )
        if time_range.end - time_range.start > max(1, max_days) * 86400:
            raise ValueError("time range exceeds configured maximum")
    if mode in ("time", "hybrid") and not enabled:
        raise ValueError("temporal retrieval is disabled")
    if mode == "time" and time_range is None:
        raise ValueError("time mode requires a resolved time range")
    if not query and time_range is None:
        raise ValueError("query is empty and no time range was provided")
    actual_mode = mode
    if mode == "auto":
        actual_mode = (
            "time"
            if time_range
            and _time_only_query(
                query if time_query is None else time_query, time_range
            )
            else "hybrid"
            if time_range
            else "semantic"
        )
    elif mode == "hybrid" and time_range is None:
        actual_mode = "semantic"
    # Disabling natural-time routing keeps explicit legacy ISO filters working
    # on the semantic path, but never silently enables the direct time path.
    if not enabled:
        actual_mode = "semantic"
    if actual_mode == "semantic" and not query:
        raise ValueError("semantic mode requires recall keywords")
    cap = max(
        1,
        min(
            5000,
            int(
                candidate_limit
                if candidate_limit is not None
                else _option(
                    config_manager, memory_engine, "temporal_candidate_limit", 500
                )
                if time_range
                else k
            ),
        ),
    )
    excluded = set(exclude_ids or [])
    if len(excluded) > 500 or any(
        isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in excluded
    ):
        raise ValueError("exclude_ids must contain at most 500 positive integer IDs")
    if persona_filtered is None:
        persona_filtered = bool(
            config_manager
            and config_manager.filtering_settings.get("use_persona_filtering", True)
        )

    async def run():
        database_path = getattr(memory_engine, "db_path", None)
        direct_available = isinstance(database_path, (str, PathLike)) and bool(
            database_path
        )
        semantic_results, temporal_results = [], []
        if actual_mode == "time" and not direct_available:
            raise ValueError("temporal database is not available")
        tasks = []
        if actual_mode in ("semantic", "hybrid"):
            tasks.append(
                _semantic_candidates(
                    memory_engine,
                    query,
                    cap,
                    session_id,
                    persona_id,
                    record_access=record_access,
                )
            )
        if actual_mode in ("time", "hybrid") and direct_available:
            tasks.append(
                direct_time_candidates(
                    memory_engine,
                    time_range,
                    k=cap,
                    session_id=session_id,
                    persona_id=persona_id,
                    persona_filtered=persona_filtered,
                    speaker=speaker,
                    exclude_ids=excluded,
                )
            )
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        failures, failed_channels = [], []
        for position, value in enumerate(gathered):
            if isinstance(value, asyncio.CancelledError):
                raise value
            if isinstance(value, BaseException):
                failures.append(value)
                failed_channels.append(
                    "temporal" if actual_mode == "time" or position > 0 else "semantic"
                )
                gathered[position] = []
        if failures and (actual_mode != "hybrid" or len(failures) == len(tasks)):
            raise failures[0]
        if actual_mode == "time":
            temporal_results = gathered[0]
        else:
            semantic_results = gathered[0]
            if len(gathered) > 1:
                temporal_results = gathered[1]
        needs_recheck = bool(
            time_range
            or speaker
            or excluded
            or not record_access
            or persona_filtered and persona_id in (None, NO_PERSONA)
        )
        semantic_results = [
            item
            for item in semantic_results
            if item.doc_id not in excluded
            and (
                not needs_recheck
                or _matches(
                    _metadata(item.metadata),
                    time_range,
                    session_id,
                    persona_id,
                    persona_filtered,
                    speaker,
                )
            )
        ]
        if actual_mode == "hybrid" and temporal_results:
            # RRF rewards both channels without pretending a time rank is a
            # semantic score. Stable ties follow the newer temporal candidate.
            combined, scores = {}, {}
            for channel, results in (
                ("temporal", temporal_results),
                ("semantic", semantic_results),
            ):
                for rank, item in enumerate(results, 1):
                    combined.setdefault(item.doc_id, item)
                    scores[item.doc_id] = scores.get(item.doc_id, 0.0) + 1.0 / (
                        60 + rank
                    )
            memories = []
            for memory_id, item in combined.items():
                breakdown = dict(
                    item.score_breakdown or {}, temporal_fusion_score=scores[memory_id]
                )
                memories.append(
                    replace(
                        item, final_score=scores[memory_id], score_breakdown=breakdown
                    )
                )
            memories.sort(key=lambda item: item.final_score, reverse=True)
        else:
            memories = temporal_results if actual_mode == "time" else semantic_results
        temporal_marks = {
            item.doc_id: item.metadata
            for item in memories
            if _metadata(item.metadata).get("temporal_time_basis") == "atom_event_time"
        }
        policy = getattr(memory_engine, "_filter_by_retrieval_policy", None)
        if needs_recheck and callable(policy):
            memories = policy(memories)
        lifecycle = getattr(memory_engine, "filter_recall_lifecycle", None)
        if needs_recheck and callable(lifecycle):
            memories = await lifecycle(memories)
            if callable(policy):
                memories = policy(memories)
        if temporal_marks:
            refreshed = []
            for item in memories:
                if item.doc_id not in temporal_marks:
                    refreshed.append(item)
                    continue
                evidence = await temporal_atom_evidence(
                    memory_engine, item.doc_id, time_range, item.metadata
                )
                if evidence:
                    content = evidence.pop("content")
                    metadata = {
                        **item.metadata,
                        **evidence,
                        "persona_summary": content,
                        "canonical_summary": content,
                        "key_facts": content.splitlines(),
                    }
                    refreshed.append(replace(item, content=content, metadata=metadata))
            memories = refreshed
            if callable(policy):
                memories = policy(memories)
        if needs_recheck:
            memories = [
                item
                for item in memories
                if _matches(
                    _metadata(item.metadata),
                    time_range,
                    session_id,
                    persona_id,
                    persona_filtered,
                    speaker,
                )
            ]
        memories = memories[: max(1, min(int(k), cap))]
        if record_access and actual_mode in ("time", "hybrid"):
            schedule = getattr(memory_engine, "_schedule_access_updates", None)
            if callable(schedule):
                schedule([item.doc_id for item in memories])
        details = {
            "mode": actual_mode,
            "requested_mode": mode,
            "time_range": time_range.as_dict() if time_range else None,
            "candidate_limit": cap,
            "search_is_bounded": True,
            "direct_time_lookup": bool(
                direct_available and actual_mode in ("time", "hybrid")
            ),
        }
        if failures:
            details["degraded_channels"] = failed_channels
        return memories, details

    async def run_scoped():
        with exact_null_persona_scope(
            persona_filtered and persona_id in (None, NO_PERSONA)
        ):
            return await run()

    if timeout is not None and float(timeout) > 0:
        if not math.isfinite(float(timeout)):
            raise ValueError("timeout must be finite")
        return await asyncio.wait_for(run_scoped(), timeout=float(timeout))
    return await run_scoped()
