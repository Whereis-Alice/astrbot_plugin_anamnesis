"""Fixed-query retrieval evaluation, without reinforcement or settings writes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import statistics
import time
from datetime import datetime, timezone

from .temporal_retrieval import search_with_time


def ranking_metrics(actual_ids: list[int], expected_ids: list[int], k: int) -> dict:
    """Standard Hit@K, Precision@K, Recall@K and reciprocal first-hit rank."""
    expected = set(expected_ids)
    # Duplicates consume ranks without earning extra true positives. Slice
    # before counting unique hits so no prediction beyond K enters Top-K.
    actual = actual_ids[:k]
    hits = len(set(actual) & expected)
    reciprocal = next(
        (
            1.0 / rank
            for rank, memory_id in enumerate(actual, 1)
            if memory_id in expected
        ),
        0.0,
    )
    return {
        "hit_at_k": int(hits > 0),
        "precision_at_k": hits / k,
        "recall_at_k": hits / len(expected) if expected else None,
        "reciprocal_rank": reciprocal if expected else None,
        "expected_empty": not expected,
        "correct_empty": not expected and not actual,
    }


def _validate_cases(cases, max_cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= max_cases:
        raise ValueError(
            f"cases must contain 1 to {max_cases} independently labeled queries"
        )
    checked = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"case {index + 1} must be an object")  # noqa: TRY004 - validation API uses ValueError
        query = case.get("query", "")
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError(
                f"case {index + 1}: query must contain at most 2000 characters"
            )
        expected = case.get("expected_ids")
        if (
            not isinstance(expected, list)
            or len(expected) > 500
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in expected
            )
        ):
            raise ValueError(
                f"case {index + 1}: expected_ids must be at most 500 positive integer IDs"
            )
        mode = case.get("mode", "auto")
        if mode not in ("auto", "semantic", "time", "hybrid"):
            raise ValueError(f"case {index + 1}: invalid mode")
        checked.append(
            {
                "query": query,
                "expected_ids": list(dict.fromkeys(expected)),
                "mode": mode,
                "start_time": case.get("start_time"),
                "end_time": case.get("end_time"),
            }
        )
    return checked


async def evaluate_queries(
    memory_engine,
    cases: list[dict],
    *,
    k: int = 5,
    session_id=None,
    persona_id=None,
    config_manager=None,
    timeout_seconds: float = 10.0,
    max_cases: int = 100,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> dict:
    """Return a report for administrator-supplied queries and expected IDs.

    The caller authorizes access and selects scope/persona. No query templates,
    configuration search, automatic apply, access-time reinforcement or cache
    writes are performed. Each case has a bounded timeout; cancellation of the
    overall job propagates and settles its current retrieval coroutine.
    """
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 100:
        raise ValueError("evaluation k must be 1 to 100")
    if not math.isfinite(float(timeout_seconds)) or not 0 < timeout_seconds <= 120:
        raise ValueError("evaluation timeout_seconds must be 0 to 120")
    max_cases = max(1, min(int(max_cases), 200))
    checked = _validate_cases(cases, max_cases)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        raise ValueError("evaluation reference time must have an explicit timezone")
    options = (
        config_manager.get_all()
        if config_manager is not None and hasattr(config_manager, "get_all")
        else dict(getattr(memory_engine, "config", None) or {})
    )
    fingerprint = hashlib.sha256(
        json.dumps(options, sort_keys=True, default=str).encode()
    ).hexdigest()[:24]
    rows, durations = [], []
    for index, case in enumerate(checked, 1):
        started = time.perf_counter()
        error, results, details = None, [], None
        try:
            results, details = await search_with_time(
                memory_engine,
                case["query"],
                k=k,
                session_id=session_id,
                persona_id=persona_id,
                config_manager=config_manager,
                mode=case["mode"],
                start_time=case["start_time"],
                end_time=case["end_time"],
                timeout=timeout_seconds,
                now=reference,
                timezone_name=timezone_name,
                record_access=False,
                persona_filtered=persona_id is not None,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            error = "timeout"
        except ValueError as exc:
            error = str(exc)
        except Exception:  # noqa: BLE001 - isolate independent queries and exclude provider diagnostics from reports
            # Preserve failure count without copying provider/DB diagnostics
            # or credentials into a downloadable report.
            error = "retrieval_failed"
        elapsed = (time.perf_counter() - started) * 1000.0
        durations.append(elapsed)
        actual = [int(item.doc_id) for item in results]
        metrics = ranking_metrics(actual, case["expected_ids"], k)
        rows.append(
            {
                "index": index,
                **case,
                "actual_ids": actual,
                "metrics": metrics,
                "latency_ms": round(elapsed, 3),
                "error": error,
                "retrieval": details,
            }
        )
    labeled = [row for row in rows if not row["metrics"]["expected_empty"]]
    empties = [row for row in rows if row["metrics"]["expected_empty"]]
    ordered = sorted(durations)
    average = lambda key: (
        sum(row["metrics"][key] for row in labeled) / len(labeled) if labeled else None
    )
    return {
        "version": 1,
        "k": k,
        "reference_time": reference.isoformat(),
        "scope": session_id,
        "persona_id": persona_id,
        "configuration_fingerprint": fingerprint,
        "query_set_fingerprint": hashlib.sha256(
            json.dumps(
                checked, sort_keys=True, ensure_ascii=False, default=str
            ).encode()
        ).hexdigest(),
        "read_only": True,
        "settings_applied": False,
        "cases": rows,
        "summary": {
            "cases": len(rows),
            "labeled_positive_cases": len(labeled),
            "expected_empty_cases": len(empties),
            "errors": sum(row["error"] is not None for row in rows),
            "hit_at_k": average("hit_at_k"),
            "precision_at_k": average("precision_at_k"),
            "recall_at_k": average("recall_at_k"),
            "mrr": average("reciprocal_rank"),
            "empty_accuracy": sum(
                row["metrics"]["correct_empty"] and not row["error"] for row in empties
            )
            / len(empties)
            if empties
            else None,
            "median_latency_ms": round(statistics.median(durations), 3),
            "p95_latency_ms": round(
                ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 3
            ),
        },
        "metric_notes": "Quality averages use independently labeled positive cases; failed queries count as misses. Expected-empty queries are scored separately. Precision@K divides true positives by K.",
    }
