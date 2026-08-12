"""Shared, deterministic validation for controller-owned schedule specs.

This module is the single semantic boundary used by the HTTP API, the
terminal projector, and the isolated schedule MCP.  Keep it independent of
database and application state so the exact same request can be checked both
before a model-visible acceptance receipt and again at durable commit time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from croniter import croniter
except ImportError:  # Source-only checks may run before runtime deps exist.
    croniter = None


_DURATION_RE = re.compile(
    r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
    re.I,
)


class ScheduleSpecError(ValueError):
    """Stable machine code plus safe model/user-facing correction detail."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedScheduleSpec:
    kind: str
    value: str
    next_run_at: datetime
    expires_at: datetime | None


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ScheduleSpecError(
            "invalid_schedule_timezone",
            f"Unknown timezone: {name}",
        ) from exc


def _reference_time(now: datetime | None, tz: ZoneInfo) -> datetime:
    if now is None:
        return datetime.now(tz)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ScheduleSpecError(
            "invalid_schedule_reference_time",
            "The schedule validation reference time must include a timezone.",
        )
    return now.astimezone(tz)


def _duration(value: str) -> timedelta:
    match = _DURATION_RE.fullmatch(value.strip())
    if match is None:
        raise ScheduleSpecError(
            "invalid_schedule_duration",
            "Use a positive duration like 30m, 2h, or 1d.",
        )
    amount = int(match.group(1))
    if amount <= 0:
        raise ScheduleSpecError(
            "invalid_schedule_duration",
            "Schedule durations must be greater than zero.",
        )
    unit = match.group(2).lower()[0]
    return {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]


def normalize_schedule_expiry(value: object) -> datetime | None:
    """Return a naive UTC persistence value for an explicit expiry."""

    if value is None:
        return None
    parsed: Any = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ScheduleSpecError(
                "invalid_schedule_expiry",
                "expires_at must be a valid timezone-aware ISO-8601 timestamp.",
            ) from exc
    if not isinstance(parsed, datetime):
        raise ScheduleSpecError(
            "invalid_schedule_expiry",
            "expires_at must be a valid timezone-aware ISO-8601 timestamp.",
        )
    if parsed.tzinfo is None:
        raise ScheduleSpecError(
            "schedule_expiry_timezone_missing",
            "expires_at must include a timezone offset.",
        )
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def next_cron_occurrence(
    expression: str,
    timezone_name: str,
    *,
    after: datetime | None = None,
) -> datetime:
    """Compute the next cron occurrence as naive UTC using one parser."""

    if croniter is None:
        raise ScheduleSpecError(
            "schedule_parser_unavailable",
            "Cron expressions require the installed schedule parser.",
        )
    tz = _timezone(timezone_name)
    local_base = _reference_time(after, tz)
    try:
        next_local = croniter(expression, local_base).get_next(datetime)
    except Exception as exc:
        raise ScheduleSpecError(
            "invalid_schedule_expression",
            "Invalid cron expression.",
        ) from exc
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    return next_local.astimezone(timezone.utc).replace(tzinfo=None)


def resolve_schedule_spec(
    schedule: str,
    timezone_name: str = "UTC",
    *,
    expires_at: object = None,
    now: datetime | None = None,
) -> ResolvedScheduleSpec:
    """Parse one schedule and prove its first occurrence fits its boundary."""

    if not isinstance(schedule, str) or not schedule.strip():
        raise ScheduleSpecError(
            "invalid_schedule_expression",
            "A non-empty schedule is required.",
        )
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ScheduleSpecError(
            "invalid_schedule_timezone",
            "A non-empty IANA timezone is required.",
        )
    raw = schedule.strip()
    lower = raw.lower()
    tz = _timezone(timezone_name.strip())
    now_local = _reference_time(now, tz)

    if lower.startswith("every "):
        duration = _duration(raw[6:].strip())
        run_at = now_local + duration
        kind = "interval"
        value = str(int(duration.total_seconds()))
    else:
        parts = raw.split()
        if len(parts) in {5, 6}:
            try:
                next_run = next_cron_occurrence(
                    raw,
                    timezone_name.strip(),
                    after=now_local,
                )
            except ScheduleSpecError as exc:
                if exc.code == "schedule_parser_unavailable":
                    raise
            else:
                expiry = normalize_schedule_expiry(expires_at)
                if expiry is not None and next_run > expiry:
                    raise ScheduleSpecError(
                        "schedule_no_occurrence_before_expiry",
                        "The schedule has no occurrence on or before expires_at.",
                    )
                return ResolvedScheduleSpec("cron", raw, next_run, expiry)

        if lower.startswith("in "):
            run_at = now_local + _duration(raw[3:].strip())
            kind = "once"
            value = run_at.isoformat()
        else:
            try:
                duration = _duration(raw)
            except ScheduleSpecError:
                try:
                    run_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except (TypeError, ValueError) as exc:
                    raise ScheduleSpecError(
                        "invalid_schedule_expression",
                        "Invalid schedule. Use '30m', 'every 2h', an ISO timestamp, or a cron expression.",
                    ) from exc
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=tz)
                kind = "once"
                value = run_at.isoformat()
            else:
                run_at = now_local + duration
                kind = "once"
                value = run_at.isoformat()

    next_run = run_at.astimezone(timezone.utc).replace(tzinfo=None)
    expiry = normalize_schedule_expiry(expires_at)
    if expiry is not None and next_run > expiry:
        raise ScheduleSpecError(
            "schedule_no_occurrence_before_expiry",
            "The schedule has no occurrence on or before expires_at.",
        )
    return ResolvedScheduleSpec(kind, value, next_run, expiry)
