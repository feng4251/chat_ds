"""Deterministic run lifecycle and evidence-quality reconciliation.

This module is intentionally independent from the agent loop, tool registry,
workspace implementation, and Backend database.  It defines two small
control-plane contracts:

``RunLifecycleMachine``
    Applies sequenced lifecycle observations while distinguishing provisional
    inner terminals from authoritative outer terminals.  Exact replays are
    idempotent, conflicting or out-of-order observations are rejected, and an
    authoritative terminal can never be reopened.

``RunContractLedger``
    Reconciles typed node, dispatch, evidence, and artifact receipts into one
    bounded, secret-free quality snapshot.  It never accepts model prose,
    response bodies, URLs, tool arguments, or arbitrary metadata.

The public snapshots are suitable for embedding in ``run.completed`` /
``run.failed`` event payloads.  They contain stable SHA-256 identities so a
Backend projection can persist or compare them without reinterpreting model
text.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping


RUN_CONTRACT_VERSION = 1
_MAX_IDENTIFIER_CHARS = 240
_MAX_TOOL_NAME_CHARS = 512
_MAX_REASON_CODE_CHARS = 128
_MAX_PATH_CHARS = 1024
_DEFAULT_MAX_SNAPSHOT_ENTRIES = 128
_DEFAULT_MAX_LEDGER_ENTRIES = 4096
_DEFAULT_MAX_LIFECYCLE_HISTORY = 128
_MAX_LIFECYCLE_OBSERVATIONS = 4096

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,512}$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SENSITIVE_RE = re.compile(
    r"(?i)(?:"
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
    r"passwd|secret|authorization|credential|private[_-]?key)"
    r"\s*[:=]\s*\S+|"
    r"https?://[^/\s:@]+:[^@\s/]+@"
    r")"
)


class LifecyclePhase(str, Enum):
    PLANNED = "planned"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMMITTING = "committing"
    TERMINAL = "terminal"


class TerminalOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QualityState(str, Enum):
    VERIFIED = "verified"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    CONFLICTED = "conflicted"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


# This is a deterministic reporting precedence, not a claim that the middle
# states are semantically interchangeable.  The full state counts and entries
# remain in every snapshot, so callers never need to infer all gaps from the
# single aggregate value.
_QUALITY_PRECEDENCE = {
    QualityState.VERIFIED: 0,
    QualityState.DEGRADED: 1,
    QualityState.UNSUPPORTED: 2,
    QualityState.UNAVAILABLE: 3,
    QualityState.CONFLICTED: 4,
    QualityState.FAILED: 5,
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _enum_value(value: Any, enum_type: type[Enum], field: str) -> str:
    normalized = str(value.value if isinstance(value, Enum) else value).strip()
    allowed = {str(item.value) for item in enum_type}
    if normalized not in allowed:
        raise ValueError(
            f"{field} must be one of: {', '.join(sorted(allowed))}"
        )
    return normalized


def _safe_identifier(
    value: Any,
    *,
    field: str,
    required: bool = True,
    max_chars: int = _MAX_IDENTIFIER_CHARS,
) -> str:
    if not isinstance(value, str):
        if not required and value is None:
            return ""
        raise TypeError(f"{field} must be a string")
    normalized = " ".join(value.strip().split())
    if not normalized:
        if required:
            raise ValueError(f"{field} must be non-empty")
        return ""
    if len(normalized) > max_chars:
        raise ValueError(f"{field} exceeds {max_chars} characters")
    if _CONTROL_RE.search(normalized):
        raise ValueError(f"{field} contains control characters")
    if "://" in normalized:
        raise ValueError(f"{field} must be a runtime identifier, not a URL")
    if _SENSITIVE_RE.search(normalized):
        raise ValueError(f"{field} appears to contain a credential")
    return normalized


def _safe_tool_name(value: Any, *, field: str = "tool_name") -> str:
    normalized = _safe_identifier(
        value,
        field=field,
        max_chars=_MAX_TOOL_NAME_CHARS,
    )
    if not _TOOL_NAME_RE.fullmatch(normalized):
        raise ValueError(f"{field} is not a canonical tool identifier")
    return normalized


def _safe_reason_code(value: Any) -> str:
    if value in {None, ""}:
        return ""
    normalized = _safe_identifier(
        value,
        field="reason_code",
        max_chars=_MAX_REASON_CODE_CHARS,
    ).casefold()
    if not _REASON_CODE_RE.fullmatch(normalized):
        raise ValueError(
            "reason_code must be a stable lowercase machine code, not prose"
        )
    return normalized


def _safe_sha256(value: Any, *, field: str, required: bool = False) -> str:
    if value in {None, ""}:
        if required:
            raise ValueError(f"{field} is required")
        return ""
    normalized = str(value).strip().casefold()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return normalized


def _safe_workspace_path(value: Any) -> str:
    normalized = _safe_identifier(
        value,
        field="path",
        max_chars=_MAX_PATH_CHARS,
    )
    if "://" in normalized or "\\" in normalized:
        raise ValueError("path must be workspace-relative, not a URL")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("path must be a safe workspace-relative path")
    return path.as_posix()


def _safe_nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class LifecycleDecision:
    """Result of one lifecycle observation."""

    accepted: bool
    changed: bool
    code: str
    seq: int
    phase: str
    terminal_outcome: str | None
    authoritative: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "changed": self.changed,
            "code": self.code,
            "seq": self.seq,
            "phase": self.phase,
            "terminal_outcome": self.terminal_outcome,
            "authoritative": self.authoritative,
        }


class RunLifecycleMachine:
    """Strict, replay-safe lifecycle reducer for one AgentRun.

    Successful completion requires the explicit committing phase.  Failure and
    cancellation are abort edges and may terminate any non-terminal phase.
    A verifier follow-up is the sole backwards phase edge:
    ``verifying -> executing``.
    """

    _ALLOWED_PHASE_EDGES = {
        LifecyclePhase.PLANNED: {LifecyclePhase.EXECUTING},
        LifecyclePhase.EXECUTING: {LifecyclePhase.VERIFYING},
        LifecyclePhase.VERIFYING: {
            LifecyclePhase.EXECUTING,
            LifecyclePhase.COMMITTING,
        },
        LifecyclePhase.COMMITTING: set(),
        LifecyclePhase.TERMINAL: set(),
    }

    def __init__(
        self,
        run_id: str,
        *,
        max_history: int = _DEFAULT_MAX_LIFECYCLE_HISTORY,
        max_observations: int = _MAX_LIFECYCLE_OBSERVATIONS,
    ) -> None:
        self.run_id = _safe_identifier(run_id, field="run_id")
        self.phase = LifecyclePhase.PLANNED
        self.terminal_outcome: TerminalOutcome | None = None
        self.last_seen_seq = -1
        self.last_applied_seq = -1
        self._max_history = max(1, int(max_history))
        self._max_observations = max(1, int(max_observations))
        self._observations: dict[int, str] = {}
        self._observation_results: dict[int, tuple[bool, str]] = {}
        self._history: list[dict[str, Any]] = []
        self._observation_count = 0
        self._authoritative_count = 0
        self._provisional_count = 0
        self._idempotent_replay_count = 0
        self._rejections: dict[str, int] = {}

    @property
    def is_terminal(self) -> bool:
        return self.phase is LifecyclePhase.TERMINAL

    @property
    def integrity_valid(self) -> bool:
        return not self._rejections

    def _decision(
        self,
        *,
        accepted: bool,
        changed: bool,
        code: str,
        seq: int,
        authoritative: bool,
    ) -> LifecycleDecision:
        return LifecycleDecision(
            accepted=accepted,
            changed=changed,
            code=code,
            seq=seq,
            phase=self.phase.value,
            terminal_outcome=(
                self.terminal_outcome.value
                if self.terminal_outcome is not None
                else None
            ),
            authoritative=authoritative,
        )

    def _record_rejection(self, code: str) -> None:
        self._rejections[code] = self._rejections.get(code, 0) + 1

    def _observe_identity(
        self,
        *,
        seq: int,
        fingerprint: str,
        authoritative: bool,
    ) -> LifecycleDecision | None:
        _safe_nonnegative_int(seq, field="seq")
        prior = self._observations.get(seq)
        if prior is not None:
            if prior == fingerprint:
                self._idempotent_replay_count += 1
                prior_accepted, prior_code = self._observation_results[seq]
                if not prior_accepted:
                    return self._decision(
                        accepted=False,
                        changed=False,
                        code=prior_code,
                        seq=seq,
                        authoritative=authoritative,
                    )
                return self._decision(
                    accepted=True,
                    changed=False,
                    code="idempotent_replay",
                    seq=seq,
                    authoritative=authoritative,
                )
            self._record_rejection("seq_conflict")
            return self._decision(
                accepted=False,
                changed=False,
                code="seq_conflict",
                seq=seq,
                authoritative=authoritative,
            )
        if seq < self.last_seen_seq:
            self._record_rejection("out_of_order")
            return self._decision(
                accepted=False,
                changed=False,
                code="out_of_order",
                seq=seq,
                authoritative=authoritative,
            )
        if len(self._observations) >= self._max_observations:
            self._record_rejection("observation_limit_exceeded")
            return self._decision(
                accepted=False,
                changed=False,
                code="observation_limit_exceeded",
                seq=seq,
                authoritative=authoritative,
            )
        self._observations[seq] = fingerprint
        self.last_seen_seq = max(self.last_seen_seq, seq)
        self._observation_count += 1
        if authoritative:
            self._authoritative_count += 1
        else:
            self._provisional_count += 1
        return None

    def _remember_observation_result(
        self,
        *,
        seq: int,
        accepted: bool,
        code: str,
    ) -> None:
        """Retain the first semantic verdict for exact replay."""

        self._observation_results[seq] = (accepted, code)

    def _append_history(
        self,
        *,
        seq: int,
        event: str,
        authoritative: bool,
        applied: bool,
        code: str,
    ) -> None:
        self._history.append({
            "seq": seq,
            "event": event,
            "authoritative": authoritative,
            "applied": applied,
            "code": code,
            "phase": self.phase.value,
            "terminal_outcome": (
                self.terminal_outcome.value
                if self.terminal_outcome is not None
                else None
            ),
        })
        if len(self._history) > self._max_history:
            del self._history[:-self._max_history]

    def transition(
        self,
        target_phase: LifecyclePhase | str,
        *,
        seq: int,
        event: str,
        authoritative: bool = True,
    ) -> LifecycleDecision:
        target = LifecyclePhase(
            _enum_value(target_phase, LifecyclePhase, "target_phase")
        )
        if target is LifecyclePhase.TERMINAL:
            raise ValueError("use terminalize() for a terminal transition")
        event_name = _safe_identifier(event, field="event")
        fingerprint = _sha256_json({
            "kind": "phase",
            "target": target.value,
            "event": event_name,
            "authoritative": bool(authoritative),
        })
        duplicate = self._observe_identity(
            seq=seq,
            fingerprint=fingerprint,
            authoritative=bool(authoritative),
        )
        if duplicate is not None:
            return duplicate
        if not authoritative:
            self._remember_observation_result(
                seq=seq,
                accepted=True,
                code="provisional_observed",
            )
            self._append_history(
                seq=seq,
                event=event_name,
                authoritative=False,
                applied=False,
                code="provisional_observed",
            )
            return self._decision(
                accepted=True,
                changed=False,
                code="provisional_observed",
                seq=seq,
                authoritative=False,
            )
        if self.is_terminal:
            self._record_rejection("terminal_monotonicity")
            self._remember_observation_result(
                seq=seq,
                accepted=False,
                code="terminal_monotonicity",
            )
            self._append_history(
                seq=seq,
                event=event_name,
                authoritative=True,
                applied=False,
                code="terminal_monotonicity",
            )
            return self._decision(
                accepted=False,
                changed=False,
                code="terminal_monotonicity",
                seq=seq,
                authoritative=True,
            )
        allowed = self._ALLOWED_PHASE_EDGES[self.phase]
        if target not in allowed:
            self._record_rejection("invalid_transition")
            self._remember_observation_result(
                seq=seq,
                accepted=False,
                code="invalid_transition",
            )
            self._append_history(
                seq=seq,
                event=event_name,
                authoritative=True,
                applied=False,
                code="invalid_transition",
            )
            return self._decision(
                accepted=False,
                changed=False,
                code="invalid_transition",
                seq=seq,
                authoritative=True,
            )
        self.phase = target
        self.last_applied_seq = seq
        self._remember_observation_result(
            seq=seq,
            accepted=True,
            code="applied",
        )
        self._append_history(
            seq=seq,
            event=event_name,
            authoritative=True,
            applied=True,
            code="applied",
        )
        return self._decision(
            accepted=True,
            changed=True,
            code="applied",
            seq=seq,
            authoritative=True,
        )

    def terminalize(
        self,
        outcome: TerminalOutcome | str,
        *,
        seq: int,
        event: str,
        authoritative: bool = True,
    ) -> LifecycleDecision:
        terminal = TerminalOutcome(
            _enum_value(outcome, TerminalOutcome, "outcome")
        )
        event_name = _safe_identifier(event, field="event")
        fingerprint = _sha256_json({
            "kind": "terminal",
            "outcome": terminal.value,
            "event": event_name,
            "authoritative": bool(authoritative),
        })
        duplicate = self._observe_identity(
            seq=seq,
            fingerprint=fingerprint,
            authoritative=bool(authoritative),
        )
        if duplicate is not None:
            return duplicate
        if not authoritative:
            self._remember_observation_result(
                seq=seq,
                accepted=True,
                code="provisional_terminal_observed",
            )
            self._append_history(
                seq=seq,
                event=event_name,
                authoritative=False,
                applied=False,
                code="provisional_terminal_observed",
            )
            return self._decision(
                accepted=True,
                changed=False,
                code="provisional_terminal_observed",
                seq=seq,
                authoritative=False,
            )
        if self.is_terminal:
            self._record_rejection("terminal_monotonicity")
            self._remember_observation_result(
                seq=seq,
                accepted=False,
                code="terminal_monotonicity",
            )
            self._append_history(
                seq=seq,
                event=event_name,
                authoritative=True,
                applied=False,
                code="terminal_monotonicity",
            )
            return self._decision(
                accepted=False,
                changed=False,
                code="terminal_monotonicity",
                seq=seq,
                authoritative=True,
            )
        if (
            terminal is TerminalOutcome.COMPLETED
            and self.phase is not LifecyclePhase.COMMITTING
        ):
            self._record_rejection("completion_before_commit")
            self._remember_observation_result(
                seq=seq,
                accepted=False,
                code="completion_before_commit",
            )
            self._append_history(
                seq=seq,
                event=event_name,
                authoritative=True,
                applied=False,
                code="completion_before_commit",
            )
            return self._decision(
                accepted=False,
                changed=False,
                code="completion_before_commit",
                seq=seq,
                authoritative=True,
            )
        self.phase = LifecyclePhase.TERMINAL
        self.terminal_outcome = terminal
        self.last_applied_seq = seq
        self._remember_observation_result(
            seq=seq,
            accepted=True,
            code="applied",
        )
        self._append_history(
            seq=seq,
            event=event_name,
            authoritative=True,
            applied=True,
            code="applied",
        )
        return self._decision(
            accepted=True,
            changed=True,
            code="applied",
            seq=seq,
            authoritative=True,
        )

    def observe_event(
        self,
        event_type: str,
        *,
        seq: int,
        authoritative: bool = True,
        verifier_followup: bool = False,
    ) -> LifecycleDecision:
        """Apply one normalized event without accepting its arbitrary payload."""

        normalized = _safe_identifier(event_type, field="event_type")
        if normalized in {"agent.spawned", "run.planned"}:
            # A freshly constructed machine already represents this phase.
            fingerprint = _sha256_json({
                "kind": "planned",
                "event": normalized,
                "authoritative": bool(authoritative),
            })
            duplicate = self._observe_identity(
                seq=seq,
                fingerprint=fingerprint,
                authoritative=bool(authoritative),
            )
            if duplicate is not None:
                return duplicate
            if not authoritative:
                code = "provisional_observed"
                accepted = True
            elif self.phase is LifecyclePhase.PLANNED:
                code = "planned_observed"
                accepted = True
                self.last_applied_seq = seq
            else:
                code = "invalid_transition"
                accepted = False
                self._record_rejection(code)
            self._remember_observation_result(
                seq=seq,
                accepted=accepted,
                code=code,
            )
            self._append_history(
                seq=seq,
                event=normalized,
                authoritative=bool(authoritative),
                applied=accepted and bool(authoritative),
                code=code,
            )
            return self._decision(
                accepted=accepted,
                changed=False,
                code=code,
                seq=seq,
                authoritative=bool(authoritative),
            )
        if normalized == "run.started":
            return self.transition(
                LifecyclePhase.EXECUTING,
                seq=seq,
                event=normalized,
                authoritative=authoritative,
            )
        if normalized == "verifier.requested":
            return self.transition(
                LifecyclePhase.VERIFYING,
                seq=seq,
                event=normalized,
                authoritative=authoritative,
            )
        if normalized == "verifier.followup_requested" or (
            normalized == "verifier.completed" and verifier_followup
        ):
            return self.transition(
                LifecyclePhase.EXECUTING,
                seq=seq,
                event=normalized,
                authoritative=authoritative,
            )
        if normalized == "verifier.completed":
            return self.transition(
                LifecyclePhase.COMMITTING,
                seq=seq,
                event=normalized,
                authoritative=authoritative,
            )
        if normalized in {"run.committing", "run.commit_requested"}:
            return self.transition(
                LifecyclePhase.COMMITTING,
                seq=seq,
                event=normalized,
                authoritative=authoritative,
            )
        if normalized == "run.completed":
            return self.terminalize(
                TerminalOutcome.COMPLETED,
                seq=seq,
                event=normalized,
                authoritative=authoritative,
            )
        if normalized == "run.failed":
            return self.terminalize(
                TerminalOutcome.FAILED,
                seq=seq,
                event=normalized,
                authoritative=authoritative,
            )
        if normalized == "run.cancelled":
            return self.terminalize(
                TerminalOutcome.CANCELLED,
                seq=seq,
                event=normalized,
                authoritative=authoritative,
            )
        raise ValueError(f"unsupported lifecycle event: {normalized}")

    def snapshot(self) -> dict[str, Any]:
        payload = {
            "version": RUN_CONTRACT_VERSION,
            "run_id": self.run_id,
            "phase": self.phase.value,
            "terminal": self.is_terminal,
            "terminal_outcome": (
                self.terminal_outcome.value
                if self.terminal_outcome is not None
                else None
            ),
            "last_seen_seq": self.last_seen_seq,
            "last_applied_seq": self.last_applied_seq,
            "integrity_valid": self.integrity_valid,
            "observation_count": self._observation_count,
            "authoritative_observation_count": self._authoritative_count,
            "provisional_observation_count": self._provisional_count,
            "idempotent_replay_count": self._idempotent_replay_count,
            "rejections": [
                {"code": code, "count": count}
                for code, count in sorted(self._rejections.items())
            ],
            "recent_history": list(self._history),
            "history_included": len(self._history),
            "history_omitted": max(
                0, self._observation_count - len(self._history)
            ),
        }
        payload["snapshot_sha256"] = _sha256_json(payload)
        return payload


@dataclass(frozen=True)
class _LedgerEntry:
    group_key: str
    identity_key: str
    entry_id: str
    kind: str
    state: str
    required: bool
    subject: dict[str, Any]
    receipt: dict[str, Any]
    reason_code: str
    revision: int

    def as_dict(self, *, active: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "state": self.state,
            "required": self.required,
            "active": active,
            "subject": dict(self.subject),
            "receipt": dict(self.receipt),
            "revision": self.revision,
        }
        if self.reason_code:
            result["reason_code"] = self.reason_code
        return result


class RunContractLedger:
    """Typed, bounded evidence ledger for one run contract.

    Callers must pass only runtime-owned identifiers and already-redacted
    receipt digests.  There is deliberately no generic ``metadata`` or
    ``details`` field: this prevents model prose, HTTP bodies, URLs, tool
    arguments, and credentials from entering the durable reconciliation
    payload by accident.
    """

    def __init__(
        self,
        run_id: str,
        *,
        max_snapshot_entries: int = _DEFAULT_MAX_SNAPSHOT_ENTRIES,
        max_entries: int = _DEFAULT_MAX_LEDGER_ENTRIES,
    ) -> None:
        self.run_id = _safe_identifier(run_id, field="run_id")
        self._max_snapshot_entries = max(1, int(max_snapshot_entries))
        self._max_entries = max(1, int(max_entries))
        self._entries: dict[str, _LedgerEntry] = {}
        self._identity_receipts: dict[str, str] = {}
        self._sealed_snapshot: dict[str, Any] | None = None
        self._terminal_outcome: TerminalOutcome | None = None

    @property
    def sealed(self) -> bool:
        return self._sealed_snapshot is not None

    def _record(
        self,
        *,
        kind: str,
        state: QualityState | str,
        required: bool,
        subject: Mapping[str, Any],
        receipt: Mapping[str, Any],
        reason_code: str,
        revision: int,
        identity_fields: Mapping[str, Any],
    ) -> str:
        if self.sealed:
            raise RuntimeError("run contract ledger is sealed")
        normalized_state = _enum_value(state, QualityState, "state")
        normalized_revision = _safe_nonnegative_int(
            revision, field="revision"
        )
        if normalized_revision < 1:
            raise ValueError("revision must be at least 1")
        normalized_reason = _safe_reason_code(reason_code)
        canonical_entry = {
            "kind": kind,
            "state": normalized_state,
            "required": bool(required),
            "subject": dict(subject),
            "receipt": dict(receipt),
            "reason_code": normalized_reason,
            "revision": normalized_revision,
        }
        identity_key = _sha256_json({
            "kind": kind,
            "identity": dict(identity_fields),
            "revision": normalized_revision,
        })
        group_key = _sha256_json({
            "kind": kind,
            "identity": dict(identity_fields),
        })
        receipt_fingerprint = _sha256_json(canonical_entry)
        prior = self._identity_receipts.get(identity_key)
        if prior is not None:
            if prior != receipt_fingerprint:
                raise ValueError(
                    "conflicting receipt for the same identity and revision"
                )
            return next(
                entry.entry_id
                for entry in self._entries.values()
                if entry.identity_key == identity_key
            )
        if any(
            entry.group_key == group_key
            and entry.revision > normalized_revision
            for entry in self._entries.values()
        ):
            raise ValueError(
                "out-of-order receipt revision for the same identity"
            )
        if len(self._entries) >= self._max_entries:
            raise RuntimeError("run contract ledger entry limit exceeded")
        entry_id = receipt_fingerprint
        self._identity_receipts[identity_key] = receipt_fingerprint
        self._entries[entry_id] = _LedgerEntry(
            group_key=group_key,
            identity_key=identity_key,
            entry_id=entry_id,
            kind=kind,
            state=normalized_state,
            required=bool(required),
            subject=dict(subject),
            receipt=dict(receipt),
            reason_code=normalized_reason,
            revision=normalized_revision,
        )
        return entry_id

    def record_node(
        self,
        node_id: str,
        *,
        state: QualityState | str,
        required: bool = True,
        skill_name: str = "",
        step_type: str = "",
        attempt: int = 1,
        result_receipt_sha256: str = "",
        reason_code: str = "",
    ) -> str:
        node = _safe_identifier(node_id, field="node_id")
        skill = _safe_identifier(
            skill_name,
            field="skill_name",
            required=False,
        )
        step = _safe_identifier(
            step_type,
            field="step_type",
            required=False,
        )
        prior_nodes = [
            entry
            for entry in self._entries.values()
            if (
                entry.kind == "node"
                and entry.subject.get("node_id") == node
            )
        ]
        if prior_nodes:
            latest = max(
                prior_nodes,
                key=lambda entry: (entry.revision, entry.entry_id),
            )
            if not skill:
                skill = str(latest.subject.get("skill_name") or "")
            if not step:
                step = str(latest.subject.get("step_type") or "")
        normalized_attempt = _safe_nonnegative_int(
            attempt, field="attempt"
        )
        if normalized_attempt < 1:
            raise ValueError("attempt must be at least 1")
        receipt_sha = _safe_sha256(
            result_receipt_sha256,
            field="result_receipt_sha256",
        )
        normalized_state = _enum_value(state, QualityState, "state")
        if normalized_state == QualityState.VERIFIED.value and not receipt_sha:
            raise ValueError(
                "a verified node receipt requires result_receipt_sha256"
            )
        subject = {"node_id": node}
        if skill:
            subject["skill_name"] = skill
        if step:
            subject["step_type"] = step
        receipt: dict[str, Any] = {"attempt": normalized_attempt}
        if receipt_sha:
            receipt["result_receipt_sha256"] = receipt_sha
        return self._record(
            kind="node",
            state=normalized_state,
            required=required,
            subject=subject,
            receipt=receipt,
            reason_code=reason_code,
            revision=normalized_attempt,
            identity_fields={"node_id": node},
        )

    def record_dispatch(
        self,
        tool_name: str,
        call_id: str,
        *,
        state: QualityState | str,
        required: bool | None = None,
        actual_dispatch_attempted: bool,
        invocation_safe_sha256: str = "",
        receipt_sha256: str = "",
        mutating: bool | None = None,
        stage: str = "completed",
        revision: int = 1,
        reason_code: str = "",
    ) -> str | None:
        """Record only a proven handler boundary, never model intent."""

        if actual_dispatch_attempted is not True:
            return None
        if required is not None and not isinstance(required, bool):
            raise TypeError("required must be a boolean or None")
        normalized_required = bool(required)
        tool = _safe_tool_name(tool_name)
        runtime_call_id = _safe_identifier(
            call_id,
            field="call_id",
            max_chars=512,
        )
        call_id_sha256 = _sha256_text(runtime_call_id)
        normalized_revision = _safe_nonnegative_int(
            revision, field="revision"
        )
        if normalized_revision < 1:
            raise ValueError("revision must be at least 1")
        invocation_sha = _safe_sha256(
            invocation_safe_sha256,
            field="invocation_safe_sha256",
        )
        receipt_digest = _safe_sha256(
            receipt_sha256,
            field="receipt_sha256",
        )
        normalized_stage = str(stage or "").strip().casefold()
        if normalized_stage not in {"started", "completed", "failed"}:
            raise ValueError(
                "stage must be one of: completed, failed, started"
            )
        prior_dispatches = [
            entry
            for entry in self._entries.values()
            if (
                entry.kind == "dispatch"
                and entry.receipt.get("call_id_sha256") == call_id_sha256
                and entry.revision < normalized_revision
            )
        ]
        if prior_dispatches:
            latest = max(
                prior_dispatches,
                key=lambda entry: (entry.revision, entry.entry_id),
            )
            if latest.subject.get("tool_name") != tool:
                raise ValueError(
                    "tool_name conflicts with the existing dispatch receipt"
                )
            if required is None:
                normalized_required = latest.required
            if not invocation_sha:
                invocation_sha = str(
                    latest.receipt.get("invocation_safe_sha256") or ""
                )
            if mutating is None and isinstance(
                latest.receipt.get("mutating"), bool
            ):
                mutating = bool(latest.receipt["mutating"])
        normalized_state = _enum_value(state, QualityState, "state")
        if (
            normalized_state == QualityState.VERIFIED.value
            and not receipt_digest
        ):
            raise ValueError(
                "a verified dispatch receipt requires receipt_sha256"
            )
        receipt: dict[str, Any] = {
            "actual_dispatch_attempted": True,
            "call_id_sha256": call_id_sha256,
            "stage": normalized_stage,
        }
        if invocation_sha:
            receipt["invocation_safe_sha256"] = invocation_sha
        if receipt_digest:
            receipt["receipt_sha256"] = receipt_digest
        if isinstance(mutating, bool):
            receipt["mutating"] = mutating
        return self._record(
            kind="dispatch",
            state=normalized_state,
            required=normalized_required,
            subject={"tool_name": tool},
            receipt=receipt,
            reason_code=reason_code,
            revision=normalized_revision,
            identity_fields={"call_id_sha256": call_id_sha256},
        )

    def record_evidence(
        self,
        evidence_id: str,
        *,
        state: QualityState | str,
        source_kind: str,
        required: bool = True,
        revision: int = 1,
        receipt_sha256: str = "",
        reason_code: str = "",
    ) -> str:
        evidence = _safe_identifier(evidence_id, field="evidence_id")
        source = _safe_identifier(source_kind, field="source_kind")
        receipt_digest = _safe_sha256(
            receipt_sha256,
            field="receipt_sha256",
        )
        normalized_state = _enum_value(state, QualityState, "state")
        if (
            normalized_state == QualityState.VERIFIED.value
            and not receipt_digest
        ):
            raise ValueError(
                "a verified evidence receipt requires receipt_sha256"
            )
        receipt: dict[str, Any] = {}
        if receipt_digest:
            receipt["receipt_sha256"] = receipt_digest
        return self._record(
            kind="evidence",
            state=normalized_state,
            required=required,
            subject={
                "evidence_id": evidence,
                "source_kind": source,
            },
            receipt=receipt,
            reason_code=reason_code,
            revision=revision,
            identity_fields={
                "evidence_id": evidence,
                "source_kind": source,
            },
        )

    def record_artifact(
        self,
        path: str,
        *,
        state: QualityState | str,
        required: bool = True,
        revision: int = 1,
        sha256: str = "",
        size_bytes: int | None = None,
        source_tool: str = "",
        receipt_sha256: str = "",
        reason_code: str = "",
    ) -> str:
        artifact_path = _safe_workspace_path(path)
        artifact_sha = _safe_sha256(sha256, field="sha256")
        receipt_digest = _safe_sha256(
            receipt_sha256,
            field="receipt_sha256",
        )
        tool = (
            _safe_tool_name(source_tool, field="source_tool")
            if source_tool
            else ""
        )
        normalized_state = _enum_value(state, QualityState, "state")
        if (
            normalized_state == QualityState.VERIFIED.value
            and not artifact_sha
        ):
            raise ValueError(
                "a verified artifact receipt requires its content sha256"
            )
        receipt: dict[str, Any] = {}
        if artifact_sha:
            receipt["sha256"] = artifact_sha
        if size_bytes is not None:
            receipt["size_bytes"] = _safe_nonnegative_int(
                size_bytes, field="size_bytes"
            )
        if tool:
            receipt["source_tool"] = tool
        if receipt_digest:
            receipt["receipt_sha256"] = receipt_digest
        return self._record(
            kind="artifact",
            state=normalized_state,
            required=required,
            subject={"path": artifact_path},
            receipt=receipt,
            reason_code=reason_code,
            revision=revision,
            identity_fields={"path": artifact_path},
        )

    def _active_entry_ids(self) -> set[str]:
        """Select the highest revision for each revisable subject."""

        selected: dict[str, _LedgerEntry] = {}
        for entry in self._entries.values():
            previous = selected.get(entry.group_key)
            if (
                previous is None
                or entry.revision > previous.revision
                or (
                    entry.revision == previous.revision
                    and entry.entry_id < previous.entry_id
                )
            ):
                selected[entry.group_key] = entry
        return {entry.entry_id for entry in selected.values()}

    def _aggregate_quality(
        self,
        active_entries: Iterable[_LedgerEntry],
        *,
        terminal_outcome: TerminalOutcome | None,
    ) -> QualityState | None:
        entries = list(active_entries)
        if terminal_outcome is TerminalOutcome.FAILED:
            return QualityState.FAILED
        if not entries:
            return None
        required_states = [
            QualityState(entry.state)
            for entry in entries
            if entry.required
        ]
        optional_nonverified = any(
            not entry.required
            and entry.state != QualityState.VERIFIED.value
            for entry in entries
        )
        if required_states:
            quality = max(
                required_states,
                key=lambda item: _QUALITY_PRECEDENCE[item],
            )
        else:
            quality = QualityState.VERIFIED
        if (
            optional_nonverified
            and _QUALITY_PRECEDENCE[quality]
            < _QUALITY_PRECEDENCE[QualityState.DEGRADED]
        ):
            quality = QualityState.DEGRADED
        return quality

    @staticmethod
    def _state_counts(
        entries: Iterable[_LedgerEntry],
    ) -> dict[str, int]:
        counts = {state.value: 0 for state in QualityState}
        for entry in entries:
            counts[entry.state] += 1
        return counts

    @staticmethod
    def _kind_counts(
        entries: Iterable[_LedgerEntry],
    ) -> dict[str, int]:
        counts = {
            "node": 0,
            "dispatch": 0,
            "evidence": 0,
            "artifact": 0,
        }
        for entry in entries:
            counts[entry.kind] += 1
        return counts

    def _build_snapshot(
        self,
        *,
        terminal_outcome: TerminalOutcome | None,
        sealed: bool,
    ) -> dict[str, Any]:
        all_entries = list(self._entries.values())
        active_ids = self._active_entry_ids()
        active_entries = [
            entry for entry in all_entries
            if entry.entry_id in active_ids
        ]
        quality = self._aggregate_quality(
            active_entries,
            terminal_outcome=terminal_outcome,
        )

        def priority(entry: _LedgerEntry) -> tuple[Any, ...]:
            active = entry.entry_id in active_ids
            nonverified = entry.state != QualityState.VERIFIED.value
            return (
                0 if active and nonverified else
                1 if active and entry.required else
                2 if active else
                3 if nonverified else
                4,
                -_QUALITY_PRECEDENCE[QualityState(entry.state)],
                entry.kind,
                _canonical_json(entry.subject),
                entry.revision,
                entry.entry_id,
            )

        ordered = sorted(all_entries, key=priority)
        included = ordered[:self._max_snapshot_entries]
        included_ids = {entry.entry_id for entry in included}
        entries_payload = [
            entry.as_dict(active=entry.entry_id in active_ids)
            for entry in included
        ]
        all_entry_payloads = [
            entry.as_dict(active=entry.entry_id in active_ids)
            for entry in sorted(
                all_entries,
                key=lambda item: item.entry_id,
            )
        ]
        active_nonverified = [
            entry for entry in active_entries
            if entry.state != QualityState.VERIFIED.value
        ]
        required_failed = [
            entry for entry in active_entries
            if entry.required and entry.state == QualityState.FAILED.value
        ]
        pending_dispatches = [
            entry for entry in active_entries
            if (
                entry.kind == "dispatch"
                and entry.receipt.get("stage") == "started"
            )
        ]
        completion_blockers = required_failed + pending_dispatches
        completion_allowed = not (
            terminal_outcome is TerminalOutcome.COMPLETED
            and completion_blockers
        )
        payload: dict[str, Any] = {
            "version": RUN_CONTRACT_VERSION,
            "run_id": self.run_id,
            "sealed": sealed,
            "terminal_outcome": (
                terminal_outcome.value
                if terminal_outcome is not None
                else None
            ),
            "quality_assessed": quality is not None,
            "quality": quality.value if quality is not None else None,
            "completion_allowed": completion_allowed,
            "entry_count_total": len(all_entries),
            "entry_count_active": len(active_entries),
            "entries_included": len(entries_payload),
            "entries_omitted": max(
                0, len(all_entries) - len(entries_payload)
            ),
            "active_nonverified_count": len(active_nonverified),
            "active_nonverified_omitted": any(
                entry.entry_id not in included_ids
                for entry in active_nonverified
            ),
            "required_failed_count": len(required_failed),
            "required_failed_omitted": any(
                entry.entry_id not in included_ids
                for entry in required_failed
            ),
            "pending_dispatch_count": len(pending_dispatches),
            "pending_dispatch_omitted": any(
                entry.entry_id not in included_ids
                for entry in pending_dispatches
            ),
            "completion_blocker_count": len(completion_blockers),
            "completion_blocker_omitted": any(
                entry.entry_id not in included_ids
                for entry in completion_blockers
            ),
            "active_state_counts": self._state_counts(active_entries),
            "all_state_counts": self._state_counts(all_entries),
            "active_kind_counts": self._kind_counts(active_entries),
            "all_kind_counts": self._kind_counts(all_entries),
            "entries_sha256": _sha256_json(all_entry_payloads),
            "entries": entries_payload,
        }
        payload["snapshot_sha256"] = _sha256_json(payload)
        return payload

    def snapshot(self) -> dict[str, Any]:
        if self._sealed_snapshot is not None:
            return json.loads(_canonical_json(self._sealed_snapshot))
        return self._build_snapshot(
            terminal_outcome=None,
            sealed=False,
        )

    def preview_terminal(
        self,
        terminal_outcome: TerminalOutcome | str,
    ) -> dict[str, Any]:
        """Evaluate a terminal outcome without mutating or sealing the ledger."""

        outcome = TerminalOutcome(
            _enum_value(
                terminal_outcome,
                TerminalOutcome,
                "terminal_outcome",
            )
        )
        if self._sealed_snapshot is not None:
            if self._terminal_outcome is not outcome:
                raise RuntimeError(
                    "run contract ledger is already sealed for a different "
                    "terminal outcome"
                )
            return json.loads(_canonical_json(self._sealed_snapshot))
        return self._build_snapshot(
            terminal_outcome=outcome,
            sealed=False,
        )

    def seal(
        self,
        terminal_outcome: TerminalOutcome | str,
    ) -> dict[str, Any]:
        outcome = TerminalOutcome(
            _enum_value(
                terminal_outcome,
                TerminalOutcome,
                "terminal_outcome",
            )
        )
        if self._sealed_snapshot is not None:
            if self._terminal_outcome is not outcome:
                raise RuntimeError(
                    "run contract ledger is already sealed for a different "
                    "terminal outcome"
                )
            return json.loads(_canonical_json(self._sealed_snapshot))
        self._terminal_outcome = outcome
        self._sealed_snapshot = self._build_snapshot(
            terminal_outcome=outcome,
            sealed=True,
        )
        return json.loads(_canonical_json(self._sealed_snapshot))


def build_reconciliation_snapshot(
    lifecycle: RunLifecycleMachine,
    ledger: RunContractLedger,
    *,
    seal_ledger: bool = True,
) -> dict[str, Any]:
    """Return one content-addressed lifecycle + quality reconciliation.

    A terminal lifecycle and ledger must refer to the same run and terminal
    outcome.  The helper can seal an open ledger at that boundary; callers may
    disable this to require an already-sealed ledger.
    """

    if lifecycle.run_id != ledger.run_id:
        raise ValueError("lifecycle and ledger run_id values do not match")
    lifecycle_snapshot = lifecycle.snapshot()
    if lifecycle.is_terminal:
        assert lifecycle.terminal_outcome is not None
        if seal_ledger:
            quality_snapshot = ledger.seal(lifecycle.terminal_outcome)
        else:
            if not ledger.sealed:
                raise RuntimeError(
                    "terminal reconciliation requires a sealed ledger"
                )
            quality_snapshot = ledger.snapshot()
        if (
            quality_snapshot.get("terminal_outcome")
            != lifecycle.terminal_outcome.value
        ):
            raise ValueError(
                "lifecycle and quality ledger terminal outcomes do not match"
            )
    else:
        if ledger.sealed:
            raise ValueError(
                "a non-terminal lifecycle cannot use a sealed quality ledger"
            )
        quality_snapshot = ledger.snapshot()
    reconciliation_valid = bool(
        lifecycle_snapshot.get("integrity_valid") is True
        and (
            not lifecycle.is_terminal
            or lifecycle.terminal_outcome
            is not TerminalOutcome.COMPLETED
            or quality_snapshot.get("completion_allowed") is True
        )
    )
    payload = {
        "version": RUN_CONTRACT_VERSION,
        "run_id": lifecycle.run_id,
        "terminal": lifecycle.is_terminal,
        "terminal_outcome": (
            lifecycle.terminal_outcome.value
            if lifecycle.terminal_outcome is not None
            else None
        ),
        "quality": quality_snapshot.get("quality"),
        "reconciliation_valid": reconciliation_valid,
        "lifecycle": lifecycle_snapshot,
        "quality_ledger": quality_snapshot,
    }
    payload["reconciliation_sha256"] = _sha256_json(payload)
    return payload


__all__ = [
    "LifecycleDecision",
    "LifecyclePhase",
    "QualityState",
    "RUN_CONTRACT_VERSION",
    "RunContractLedger",
    "RunLifecycleMachine",
    "TerminalOutcome",
    "build_reconciliation_snapshot",
]
