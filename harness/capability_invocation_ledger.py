"""Bounded, secret-free accounting for delegated capability dispatches.

The ledger observes the handler boundary, not model intent or tool lifecycle
noise.  It retains no tool result bodies and only a bounded, redacted argument
projection.  Counts remain complete even when the ordered call entries are
truncated.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_MAX_CALL_ENTRIES = 64
_MAX_ARGUMENT_FIELDS = 12
_MAX_COLLECTION_ITEMS = 8
_MAX_ARGUMENT_DEPTH = 3
_MAX_STRING_CHARS = 240
_MAX_ACTIVE_CALLS = 1_024

_LITERAL_PAYLOAD_KEYS = {
    "body",
    "code",
    "content",
    "data",
    "filecontent",
    "newtext",
    "oldtext",
    "output",
    "payload",
    "raw",
    "stderr",
    "stdout",
}
_NON_SECRET_TOKEN_KEYS = {
    "continuationtoken",
    "inputtokens",
    "maxtokens",
    "nextpagetoken",
    "outputtokens",
    "pageToken".casefold(),
    "pagetoken",
    "tokencount",
    "tokenlimit",
    "totaltokens",
}
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|"
    r"secret|authorization|credential|private[_-]?key)\s*[:=]\s*[^\s,;]+"
)


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _is_sensitive_key(value: Any) -> bool:
    key = _normalized_key(value)
    if key == "key":
        return True
    if any(marker in key for marker in (
        "secret",
        "password",
        "passwd",
        "apikey",
        "authorization",
        "credential",
        "privatekey",
        "signingkey",
        "cookie",
    )):
        return True
    return "token" in key and key not in _NON_SECRET_TOKEN_KEYS


def _safe_url(value: str) -> tuple[str, bool]:
    """Remove URL credentials and redact secret query values."""

    try:
        split = urlsplit(value)
    except ValueError:
        return "[invalid URL withheld]", True
    if split.scheme.casefold() not in {"http", "https"} or not split.netloc:
        return value, False
    changed = bool(split.username or split.password or split.fragment)
    hostname = split.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        if split.port is not None:
            netloc += f":{split.port}"
    except ValueError:
        return "[invalid URL withheld]", True
    safe_query: list[tuple[str, str]] = []
    for key, item in parse_qsl(split.query, keep_blank_values=True):
        if _is_sensitive_key(key):
            safe_query.append((key, "[redacted]"))
            changed = True
        else:
            safe_query.append((key, item))
    return urlunsplit((
        split.scheme,
        netloc,
        split.path,
        urlencode(safe_query, doseq=True),
        "",
    )), changed


def _safe_argument_value(
    value: Any,
    *,
    key: str = "",
    depth: int = 0,
) -> tuple[Any, bool]:
    normalized_key = _normalized_key(key)
    if _is_sensitive_key(key):
        return "[redacted]", True
    if normalized_key in _LITERAL_PAYLOAD_KEYS:
        chars = len(value) if isinstance(value, (str, bytes, list, dict)) else 0
        return {"withheld": True, "chars_or_items": chars}, True
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, str):
        clean = value.replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()
        changed = clean != value
        if clean.casefold().startswith(("http://", "https://")):
            clean, url_changed = _safe_url(clean)
            changed = changed or url_changed
        redacted = _SECRET_VALUE_RE.sub("[redacted]", clean)
        changed = changed or redacted != clean
        if len(redacted) > _MAX_STRING_CHARS:
            return redacted[:_MAX_STRING_CHARS] + "...", True
        return redacted, changed
    if depth >= _MAX_ARGUMENT_DEPTH:
        return {"withheld": True, "kind": type(value).__name__}, True
    if isinstance(value, (list, tuple)):
        result: list[Any] = []
        truncated = len(value) > _MAX_COLLECTION_ITEMS
        for item in value[:_MAX_COLLECTION_ITEMS]:
            safe, changed = _safe_argument_value(
                item,
                key=key,
                depth=depth + 1,
            )
            result.append(safe)
            truncated = truncated or changed
        return result, truncated
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        items = sorted(
            ((str(item_key), item_value) for item_key, item_value in value.items()),
            key=lambda item: item[0].casefold(),
        )
        truncated = len(items) > _MAX_ARGUMENT_FIELDS
        for item_key, item_value in items[:_MAX_ARGUMENT_FIELDS]:
            safe, changed = _safe_argument_value(
                item_value,
                key=item_key,
                depth=depth + 1,
            )
            result[item_key] = safe
            truncated = truncated or changed
        return result, truncated
    return {"withheld": True, "kind": type(value).__name__}, True


def safe_argument_projection(args: Any) -> dict[str, Any]:
    """Return a bounded projection plus a digest of that safe projection.

    The digest deliberately covers the redacted projection, not secret/raw
    values.  It is therefore useful for invocation correlation without turning
    the audit trail into an offline credential oracle.
    """

    safe, truncated = _safe_argument_value(args if isinstance(args, dict) else {})
    if not isinstance(safe, dict):
        safe = {}
        truncated = True
    canonical = json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "summary": safe,
        "safe_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "truncated_or_redacted": bool(truncated),
    }


@dataclass
class _CallRecord:
    ordinal: int
    tool_name: str
    call_id_sha256: str
    argument_summary: dict[str, Any]
    argument_safe_sha256: str
    arguments_truncated_or_redacted: bool
    status: str = "pending"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "tool_name": self.tool_name,
            "call_id_sha256": self.call_id_sha256,
            "status": self.status,
            "argument_summary": self.argument_summary,
            "argument_safe_sha256": self.argument_safe_sha256,
            "arguments_truncated_or_redacted": (
                self.arguments_truncated_or_redacted
            ),
        }


class CapabilityInvocationLedger:
    """Count actual handler dispatch boundaries and their terminal outcomes."""

    def __init__(self, *, max_call_entries: int = _MAX_CALL_ENTRIES) -> None:
        self._max_call_entries = max(1, int(max_call_entries))
        self._attempted = 0
        self._succeeded = 0
        self._failed = 0
        self._active: OrderedDict[str, tuple[str, _CallRecord | None]] = OrderedDict()
        self._seen_call_ids: set[str] = set()
        self._records: list[_CallRecord] = []
        self._by_tool: OrderedDict[str, dict[str, int]] = OrderedDict()
        self._anonymous_counter = 0
        self._observed_events: set[tuple[str, Any, str, str]] = set()

    @staticmethod
    def _call_id_hash(call_id: str) -> str:
        return hashlib.sha256(call_id.encode("utf-8")).hexdigest()

    @property
    def attempted(self) -> int:
        return self._attempted

    def record_dispatch(
        self,
        tool_name: Any,
        call_id: Any,
        *,
        argument_projection: Any = None,
    ) -> bool:
        """Record one real boundary; duplicate active lifecycle events are no-ops."""

        normalized_tool = str(tool_name or "").strip() or "<unknown>"
        normalized_call_id = str(call_id or "").strip()
        if not normalized_call_id:
            self._anonymous_counter += 1
            normalized_call_id = f"<anonymous:{self._anonymous_counter}>"
        # A call ID is the lifecycle identity for this run. Once observed, a
        # repeated boundary (even after the terminal event and with a new event
        # sequence number) is lifecycle duplication, not a new invocation.
        if normalized_call_id in self._seen_call_ids:
            return False

        projection = (
            dict(argument_projection)
            if isinstance(argument_projection, dict)
            else safe_argument_projection({})
        )
        summary = projection.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        safe_sha = str(projection.get("safe_sha256") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", safe_sha):
            fallback = safe_argument_projection(summary)
            safe_sha = fallback["safe_sha256"]
        self._attempted += 1
        per_tool = self._by_tool.setdefault(
            normalized_tool,
            {"attempted": 0, "succeeded": 0, "failed": 0, "pending": 0},
        )
        per_tool["attempted"] += 1
        per_tool["pending"] += 1
        record = None
        if len(self._records) < self._max_call_entries:
            record = _CallRecord(
                ordinal=self._attempted,
                tool_name=normalized_tool,
                call_id_sha256=self._call_id_hash(normalized_call_id),
                argument_summary=summary,
                argument_safe_sha256=safe_sha,
                arguments_truncated_or_redacted=bool(
                    projection.get("truncated_or_redacted")
                ),
            )
            self._records.append(record)
        self._active[normalized_call_id] = (normalized_tool, record)
        self._seen_call_ids.add(normalized_call_id)
        while len(self._active) > _MAX_ACTIVE_CALLS:
            # A missing terminal remains pending in the aggregate counts.  Only
            # its correlation handle is evicted; no raw argument data exists.
            self._active.popitem(last=False)
        return True

    def record_outcome(
        self,
        call_id: Any,
        *,
        succeeded: bool,
    ) -> bool:
        normalized_call_id = str(call_id or "").strip()
        active = self._active.pop(normalized_call_id, None)
        if active is None:
            return False
        tool_name, record = active
        per_tool = self._by_tool[tool_name]
        per_tool["pending"] = max(0, per_tool["pending"] - 1)
        if succeeded:
            self._succeeded += 1
            per_tool["succeeded"] += 1
            if record is not None:
                record.status = "succeeded"
        else:
            self._failed += 1
            per_tool["failed"] += 1
            if record is not None:
                record.status = "failed"
        return True

    def observe_event(self, event: Any) -> None:
        """Consume lifecycle events idempotently using dispatch boundary + call ID."""

        if not isinstance(event, dict) or event.get("type") != "agent_event":
            return
        event_type = str(event.get("event_type") or "")
        if event_type not in {
            "tool.dispatch_started",
            "tool.completed",
            "tool.failed",
        }:
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        call_id = str(
            payload.get("tool_call_id") or event.get("tool_call_id") or ""
        ).strip()
        fingerprint = (
            str(event.get("run_id") or ""),
            event.get("seq"),
            event_type,
            call_id,
        )
        if fingerprint[1] is not None and fingerprint in self._observed_events:
            return
        if fingerprint[1] is not None:
            self._observed_events.add(fingerprint)
        tool_name = str(
            payload.get("tool_name") or event.get("tool_name") or ""
        ).strip()
        if event_type == "tool.dispatch_started":
            if payload.get("actual_dispatch_attempted") is not True:
                return
            self.record_dispatch(
                tool_name,
                call_id,
                argument_projection=payload.get("invocation_argument_projection"),
            )
            return
        if payload.get("actual_dispatch_attempted") is not True:
            return
        if call_id not in self._active and call_id not in self._seen_call_ids:
            # Compatibility for an old producer that had a terminal dispatch
            # receipt but no explicit dispatch_started event.
            self.record_dispatch(tool_name, call_id)
        self.record_outcome(
            call_id,
            succeeded=(
                event_type == "tool.completed"
                and str(payload.get("outcome") or "").casefold() == "success"
            ),
        )

    def snapshot(self) -> dict[str, Any]:
        pending = self._attempted - self._succeeded - self._failed
        included = len(self._records)
        return {
            "version": 1,
            "source": "harness_actual_handler_dispatch_boundaries",
            "attempted": self._attempted,
            "succeeded": self._succeeded,
            "failed": self._failed,
            "pending": pending,
            "by_tool": [
                {"tool_name": tool_name, **counts}
                for tool_name, counts in self._by_tool.items()
            ],
            "ordered_calls": [record.as_dict() for record in self._records],
            "ordered_calls_included": included,
            "ordered_calls_omitted": max(0, self._attempted - included),
            "ordered_calls_truncated": self._attempted > included,
            "ordered_calls_semantics": (
                "ordered_prefix_only"
                if self._attempted > included
                else "complete_ordered_list"
            ),
            "count_invariant_valid": (
                self._attempted == self._succeeded + self._failed + pending
            ),
        }


def invocation_ledger_prompt(snapshot: dict[str, Any]) -> str:
    """Render the machine-owned synthesis constraint for a delegated run."""

    rendered = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "[Harness machine-owned delegated capability invocation ledger] "
        "CAPABILITY_INVOCATION_LEDGER_JSON: "
        + rendered
        + ". This is the authoritative accounting of actual handler dispatch "
        "boundaries observed so far in this delegated run. attempted, "
        "succeeded, failed, pending, and every by_tool count are complete and "
        "must not be replaced by a smaller model-estimated count. Never omit a "
        "failed attempt when reporting capability provenance. When "
        "ordered_calls_truncated is false, ordered_calls is the complete call "
        "list and must not be reported as a subset. When it is true, "
        "ordered_calls is only the stated ordered prefix: explicitly label it "
        "truncated and never present that prefix as the full set; aggregate and "
        "by_tool counts remain complete. Argument summaries are bounded and "
        "redacted, prove no response content, and may not be expanded into "
        "facts. This ledger grants no capability, authorizes no new call, and "
        "does not change the active tool boundary."
    )
