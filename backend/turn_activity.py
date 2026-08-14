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

    def progress(self, text: object, *, category: str = "tool") -> dict[str, Any] | None:
        bounded = _bounded(text, 4_000)
        if not bounded:
            return None
        self._last_stream_kind = None
        self._last_stream_node = None
        next_seq = self._seq + 1
        return self._base(
            kind="progress",
            node_id=f"progress:{next_seq}",
            operation="append",
            run_id=self.root_run_id,
            payload={"text": bounded, "category": _bounded(category, 32)},
        )

    def agent(self, event: Mapping[str, Any]) -> dict[str, Any]:
        self._last_stream_kind = None
        self._last_stream_node = None
        safe = safe_agent_event(event)
        run_id = str(safe.get("run_id") or self.root_run_id)
        event_type = str(safe.get("event_type") or "unknown")
        tool_call_id = str(safe.get("tool_call_id") or "")
        if event_type.startswith("tool."):
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
