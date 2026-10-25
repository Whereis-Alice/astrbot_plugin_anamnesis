"""Scoped agentic retrieval and evidence share one request budget."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from datetime import datetime, timezone

from ..memory_scope import (
    GLOBAL_MEMORY_SCOPE,
    is_event_memory_allowed,
    resolve_memory_scope,
)
from ..retrieval.temporal_query import (
    TimeRange,
    parse_timestamp,
    resolve_context_timezone,
    speaker_matches,
)
from ..retrieval.temporal_retrieval import (
    retrieval_time_bounds,
    search_with_time,
    temporal_atom_evidence,
)
from ..utils import get_persona_id


def request_state(event):
    getter, setter = getattr(event, "get_extra", None), getattr(event, "set_extra", None)
    state = getter("anamnesis_agentic_budget") if callable(getter) else getattr(event, "_anamnesis_agentic_budget", None)
    if not isinstance(state, dict):
        state = {"calls": 0, "seconds": 0.0, "chars": 0, "lock": asyncio.Lock(), "pages": {},
                 "routes": {}, "reference_time": time.time()}
        if callable(setter):
            setter("anamnesis_agentic_budget", state)
        else:
            setattr(event, "_anamnesis_agentic_budget", state)
    return state


async def scoped_filters(tool, event):
    if not is_event_memory_allowed(tool.config_manager, event):
        raise ValueError("memory access is not allowed")
    persona = await get_persona_id(tool.context, event) if tool.config_manager.filtering_settings.get("use_persona_filtering", True) else None
    return resolve_memory_scope(tool.config_manager, event), persona


def evidence_allowed(metadata, scope, persona, event, config):
    origin = str(getattr(event, "unified_msg_origin", "") or "")
    if scope in (None, GLOBAL_MEMORY_SCOPE):
        matches = bool(origin and metadata.get("source_session_id") == origin)
        if scope == GLOBAL_MEMORY_SCOPE:
            matches = matches and metadata.get("session_id") == scope
    else:
        matches = metadata.get("session_id") == scope
    if config.filtering_settings.get("use_persona_filtering", True):
        matches = matches and metadata.get("persona_id") == persona
    return matches and metadata.get("status", "active") == "active"


timestamp = parse_timestamp


def bounded_json(data, limit):
    data = dict(data)
    resume = data.pop("_resume", None)
    source_count = len(data.get("source_messages", []))
    result_count = len(data.get("results", []))
    value = json.dumps(data, ensure_ascii=False, default=str)
    if len(value) <= limit:
        return value
    # Preserve valid JSON and visibly declare omitted output.
    data = dict(data, truncated=True)
    for key in ("source_messages", "atoms"):
        if isinstance(data.get(key), list):
            while data[key] and len(json.dumps(data, ensure_ascii=False, default=str)) > limit:
                data[key].pop()
    if isinstance(data.get("results"), list):
        for result in data["results"]:
            result["content"] = str(result.get("content", ""))[:600]
            result.pop("source_messages", None)
        while data["results"] and len(json.dumps(data, ensure_ascii=False, default=str)) > limit:
            data["results"].pop()
        data["count"] = len(data["results"])
        if resume and len(data["results"]) < result_count:
            position = resume["start"]
            if data["results"]:
                position = resume["positions"][str(data["results"][-1]["id"])]
            data["next_cursor"] = base64.urlsafe_b64encode(json.dumps([resume["signature"], position]).encode()).decode()
    if source_count and len(data.get("source_messages", [])) < source_count and "source_offset" in data:
        data["next_offset"] = data["source_offset"] + len(data["source_messages"])
    if "content" in data:
        data["content"] = str(data["content"])[:max(0, limit // 2)]
    value = json.dumps(data, ensure_ascii=False, default=str)
    return value if len(value) <= limit else json.dumps({"error": "output_budget_exhausted", "truncated": True})


async def with_budget(tool, event, operation):
    state = request_state(event)
    async with state["lock"]:
        seconds = float(tool.config_manager.get("agent_tools.recall_budget_seconds", 20)) - state["seconds"]
        chars = int(tool.config_manager.get("agent_tools.recall_output_max_chars", 16000)) - state["chars"]
        if state["calls"] >= int(tool.config_manager.get("agent_tools.recall_max_calls", 5)) or seconds <= 0 or chars < 300:
            return json.dumps({"error": "recall_budget_exhausted"})
        state["calls"] += 1
        started = time.monotonic()
        timed_out = False
        try:
            data = await asyncio.wait_for(operation(state), timeout=seconds)
        except TimeoutError:
            timed_out = True
            data = {"error": "recall_time_budget_exhausted"}
        except ValueError as error:
            data = {"error": str(error)}
        finally:
            elapsed = time.monotonic() - started
            state["seconds"] += max(elapsed, seconds) if timed_out else elapsed
        result = bounded_json(data, chars)
        state["chars"] += len(result)
        return result


async def agentic_search(tool, event, query, k, include_source=False, *, speaker=None,
                         start_time=None, end_time=None, exclude_ids=None, cursor=None, mode="auto"):
    async def search(state):
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("query must contain at most 2000 characters")
        scope, persona = await scoped_filters(tool, event)
        zone = resolve_context_timezone(tool.context, tool.config_manager, getattr(event, "unified_msg_origin", None))
        after, before = timestamp(start_time, timezone_name=zone), timestamp(end_time, timezone_name=zone)
        if after is not None and before is not None and after >= before:
            raise ValueError("start_time must be before end_time")
        if not isinstance(exclude_ids or [], list) or len(exclude_ids or []) > 500:
            raise ValueError("exclude_ids must be an array of at most 500 IDs")
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (exclude_ids or [])):
            raise ValueError("exclude_ids must contain positive integer IDs")
        excluded = {int(value) for value in (exclude_ids or [])}
        descriptor = [query, scope, persona, speaker, after, before, sorted(excluded), mode, zone]
        signature = hashlib.sha256(json.dumps(descriptor, ensure_ascii=False).encode()).hexdigest()[:24]
        offset = 0
        if cursor:
            if len(str(cursor)) > 4096:
                raise ValueError("invalid cursor")
            try:
                decoded = json.loads(base64.urlsafe_b64decode(str(cursor)).decode())
                if decoded[0] != signature:
                    raise ValueError
                offset = int(decoded[1])
                if offset < 0:
                    raise ValueError
            except Exception as error:
                raise ValueError("cursor does not match this query and its filters") from error
        cap = int(tool.config_manager.get("agent_tools.recall_candidate_limit", 100))
        if signature not in state["pages"]:
            candidates, route = await search_with_time(tool.memory_engine, query, cap,
                session_id=scope, persona_id=persona, config_manager=tool.config_manager,
                mode=mode, start_time=start_time, end_time=end_time, speaker=speaker,
                exclude_ids=excluded, candidate_limit=cap,
                timezone_name=zone,
                now=datetime.fromtimestamp(state.setdefault("reference_time", time.time()), timezone.utc))
            state.setdefault("routes", {})[signature] = route
            bounds = route.get("time_range") or {}
            after, before = bounds.get("start"), bounds.get("end")
            matches = []
            for memory in candidates:
                metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
                if memory.doc_id in excluded:
                    continue
                start, end, basis = retrieval_time_bounds(metadata)
                if after is not None and (end is None or end < after):
                    continue
                if before is not None and (start is None or start >= before):
                    continue
                if not speaker_matches(metadata, speaker):
                    continue
                matches.append(memory)
            state["pages"][signature] = matches
        matches = state["pages"][signature]
        route = state.get("routes", {}).get(signature, {})
        bounds = route.get("time_range") or {}
        after, before = bounds.get("start", after), bounds.get("end", before)
        size = max(1, min(int(k or 5), int(tool.config_manager.get("recall_engine.max_k", 10))))
        results = []
        positions = {}
        for position, memory in enumerate(matches[offset:offset + size], start=offset):
            # Recheck stored state even for cursor snapshots after deletion/expiry.
            stored = await tool.memory_engine.get_memory(memory.doc_id)
            metadata = stored.get("metadata") if stored else None
            if not isinstance(metadata, dict) or metadata.get("status", "active") != "active":
                continue
            if scope is not None and metadata.get("session_id") != scope:
                continue
            if tool.config_manager.filtering_settings.get("use_persona_filtering", True) and metadata.get("persona_id") != persona:
                continue
            atom_evidence = None
            if memory.metadata.get("temporal_time_basis") == "atom_event_time":
                atom_evidence = await temporal_atom_evidence(tool.memory_engine, memory.doc_id, TimeRange(after, before), metadata)
                if atom_evidence is None:
                    continue
                metadata = {**metadata, **atom_evidence}
            start, end, basis = retrieval_time_bounds(metadata)
            if after is not None and (end is None or end < after):
                continue
            if before is not None and (start is None or start >= before):
                continue
            if not speaker_matches(metadata, speaker):
                continue
            live = (atom_evidence["content"].splitlines() if atom_evidence else
                    await tool.memory_engine.resolve_live_facts(memory.doc_id, stored.get("text", ""), metadata))
            if live is None:
                continue
            start, end, basis = retrieval_time_bounds(metadata)
            item = {"id": memory.doc_id, "content": "\n".join(live), "score": memory.final_score,
                    "create_time": metadata.get("create_time"), "event_time": metadata.get("event_time"),
                    "time_basis": basis, "time_start": start, "time_end": end,
                    "source_session_id": metadata.get("source_session_id"), "has_source": bool(metadata.get("has_source"))}
            if isinstance(memory.score_breakdown, dict):
                item["score_breakdown"] = memory.score_breakdown
            if include_source and evidence_allowed(metadata, scope, persona, event, tool.config_manager):
                item["source_messages"] = (await tool.memory_engine.get_memory_source(memory.doc_id))[:20]
            results.append(item)
            positions[str(memory.doc_id)] = position + 1
        next_offset = offset + size
        next_cursor = base64.urlsafe_b64encode(json.dumps([signature, next_offset]).encode()).decode() if next_offset < len(matches) else None
        return {"query": query, "count": len(results), "results": results, "next_cursor": next_cursor,
                "candidate_limit": cap, "search_is_bounded": True, "retrieval": route,
                "_resume": {"signature": signature, "start": offset, "positions": positions}}
    return await with_budget(tool, event, search)


async def agentic_evidence(tool, event, memory_id, offset=0, limit=20):
    async def read(state):
        if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id <= 0:
            raise ValueError("invalid_memory_id")
        scope, persona = await scoped_filters(tool, event)
        memory = await tool.memory_engine.get_memory(int(memory_id))
        metadata = memory.get("metadata") if memory else None
        if not isinstance(metadata, dict) or not evidence_allowed(metadata, scope, persona, event, tool.config_manager):
            raise ValueError("memory is not available in current scope")
        facts = await tool.memory_engine.resolve_live_facts(int(memory_id), memory.get("text", ""), metadata)
        if facts is None:
            raise ValueError("memory no longer has live facts")
        start, size = max(0, int(offset)), max(1, min(50, int(limit)))
        source = await tool.memory_engine.get_memory_source(int(memory_id))
        return {"id": int(memory_id), "content": "\n".join(facts),
                "source_messages": source[start:start + size], "source_offset": start,
                "next_offset": start + size if start + size < len(source) else None,
                "atoms": await tool.memory_engine.get_memory_atom_lifecycle(int(memory_id)),
                "source_note": "Historical original messages; use current live facts to assess superseded claims."}
    return await with_budget(tool, event, read)
