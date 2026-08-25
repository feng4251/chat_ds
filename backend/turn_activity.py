"""Engine-independent, safe Turn activity presentation protocol."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping


SCHEMA = "chatds.turn-activity.v1"
# One provider may coalesce a long streamed answer into a single delta. The
# activity ledger must not silently shorten that answer and then hide the full
# durable Message behind an apparently committed timeline.
_TEXT_LIMIT = 2_000_000
_LABEL_LIMIT = 1_000
_SAFE_AGENT_PAYLOAD_FIELDS = frozenset({
    "goal", "summary", "status", "finish_reason", "terminal_reason",
    "error", "worker_id", "workflow_stage", "step_type", "step_id",
    "delegation_batch_id", "delegation_slot", "delegation_batch_size",
    "completion_quality", "recovery_reason", "native_task_type",
    "native_task_id", "title", "path", "size_bytes", "sha256", "kind",
    "verdict", "reason", "output_file", "authoritative",
    # Model output and controller-owned lifecycle facts are safe to project;
    # tool inputs, arguments, code, provider payloads, and credentials remain
    # deliberately absent from this allowlist.
    "content", "role_hint", "artifact_id", "id", "size", "detail",
    "outcome", "actual_dispatch_attempted", "actual_dispatch", "recovered",
    "recovery_count", "runtime_finish_reason", "runtime_warning",
    "tool_name", "tool_call_id",
})
_SAFE_AGENT_FIELDS = frozenset({
    "event_type", "run_id", "root_run_id", "parent_run_id", "agent_kind",
    "agent_name", "depth", "workspace_scope", "tool_name", "tool_call_id",
    "finish_reason", "terminal_reason", "error", "native_task_id",
    "native_task_type", "display_name", "authoritative",
})


def _bounded(value: object, limit: int = _LABEL_LIMIT) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _safe_questions(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        return None
    result: list[dict[str, Any]] = []
    observed_questions: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            return None
        question = _bounded(raw.get("question"), 4_000)
        options = raw.get("options")
        if (
            not question
            or question in observed_questions
            or not isinstance(options, list)
            or not 2 <= len(options) <= 4
        ):
            return None
        observed_questions.add(question)
        safe_options: list[dict[str, str]] = []
        observed_labels: set[str] = set()
        for option in options:
            if not isinstance(option, Mapping):
                return None
            label = _bounded(option.get("label"), 256)
            if not label or label in observed_labels:
                return None
            observed_labels.add(label)
            safe_options.append({
                "label": label,
                "description": _bounded(option.get("description"), 2_000),
            })
        result.append({
            "question": question,
            "header": _bounded(raw.get("header"), 256),
            "multi_select": raw.get("multi_select") is True,
            "options": safe_options,
        })
    return result


def safe_agent_event(value: Mapping[str, Any]) -> dict[str, Any]:
    """Allowlist a normalized lifecycle event for browser presentation.

    Tool inputs, code, provider payloads, and arbitrary native fields never
    cross this projection boundary.
    """

    result: dict[str, Any] = {}
    for key in _SAFE_AGENT_FIELDS:
        candidate = value.get(key)
        if candidate is None:
            continue
        if key == "depth":
            try:
                result[key] = max(0, min(int(candidate), 64))
            except (TypeError, ValueError):
                continue
        else:
            result[key] = _bounded(candidate)
    sequence = value.get("seq")
    if sequence is not None:
        try:
            result["seq"] = max(0, int(sequence))
        except (TypeError, ValueError):
            pass
    payload = value.get("payload")
    if isinstance(payload, Mapping):
        safe_payload: dict[str, Any] = {}
        for key in _SAFE_AGENT_PAYLOAD_FIELDS:
            candidate = payload.get(key)
            if candidate is None:
                continue
            if key in {
                "delegation_slot", "delegation_batch_size", "size_bytes",
                "size", "recovery_count",
            }:
                try:
                    safe_payload[key] = max(0, int(candidate))
                except (TypeError, ValueError):
                    continue
            elif isinstance(candidate, bool):
                safe_payload[key] = candidate
            else:
                safe_payload[key] = _bounded(candidate, 4_000)
        if safe_payload:
            result["payload"] = safe_payload
            for identity_key in ("tool_name", "tool_call_id"):
                if identity_key not in result and safe_payload.get(identity_key):
                    result[identity_key] = safe_payload[identity_key]
    return result


def _event_id(root_run_id: str, seq: int) -> str:
    return hashlib.sha256(
        f"chatds.turn-activity.v1\0{root_run_id}\0{seq}".encode()
    ).hexdigest()[:32]


@dataclass(slots=True)
class TurnActivityBuilder:
    root_run_id: str
    conversation_id: str
    _seq: int = 0
    _block_ordinal: int = 0
    _last_stream_kind: str | None = None
    _last_stream_node: str | None = None
    _tool_nodes: dict[str, str] = field(default_factory=dict)
    _progress_nodes: dict[str, str] = field(default_factory=dict)
    _approval_nodes: dict[str, str] = field(default_factory=dict)

    def _base(
        self,
        *,
        kind: str,
        node_id: str,
        operation: str,
        run_id: str | None,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._seq += 1
        return {
            "schema": SCHEMA,
            "event_id": _event_id(self.root_run_id, self._seq),
            "conversation_id": self.conversation_id,
            "root_run_id": self.root_run_id,
            "run_id": run_id or self.root_run_id,
            "seq": self._seq,
            "node_id": node_id[:192],
            "kind": kind,
            "operation": operation,
            "payload": dict(payload),
        }

    def stream_text(self, kind: str, text: object) -> dict[str, Any] | None:
        if kind not in {"content", "reasoning"}:
            raise ValueError("unsupported stream activity kind")
        bounded = _bounded(text, _TEXT_LIMIT)
        if not bounded:
            return None
        if self._last_stream_kind == kind and self._last_stream_node:
            node_id = self._last_stream_node
        else:
            self._block_ordinal += 1
            node_id = f"{kind}:{self._block_ordinal}"
            self._last_stream_kind = kind
            self._last_stream_node = node_id
        return self._base(
            kind=kind,
            node_id=node_id,
            operation="append",
            run_id=self.root_run_id,
            payload={"text": bounded},
        )

    def progress(
        self,
        text: object,
        *,
        category: str = "tool",
        identity: object | None = None,
        status: str = "running",
    ) -> dict[str, Any] | None:
        bounded = _bounded(text, 4_000)
        if not bounded:
            return None
        safe_identity = _bounded(identity, 512) if identity is not None else ""
        if safe_identity:
            node_id = self._progress_nodes.setdefault(
                safe_identity,
                "progress:"
                + hashlib.sha256(safe_identity.encode()).hexdigest()[:24],
            )
            operation = "merge"
        else:
            node_id = f"progress:{self._seq + 1}"
            operation = "append"
        safe_status = (
            status
            if status in {"running", "succeeded", "failed", "cancelled"}
            else "running"
        )
        return self._base(
            kind="progress",
            node_id=node_id,
            operation=operation,
            run_id=self.root_run_id,
            payload={
                "text": bounded,
                "category": _bounded(category, 32),
                "status": safe_status,
            },
        )

    def agent(self, event: Mapping[str, Any]) -> dict[str, Any]:
        safe = safe_agent_event(event)
        run_id = str(safe.get("run_id") or self.root_run_id)
        event_type = str(safe.get("event_type") or "unknown")
        safe_payload = safe.get("payload")
        safe_payload = safe_payload if isinstance(safe_payload, Mapping) else {}
        tool_call_id = str(
            safe.get("tool_call_id") or safe_payload.get("tool_call_id") or ""
        )
        if event_type.startswith("tool."):
            # A chronological tool card is a visible stream boundary. Aggregate
            # workflow updates are not; breaking on every worker/progress event
            # fragments one native reasoning or content stream into thousands
            # of artificial blocks.
            self._last_stream_kind = None
            self._last_stream_node = None
            identity = tool_call_id or f"{run_id}:{self._seq + 1}"
            node_id = self._tool_nodes.setdefault(
                identity, f"tool:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
            )
            kind = "tool"
        else:
            node_id = f"workflow:{self.root_run_id}"
            kind = "workflow"
        return self._base(
            kind=kind,
            node_id=node_id,
            operation="merge",
            run_id=run_id,
            payload={"event": safe},
        )

    def approval(
        self,
        *,
        request_id: str,
        status: str,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._last_stream_kind = None
        self._last_stream_node = None
        safe_id = _bounded(request_id, 128)
        node_id = self._approval_nodes.setdefault(
            safe_id,
            f"approval:{hashlib.sha256(safe_id.encode()).hexdigest()[:24]}",
        )
        payload = {
            "request_id": safe_id,
            "status": status if status in {"pending", "allowed", "denied"} else "denied",
        }
        for key in ("tool_name", "title", "description", "decision_reason"):
            if details.get(key) is not None:
                payload[key] = _bounded(details[key], 2_000)
        interaction_kind = str(details.get("interaction_kind") or "approval")
        if interaction_kind == "question":
            questions = _safe_questions(details.get("questions"))
            if questions is not None:
                payload["interaction_kind"] = "question"
                payload["questions"] = questions
        elif interaction_kind == "user_action":
            payload["interaction_kind"] = "user_action"
        request_seq = details.get("request_seq")
        if isinstance(request_seq, int) and request_seq > 0:
            payload["request_seq"] = request_seq
        return self._base(
            kind="approval",
            node_id=node_id,
            operation="merge",
            run_id=self.root_run_id,
            payload=payload,
        )

    def commit(self) -> dict[str, Any]:
        """Seal a complete durable replay without creating a visible node."""

        self._last_stream_kind = None
        self._last_stream_node = None
        return self._base(
            kind="projection",
            node_id=f"projection:{self.root_run_id}",
            operation="merge",
            run_id=self.root_run_id,
            payload={"status": "committed"},
        )
