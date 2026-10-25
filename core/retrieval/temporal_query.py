"""Deterministic time ranges for retrieval; no host-local timezone or LLM calls."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger


@dataclass(frozen=True, slots=True)
class TimeRange:
    start: float | None
    end: float | None
    expression: str = ""
    timezone: str = "UTC"

    def as_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "expression": self.expression,
            "timezone": self.timezone,
            "interval": "[start,end)",
        }


def parse_timestamp(value, *, timezone_name: str = "UTC") -> float | None:
    """Read ISO-8601/epoch seconds; naive ISO uses the explicitly chosen zone."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("invalid time filter; use epoch seconds or ISO-8601")  # noqa: TRY004 - one public filter-validation error type
    try:
        number = float(value)
    except (TypeError, ValueError):
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=resolve_timezone(timezone_name))
        number = moment.timestamp()
    if not math.isfinite(number) or not 0 <= number < 1e11:
        raise ValueError("invalid time filter; use epoch seconds or ISO-8601")
    return number


def resolve_timezone(name: str):
    try:
        return ZoneInfo(name or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        # Windows distributions without tzdata still know UTC. Other zones
        # must not silently be interpreted as the computer's local timezone.
        if name in ("UTC", "Etc/UTC"):
            return timezone.utc
        raise ValueError(f"unknown temporal timezone: {name}") from error


def resolve_context_timezone(context, config_manager, origin: str | None = None) -> str:
    """Prefer plugin override, then AstrBot's event-profile timezone.

    AstrBot Context.get_config(umo) reads the active host configuration. Old
    adapters without it fall back explicitly to Asia/Shanghai. Invalid names
    are reported and skipped; the machine's local timezone is never used.
    """
    candidates = []
    if config_manager is not None:
        candidates.append(config_manager.get("recall_engine.temporal_timezone", ""))
    getter = getattr(context, "get_config", None)
    if callable(getter):
        try:
            host = getter(origin)
            if hasattr(host, "get"):
                candidates.append(host.get("timezone", ""))
        except (AttributeError, TypeError, KeyError, ValueError):
            logger.debug("读取 AstrBot 时区失败，使用明确的默认时区", exc_info=True)
    candidates.append("Asia/Shanghai")
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        name = candidate.strip()
        try:
            resolve_timezone(name)
        except ValueError:
            logger.warning("无效的记忆检索时区 %r，尝试宿主或默认时区", name)
            continue
        return name
    return "UTC"  # UTC remains valid even without an installed timezone database.


def _month_start(moment: datetime, offset: int = 0) -> datetime:
    index = moment.year * 12 + moment.month - 1 + offset
    year, month = divmod(index, 12)
    return moment.replace(
        year=year, month=month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
    )


_DATE = r"(?:(\d{4})[年/-])?(\d{1,2})[月/-](\d{1,2})(?:日|号)?"
_FULL_DATE = r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})(?:日|号)?"
_RANGE = re.compile(rf"(?:从)?({_DATE})\s*(?:到|至|~|～|—|–|-)\s*({_DATE})")


