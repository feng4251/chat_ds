"""Deterministic, bounded deadlines for streamed provider completions.

The model context window answers whether a request fits; it does not say how
long a slow but healthy decoder needs to reach its terminal frame.  This
module therefore separates three limits:

* an initial lease that catches streams which make no useful progress;
* a request-specific planning checkpoint derived from input/output budgets
  for observability and capacity diagnostics;
* an absolute configured/caller hard cap which progress can never extend.

Only callers that have observed material provider output may renew the soft
lease.  Transport pings, empty deltas, and usage-only frames are intentionally
inert.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math


class ProviderStreamDeadlineExceeded(asyncio.TimeoutError):
    """A material-progress lease or its immutable hard cap expired."""

    def __init__(self, metrics: dict[str, int | float | None | str]) -> None:
        super().__init__(
            "provider stream exceeded its deterministic material-progress "
            "deadline"
        )
        self.metrics = dict(metrics)


def _positive_finite(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a finite positive number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return normalized


def _non_negative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class ProviderStreamDeadlinePlan:
    """One secret-free provider-stream timing plan."""

    estimated_input_tokens: int
    max_output_tokens: int
    initial_lease_seconds: float
    progress_grace_seconds: float
    planned_deadline_seconds: float
    hard_cap_seconds: float
    configured_hard_cap_seconds: float
    caller_hard_cap_seconds: float | None
    estimated_budget_seconds: float

    def debug_payload(self) -> dict[str, int | float | None]:
        return {
            "estimated_input_tokens": self.estimated_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "initial_lease_seconds": self.initial_lease_seconds,
            "progress_grace_seconds": self.progress_grace_seconds,
            "planned_deadline_seconds": self.planned_deadline_seconds,
            "hard_cap_seconds": self.hard_cap_seconds,
            "configured_hard_cap_seconds": self.configured_hard_cap_seconds,
            "caller_hard_cap_seconds": self.caller_hard_cap_seconds,
            "estimated_budget_seconds": self.estimated_budget_seconds,
        }


def build_provider_stream_deadline_plan(
    *,
    estimated_input_tokens: int,
    max_output_tokens: int,
    initial_lease_seconds: float,
    progress_grace_seconds: float,
    configured_hard_cap_seconds: float,
    caller_hard_cap_seconds: float | None,
    input_planning_tokens_per_second: float,
    output_planning_tokens_per_second: float,
    planning_safety_factor: float,
    fixed_overhead_seconds: float,
) -> ProviderStreamDeadlinePlan:
    """Build a deterministic deadline plan from the concrete request budget.

    Planning rates are conservative capacity estimates, not claims about
    provider billing or exact tokenizer output.  The configured and explicit
    caller limits always remain authoritative upper bounds.
    """

    input_tokens = _non_negative_int(
        estimated_input_tokens,
        field="estimated_input_tokens",
    )
    output_tokens = _non_negative_int(
        max_output_tokens,
        field="max_output_tokens",
    )
    if output_tokens == 0:
        raise ValueError("max_output_tokens must be a positive integer")

    initial = _positive_finite(
        initial_lease_seconds,
        field="initial_lease_seconds",
    )
    progress_grace = _positive_finite(
        progress_grace_seconds,
        field="progress_grace_seconds",
    )
    configured_hard = _positive_finite(
        configured_hard_cap_seconds,
        field="configured_hard_cap_seconds",
    )
    input_rate = _positive_finite(
        input_planning_tokens_per_second,
        field="input_planning_tokens_per_second",
    )
    output_rate = _positive_finite(
        output_planning_tokens_per_second,
        field="output_planning_tokens_per_second",
    )
    safety = _positive_finite(
        planning_safety_factor,
        field="planning_safety_factor",
    )
    overhead = _positive_finite(
        fixed_overhead_seconds,
        field="fixed_overhead_seconds",
    )
    caller_hard = (
        None
        if caller_hard_cap_seconds is None
        else _positive_finite(
            caller_hard_cap_seconds,
            field="caller_hard_cap_seconds",
        )
    )
    hard_cap = min(
        configured_hard,
        caller_hard if caller_hard is not None else configured_hard,
    )
    estimated_budget = overhead + safety * (
        input_tokens / input_rate + output_tokens / output_rate
    )
    planned = min(hard_cap, max(min(initial, hard_cap), estimated_budget))

    return ProviderStreamDeadlinePlan(
        estimated_input_tokens=input_tokens,
        max_output_tokens=output_tokens,
        # ``planned`` is an estimate/checkpoint for observability.  It must
        # never shorten either of the independently configured soft-lease
        # inputs: a healthy stream may continue past that estimate while
        # remaining bounded by the effective configured/caller hard cap.
        initial_lease_seconds=min(initial, hard_cap),
        progress_grace_seconds=min(progress_grace, hard_cap),
        planned_deadline_seconds=planned,
        hard_cap_seconds=hard_cap,
        configured_hard_cap_seconds=configured_hard,
        caller_hard_cap_seconds=caller_hard,
        estimated_budget_seconds=estimated_budget,
    )


@dataclass
class MaterialProgressLease:
    """Mutable soft lease bounded by an immutable request deadline plan."""

    plan: ProviderStreamDeadlinePlan
    started_at: float
    soft_deadline: float
    hard_deadline: float
    last_material_progress_at: float | None = None
    material_progress_chars: int = 0
    renewal_count: int = 0

    @classmethod
    def start(
        cls,
        plan: ProviderStreamDeadlinePlan,
        *,
        now: float,
    ) -> "MaterialProgressLease":
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise TypeError("now must be a finite number")
        normalized_now = float(now)
        if not math.isfinite(normalized_now):
            raise ValueError("now must be a finite number")
        hard_deadline = normalized_now + plan.hard_cap_seconds
        return cls(
            plan=plan,
            started_at=normalized_now,
            soft_deadline=min(
                hard_deadline,
                normalized_now + plan.initial_lease_seconds,
            ),
            hard_deadline=hard_deadline,
        )

    @property
    def deadline(self) -> float:
        return min(self.soft_deadline, self.hard_deadline)

    def remaining(self, *, now: float) -> float:
        return self.deadline - float(now)

    def observe_material_progress(self, chars: int, *, now: float) -> bool:
        """Renew the soft lease for non-empty provider-owned material only."""

        amount = _non_negative_int(chars, field="chars")
        if amount == 0:
            return False
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise TypeError("now must be a finite number")
        normalized_now = float(now)
        if not math.isfinite(normalized_now):
            raise ValueError("now must be a finite number")
        if normalized_now > self.hard_deadline:
            return False
        self.material_progress_chars += amount
        self.last_material_progress_at = normalized_now
        renewed_deadline = min(
            self.hard_deadline,
            normalized_now + self.plan.progress_grace_seconds,
        )
        if renewed_deadline > self.soft_deadline:
            self.soft_deadline = renewed_deadline
            self.renewal_count += 1
            return True
        return False

    def debug_payload(self, *, now: float) -> dict[str, int | float | None | str]:
        normalized_now = float(now)
        if self.hard_deadline <= self.soft_deadline:
            reason = "configured_or_caller_hard_cap"
        elif self.last_material_progress_at is None:
            reason = "initial_lease"
        else:
            reason = "progress_lease"
        planned_deadline = (
            self.started_at + self.plan.planned_deadline_seconds
        )
        return {
            **self.plan.debug_payload(),
            "elapsed_seconds": max(0.0, normalized_now - self.started_at),
            "remaining_seconds": max(0.0, self.remaining(now=normalized_now)),
            "material_progress_chars": self.material_progress_chars,
            "last_material_progress_elapsed_seconds": (
                None
                if self.last_material_progress_at is None
                else max(
                    0.0,
                    self.last_material_progress_at - self.started_at,
                )
            ),
            "renewal_count": self.renewal_count,
            "deadline_kind": reason,
            "planned_budget_crossed": normalized_now >= planned_deadline,
        }


__all__ = [
    "MaterialProgressLease",
    "ProviderStreamDeadlineExceeded",
    "ProviderStreamDeadlinePlan",
    "build_provider_stream_deadline_plan",
]
