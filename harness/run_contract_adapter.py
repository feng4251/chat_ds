"""Safe adapter from Harness ``agent_event`` records to run contracts.

The adapter deliberately accepts only a small typed projection of each event.
It never stores error prose, result bodies, URLs, raw tool arguments, or raw
tool call IDs. Unknown events are ignored. Known malformed events fail closed
with a stable code and without echoing their payload.

``apply_agent_event`` is deterministic for a given event and contract state.
It is intentionally independent from ``agent_loop`` so the emitter, delegated
event bridge, Backend projector, and replay tests can share the same rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

try:
    from .run_contract import (
        LifecycleDecision,
        QualityState,
        RunContractLedger,
        RunLifecycleMachine,
        TerminalOutcome,
    )
    from .retrieval_completeness import (
        retrieval_receipt_affects_completion_quality,
    )
except ImportError:  # pragma: no cover - direct Harness module loading
    from run_contract import (
        LifecycleDecision,
        QualityState,
        RunContractLedger,
        RunLifecycleMachine,
        TerminalOutcome,
    )
    from retrieval_completeness import (
        retrieval_receipt_affects_completion_quality,
    )


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")

_LIFECYCLE_EVENTS = {
    "agent.spawned",
    "run.planned",
    "run.started",
    "verifier.requested",
    "verifier.followup_requested",
    "verifier.completed",
    "verifier.failed",
    "run.committing",
    "run.commit_requested",
    "run.completed",
    "run.failed",
    "run.cancelled",
}
_TOOL_EVENTS = {
    "tool.dispatch_started",
    "tool.dispatch_completed",
    "tool.completed",
    "tool.failed",
}
_NODE_EVENTS = {"agent.result"}
_ARTIFACT_EVENTS = {
    "artifact.created",
    "artifact.updated",
    "artifact.verified",
    "artifact.failed",
}
_EVIDENCE_EVENTS = {
    "evidence.recorded",
    "evidence.verified",
    "evidence.degraded",
    "evidence.unavailable",
    "evidence.conflicted",
    "evidence.unsupported",
    "evidence.failed",
}
_RECOGNIZED_EVENTS = (
    _LIFECYCLE_EVENTS
    | _TOOL_EVENTS
    | _NODE_EVENTS
    | _ARTIFACT_EVENTS
    | _EVIDENCE_EVENTS
)

_QUALITY_ALIASES = {
    "complete": QualityState.VERIFIED.value,
    "completed": QualityState.VERIFIED.value,
    "success": QualityState.VERIFIED.value,
    "succeeded": QualityState.VERIFIED.value,
    "pass": QualityState.VERIFIED.value,
    "partial": QualityState.DEGRADED.value,
    "warning": QualityState.DEGRADED.value,
    "inconclusive": QualityState.DEGRADED.value,
    "error": QualityState.FAILED.value,
}
_QUALITY_VALUES = {item.value for item in QualityState}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_sha256(value: Any) -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if _SHA256_RE.fullmatch(candidate) else ""


def _first_sha256(*values: Any) -> str:
    for value in values:
        digest = _valid_sha256(value)
        if digest:
            return digest
    return ""


def _string_field(
    value: Any,
    *,
    required: bool = True,
) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise TypeError("expected a string")
    normalized = " ".join(value.strip().split())
    if required and not normalized:
        raise ValueError("expected a non-empty string")
    return normalized


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("payload")
    return value if isinstance(value, Mapping) else {}


def _required(
    payload: Mapping[str, Any],
    *,
    default: bool,
) -> bool:
    value = payload.get("contract_required")
    if value is None:
        value = payload.get("required")
    if value is None:
        return default
    if not isinstance(value, bool):
        raise TypeError("required must be a boolean")
    return value


def _optional_required(payload: Mapping[str, Any]) -> bool | None:
    value = payload.get("contract_required")
    if value is None:
        value = payload.get("required")
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError("required must be a boolean")
    return value


def _positive_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected a positive integer")
    return value


def _event_revision(payload: Mapping[str, Any], seq: Any) -> int:
    if payload.get("revision") is not None:
        return _positive_int(payload.get("revision"), default=1)
    if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1:
        return seq
    raise ValueError("versioned receipt event requires seq or revision")


def _node_revision(payload: Mapping[str, Any]) -> int:
    attempt = payload.get("attempt")
    revision = payload.get("revision")
    if attempt is not None and revision is not None and attempt != revision:
        raise ValueError("attempt and revision must match")
    return _positive_int(
        attempt if attempt is not None else revision,
        default=1,
    )


def _reason_code(
    payload: Mapping[str, Any],
    *,
    fallback: str,
) -> str:
    candidate = str(payload.get("reason_code") or "").strip().casefold()
    if candidate and _REASON_CODE_RE.fullmatch(candidate):
        return candidate
    return fallback


def _quality(
    value: Any,
    *,
    default: QualityState | str,
) -> str:
    candidate = str(value or "").strip().casefold()
    if not candidate:
        candidate = str(
            default.value if isinstance(default, QualityState) else default
        ).strip().casefold()
    candidate = _QUALITY_ALIASES.get(candidate, candidate)
    if candidate not in _QUALITY_VALUES:
        raise ValueError("unsupported quality state")
    return candidate


def _authoritative(
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> bool:
    top_level = event.get("authoritative")
    if isinstance(top_level, bool):
        return top_level
    nested = payload.get("authoritative")
    if isinstance(nested, bool):
        return nested
    if payload.get("provisional_terminal") is True:
        return False
    return True


def _result_sha256(payload: Mapping[str, Any]) -> str:
    transaction = payload.get("output_transaction")
    transaction_sha = (
        transaction.get("content_sha256")
        if isinstance(transaction, Mapping)
        else None
    )
    return _first_sha256(
        payload.get("result_receipt_sha256"),
        payload.get("result_sha256"),
        payload.get("content_sha256"),
        transaction_sha,
    )


@dataclass(frozen=True)
class AgentEventApplication:
    """Bounded result of adapting one event.

    ``code`` is a stable machine value. It intentionally contains no exception
    text because validation errors may have originated in untrusted payloads.
    """

    event_type: str
    recognized: bool
    applied: bool
    code: str
    ledger_entry_ids: tuple[str, ...] = ()
    lifecycle_decision: LifecycleDecision | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "recognized": self.recognized,
            "applied": self.applied,
            "code": self.code,
            "ledger_entry_ids": list(self.ledger_entry_ids),
            "lifecycle_decision": (
                self.lifecycle_decision.as_dict()
                if self.lifecycle_decision is not None
                else None
            ),
        }


def _ignored(event_type: str, code: str) -> AgentEventApplication:
    return AgentEventApplication(
        event_type=event_type,
        recognized=False,
        applied=False,
        code=code,
    )


def _rejected(event_type: str, code: str) -> AgentEventApplication:
    return AgentEventApplication(
        event_type=event_type,
        recognized=True,
        applied=False,
        code=code,
    )


def _apply_lifecycle(
    lifecycle: RunLifecycleMachine,
    ledger: RunContractLedger,
    event_type: str,
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> AgentEventApplication:
    seq = event.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return _rejected(event_type, "invalid_event_contract")
    authoritative = _authoritative(event, payload)
    if (
        event_type == "run.completed"
        and authoritative
        and ledger.preview_terminal(
            TerminalOutcome.COMPLETED
        ).get("completion_allowed") is not True
    ):
        return _rejected(event_type, "completion_contract_failed")
    if event_type == "verifier.failed":
        decision = lifecycle.terminalize(
            TerminalOutcome.FAILED,
            seq=seq,
            event=event_type,
            authoritative=authoritative,
        )
    else:
        decision = lifecycle.observe_event(
            event_type,
            seq=seq,
            authoritative=authoritative,
            verifier_followup=bool(
                payload.get("needs_more_work") is True
                or payload.get("followup_required") is True
            ),
        )
    return AgentEventApplication(
        event_type=event_type,
        recognized=True,
        applied=decision.accepted,
        code=decision.code,
        lifecycle_decision=decision,
    )


def _apply_tool(
    ledger: RunContractLedger,
    event_type: str,
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> AgentEventApplication:
    if payload.get("actual_dispatch_attempted") is not True:
        return _rejected(event_type, "dispatch_boundary_not_entered")
    tool_name = _string_field(
        payload.get("tool_name") or event.get("tool_name")
    )
    call_id = _string_field(
        payload.get("tool_call_id") or event.get("tool_call_id")
    )
    required = (
        _required(payload, default=False)
        if event_type == "tool.dispatch_started"
        else _optional_required(payload)
    )
    mutating_value = payload.get("mutating")
    mutating = mutating_value if isinstance(mutating_value, bool) else None
    if event_type == "tool.dispatch_started":
        projection = payload.get("invocation_argument_projection")
        invocation_sha = (
            _valid_sha256(projection.get("safe_sha256"))
            if isinstance(projection, Mapping)
            else ""
        )
        entry_id = ledger.record_dispatch(
            tool_name,
            call_id,
            state=QualityState.DEGRADED,
            required=required,
            actual_dispatch_attempted=True,
            invocation_safe_sha256=invocation_sha,
            mutating=mutating,
            stage="started",
            revision=1,
            reason_code="dispatch_pending",
        )
    else:
        succeeded = (
            event_type in {"tool.completed", "tool.dispatch_completed"}
            and str(payload.get("outcome") or "success").casefold()
            in {"success", "succeeded", "completed"}
        )
        normalized_tool = tool_name
        normalized_call_id = call_id
        receipt_sha = _canonical_sha256({
            "kind": "dispatch_terminal",
            "tool_name": normalized_tool,
            "call_id_sha256": hashlib.sha256(
                normalized_call_id.encode("utf-8")
            ).hexdigest(),
            "outcome": "success" if succeeded else "failed",
            "actual_dispatch_attempted": True,
        })
        entry_id = ledger.record_dispatch(
            normalized_tool,
            normalized_call_id,
            state=(
                QualityState.VERIFIED
                if succeeded
                else QualityState.FAILED
            ),
            required=required,
            actual_dispatch_attempted=True,
            receipt_sha256=receipt_sha,
            mutating=mutating,
            stage="completed" if succeeded else "failed",
            revision=2,
            reason_code="" if succeeded else "tool_dispatch_failed",
        )
    return AgentEventApplication(
        event_type=event_type,
        recognized=True,
        applied=entry_id is not None,
        code="receipt_recorded" if entry_id else "receipt_not_recorded",
        ledger_entry_ids=(entry_id,) if entry_id else (),
    )


def _apply_node(
    ledger: RunContractLedger,
    event_type: str,
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> AgentEventApplication:
    node_id = (
        payload.get("node_id")
        or payload.get("step_id")
        or payload.get("worker_id")
        or event.get("agent_name")
    )
    normalized_node_id = _string_field(node_id)
    status = str(payload.get("status") or "").strip().casefold()
    quality_value = (
        payload.get("quality")
        or payload.get("completion_quality")
        or status
    )
    default_quality = (
        QualityState.FAILED
        if status in {"error", "failed", "cancelled"}
        else QualityState.VERIFIED
    )
    state = _quality(quality_value, default=default_quality)
    if retrieval_receipt_affects_completion_quality(
        payload.get("unresolved_retrieval")
    ):
        state = QualityState.DEGRADED.value
    receipt_sha = _result_sha256(payload)
    reason = _reason_code(
        payload,
        fallback=(
            "agent_result_failed"
            if state == QualityState.FAILED.value
            else ""
        ),
    )
    if state == QualityState.VERIFIED.value and not receipt_sha:
        state = QualityState.DEGRADED.value
        reason = "result_receipt_missing"
    entry_id = ledger.record_node(
        normalized_node_id,
        state=state,
        required=_required(payload, default=True),
        skill_name=_string_field(
            payload.get("skill_name"),
            required=False,
        ),
        step_type=_string_field(
            payload.get("step_type"),
            required=False,
        ),
        attempt=_node_revision(payload),
        result_receipt_sha256=receipt_sha,
        reason_code=reason,
    )
    return AgentEventApplication(
        event_type=event_type,
        recognized=True,
        applied=True,
        code="receipt_recorded",
        ledger_entry_ids=(entry_id,),
    )


def _apply_artifact(
    ledger: RunContractLedger,
    event_type: str,
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> AgentEventApplication:
    artifact_sha = _valid_sha256(
        payload.get("sha256") or payload.get("content_sha256")
    )
    if event_type == "artifact.failed":
        state = QualityState.FAILED.value
        reason = _reason_code(
            payload,
            fallback="artifact_receipt_failed",
        )
    else:
        default_state = (
            QualityState.VERIFIED
            if artifact_sha
            else QualityState.DEGRADED
        )
        state = _quality(payload.get("quality"), default=default_state)
        reason = _reason_code(
            payload,
            fallback=(
                ""
                if state == QualityState.VERIFIED.value
                else "artifact_hash_missing"
            ),
        )
    if state == QualityState.VERIFIED.value and not artifact_sha:
        state = QualityState.DEGRADED.value
        reason = "artifact_hash_missing"
    size_bytes = payload.get("size_bytes")
    if size_bytes is not None and (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        raise ValueError("invalid artifact size")
    entry_id = ledger.record_artifact(
        _string_field(payload.get("path")),
        state=state,
        required=_required(payload, default=False),
        revision=_event_revision(payload, event.get("seq")),
        sha256=artifact_sha,
        size_bytes=size_bytes,
        source_tool=_string_field(
            payload.get("source_tool_name")
            or payload.get("source_tool")
            or event.get("tool_name")
            or None,
            required=False,
        ),
        receipt_sha256=_valid_sha256(payload.get("receipt_sha256")),
        reason_code=reason,
    )
    return AgentEventApplication(
        event_type=event_type,
        recognized=True,
        applied=True,
        code="receipt_recorded",
        ledger_entry_ids=(entry_id,),
    )


def _apply_evidence(
    ledger: RunContractLedger,
    event_type: str,
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> AgentEventApplication:
    suffix = event_type.rsplit(".", 1)[-1]
    default_state = (
        suffix
        if suffix in _QUALITY_VALUES
        else QualityState.VERIFIED.value
    )
    state = _quality(payload.get("quality"), default=default_state)
    receipt_sha = _valid_sha256(payload.get("receipt_sha256"))
    reason = _reason_code(
        payload,
        fallback=(
            ""
            if state == QualityState.VERIFIED.value
            else f"evidence_{state}"
        ),
    )
    if state == QualityState.VERIFIED.value and not receipt_sha:
        state = QualityState.DEGRADED.value
        reason = "evidence_receipt_missing"
    entry_id = ledger.record_evidence(
        _string_field(payload.get("evidence_id")),
        state=state,
        source_kind=_string_field(payload.get("source_kind")),
        required=_required(payload, default=True),
        revision=_event_revision(payload, event.get("seq")),
        receipt_sha256=receipt_sha,
        reason_code=reason,
    )
    return AgentEventApplication(
        event_type=event_type,
        recognized=True,
        applied=True,
        code="receipt_recorded",
        ledger_entry_ids=(entry_id,),
    )


def apply_agent_event(
    lifecycle: RunLifecycleMachine,
    ledger: RunContractLedger,
    event: Any,
) -> AgentEventApplication:
    """Safely project one existing Harness event into both contracts.

    Required common fields for recognized events are ``type=agent_event``,
    ``event_type``, and the matching ``run_id``. Lifecycle and versioned
    artifact/evidence events also require a non-negative integer ``seq``.
    """

    if not isinstance(event, Mapping):
        return _ignored("", "ignored_non_mapping")
    if event.get("type") != "agent_event":
        return _ignored("", "ignored_non_agent_event")
    event_type = str(event.get("event_type") or "").strip()
    if event_type not in _RECOGNIZED_EVENTS:
        return _ignored(event_type, "ignored_unknown_event")
    if lifecycle.run_id != ledger.run_id:
        return _rejected(event_type, "contract_run_id_mismatch")
    event_run_id = str(event.get("run_id") or "").strip()
    if event_run_id != lifecycle.run_id:
        return _rejected(event_type, "event_run_id_mismatch")
    payload = _payload(event)
    try:
        if event_type in _LIFECYCLE_EVENTS:
            return _apply_lifecycle(
                lifecycle,
                ledger,
                event_type,
                event,
                payload,
            )
        if event_type in _TOOL_EVENTS:
            return _apply_tool(ledger, event_type, event, payload)
        if event_type in _NODE_EVENTS:
            return _apply_node(ledger, event_type, event, payload)
        if event_type in _ARTIFACT_EVENTS:
            return _apply_artifact(
                ledger,
                event_type,
                event,
                payload,
            )
        if event_type in _EVIDENCE_EVENTS:
            return _apply_evidence(
                ledger,
                event_type,
                event,
                payload,
            )
    except (TypeError, ValueError, RuntimeError):
        return _rejected(event_type, "invalid_event_contract")
    return _ignored(event_type, "ignored_unknown_event")


__all__ = [
    "AgentEventApplication",
    "apply_agent_event",
]