def parse_time_range(
    query: str, *, now: datetime | None = None, timezone_name: str = "Asia/Shanghai"
) -> TimeRange | None:
    """Resolve unambiguous Chinese calendar ranges, anchored to request time.

    Weeks start Monday. An explicit date range includes the final calendar
    day and is represented by the next midnight, never 23:59:00. Vague words
    such as '以前', '上次' and '最近' do not impose an invented range.
    """
    zone = resolve_timezone(timezone_name)
    moment = now or datetime.now(zone)
    if moment.tzinfo is None:
        raise ValueError("temporal reference time must have an explicit timezone")
    moment = moment.astimezone(zone)
    today = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    text = str(query or "")

    def result(start, end, expression):
        if start >= end:
            raise ValueError("start_time must be before end_time")
        return TimeRange(start.timestamp(), end.timestamp(), expression, timezone_name)

    match = _RANGE.search(text)
    if match:
        # Nested captures: entire left date + year/month/day, then right.
        left_year = int(match.group(2) or moment.year)
        right_year = int(match.group(6) or left_year)
        left = today.replace(
            year=left_year, month=int(match.group(3)), day=int(match.group(4))
        )
        right = today.replace(
            year=right_year, month=int(match.group(7)), day=int(match.group(8))
        )
        # '12月30日至1月2日' unambiguously crosses the year boundary.
        if match.group(6) is None and right < left and right.month < left.month:
            right = right.replace(year=right.year + 1)
        return result(left, right + timedelta(days=1), match.group(0))

    match = re.search(_FULL_DATE, text)
    if match:
        day = today.replace(
            year=int(match.group(1)), month=int(match.group(2)), day=int(match.group(3))
        )
        return result(day, day + timedelta(days=1), match.group(0))
    match = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})(?:日|号)", text)
    if match:
        day = today.replace(month=int(match.group(1)), day=int(match.group(2)))
        return result(day, day + timedelta(days=1), match.group(0))

    # Longest overlapping phrases first ('大前天' before '前天').
    for phrase, offset in (
        ("大前天", -3),
        ("前天", -2),
        ("昨天", -1),
        ("今天", 0),
        ("明天", 1),
        ("后天", 2),
    ):
        if phrase in text:
            day = today + timedelta(days=offset)
            return result(day, day + timedelta(days=1), phrase)
    monday = today - timedelta(days=today.weekday())
    for phrases, offset in (
        (("上上周", "上上星期"), -2),
        (("上周", "上星期", "上个星期"), -1),
        (("本周", "这周", "本星期", "这星期"), 0),
    ):
        for phrase in phrases:
            if phrase in text:
                start = monday + timedelta(weeks=offset)
                return result(start, start + timedelta(weeks=1), phrase)
    for phrases, offset in (
        (("上上个月", "上上月"), -2),
        (("上个月", "上月"), -1),
        (("这个月", "本月"), 0),
    ):
        for phrase in phrases:
            if phrase in text:
                return result(
                    _month_start(today, offset), _month_start(today, offset + 1), phrase
                )
    match = re.search(r"(?:最近|过去|近)(\d{1,4})\s*(天|日|周|星期|个月|月)", text)
    if match:
        count = int(match.group(1))
        if count <= 0:
            raise ValueError("time range must be positive")
        unit = match.group(2)
        if unit in ("月", "个月"):
            start = _month_start(today, -count)
        else:
            days = count * 7 if unit in ("周", "星期") else count
            start = moment - timedelta(days=days)
        return result(start, moment, match.group(0))
    match = re.search(r"(?<!\d)(\d{4})年(\d{1,2})月(?!\d)", text)
    if match:
        start = _month_start(
            today.replace(year=int(match.group(1)), month=int(match.group(2)), day=1)
        )
        return result(start, _month_start(start, 1), match.group(0))
    for phrase, year_offset in (("前年", -2), ("去年", -1), ("今年", 0)):
        if phrase in text:
            start = today.replace(year=today.year + year_offset, month=1, day=1)
            return result(start, start.replace(year=start.year + 1), phrase)
    return None


def memory_time_bounds(metadata: dict) -> tuple[float | None, float | None, str]:
    """Prefer event time, then source-message interval, then creation time."""
    for basis, first, last in (
        ("event_time", metadata.get("event_time"), metadata.get("event_time")),
        (
            "source_time",
            metadata.get("source_time_start"),
            metadata.get("source_time_end"),
        ),
        ("create_time", metadata.get("create_time"), metadata.get("create_time")),
    ):
        try:
            start = parse_timestamp(first)
            end = parse_timestamp(last)
        except (TypeError, ValueError, OverflowError):
            continue
        if start is not None:
            return start, end if end is not None else start, basis
    return None, None, "create_time"


def range_overlaps(start, end, after, before) -> bool:
    """A stored instant/window intersects [after,before)."""
    return not (
        (after is not None and (end is None or end < after))
        or (before is not None and (start is None or start >= before))
    )


def speaker_matches(metadata: dict, speaker: str | None) -> bool:
    if not speaker:
        return True
    names = metadata.get("participants") or []
    names = list(names) if isinstance(names, (list, tuple)) else [names]
    identities = metadata.get("participant_identities") or []
    if isinstance(identities, dict):
        identities = [identities]
    for identity in identities:
        if isinstance(identity, dict):
            names.extend(
                str(identity.get(key) or "")
                for key in (
                    "sender_id",
                    "user_id",
                    "name",
                    "display_name",
                    "identity_key",
                )
            )
    return str(speaker).casefold() in {str(name).casefold() for name in names}
