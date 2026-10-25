"""Conservative checkpoint tail alignment, adapted from LivingMemory 2.7.1."""

from __future__ import annotations


def compute_revert_cutoff(rows: list[dict], checkpoints: list[str]) -> int | None:
    if not rows:
        return None
    ids = [(row.get("metadata") or {}).get("llm_checkpoint_id") for row in rows]
    if not checkpoints:
        return 0 if all(ids) else None
    allowed = set(checkpoints)
    cutoff = None
    remaining = len(checkpoints) - 1
    aligned = False
    for i in range(len(rows) - 1, -1, -1):
        checkpoint = ids[i]
        if not checkpoint:
            return cutoff if cutoff is not None and rows[i].get("role") != "user" else None
        if checkpoint not in allowed:
            if aligned:
                return cutoff
            cutoff = i
        else:
            aligned = True
            if rows[i].get("role") == "user":
                matches = [j for j in range(remaining + 1) if checkpoints[j] == checkpoint]
                if not matches:
                    return None
                remaining = matches[-1] - 1
                if remaining < 0:
                    break  # Missing head is compression, never a rollback.
    return cutoff


def split_summary_windows(messages, *, split_by_day=True, idle_gap_minutes=180, tz=None):
    """Return contiguous windows; a user and the following reply remain together."""
    from datetime import datetime

    def get(message, key):
        return message.get(key) if isinstance(message, dict) else getattr(message, key, None)

    boundaries = [0]
    previous = None
    previous_role = None
    for index, message in enumerate(messages):
        value = get(message, "timestamp")
        try:
            current = float(value)
        except (TypeError, ValueError):
            current = None
        role = get(message, "role")
        if previous is not None and current is not None and current >= previous:
            day_changed = split_by_day and datetime.fromtimestamp(previous, tz).date() != datetime.fromtimestamp(current, tz).date()
            gap = idle_gap_minutes > 0 and current - previous >= idle_gap_minutes * 60
            if (day_changed or gap) and not (previous_role == "user" and role == "assistant"):
                boundaries.append(index)
        previous, previous_role = current, role
    boundaries.append(len(messages))
    return [(start, end) for start, end in zip(boundaries, boundaries[1:]) if end > start]
