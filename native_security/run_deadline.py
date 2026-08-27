"""Deployment-owned lifetime policy for native Agent Engine Turns.

Native harnesses may legitimately run for many hours. A total wall-clock
deadline is therefore optional; cancellation, provider idle limits, and
container resource limits remain separate lifecycle authorities.
"""

from __future__ import annotations


MIN_FINITE_RUN_SECONDS = 60
MAX_FINITE_RUN_SECONDS = 31_536_000


def parse_optional_run_deadline_seconds(
    raw: str | None,
    *,
    minimum: int = MIN_FINITE_RUN_SECONDS,
    maximum: int = MAX_FINITE_RUN_SECONDS,
) -> int | None:
    """Return ``None`` for an unbounded Turn and validate finite limits.

    An unset value or the explicit value ``0`` disables the total Turn
    deadline. Positive limits remain available as deployment policy, but
    values below the operational minimum and negative values fail closed.
    """

    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("run deadline is invalid") from exc
    if value == 0:
        return None
    if not minimum <= value <= maximum:
        raise ValueError("run deadline is outside its bound")
    return value


def remaining_run_deadline_seconds(
    limit_seconds: int | None,
    elapsed_seconds: float,
) -> float | None:
    """Preserve an optional total deadline across Supervisor adoption."""

    if limit_seconds is None:
        return None
    return max(0.0, float(limit_seconds) - max(0.0, elapsed_seconds))
