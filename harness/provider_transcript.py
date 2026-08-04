"""Provider-facing conversation transaction invariants.

The Harness keeps richer workflow state than an LLM provider understands,
but the provider transcript still has a small, strict protocol: an assistant
tool-call batch must be followed immediately by exactly one result for every
call before any ordinary user/assistant message is appended.  This module is
the single source of truth for that boundary.

It deliberately does not infer successful effects.  Active batches can be
closed with explicit *not dispatched* error results, while malformed legacy
history is quarantined by removing unresolved native call envelopes rather
than fabricating successful tool output.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Literal


@dataclass(frozen=True)
class TranscriptIssue:
    """One provider-protocol violation without message content."""

    code: str
    message_index: int
    tool_call_id: str = ""
    related_index: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            # ``code`` is a generic credential/debug redaction key elsewhere
            # in the Harness because it commonly contains executable source.
            # This typed value is an enum-like diagnostic, so use a distinct
            # name that remains visible in persisted safe debug events.
            "issue_code": self.code,
            "message_index": self.message_index,
            **(
                {"tool_call_id": self.tool_call_id}
                if self.tool_call_id else {}
            ),
            **(
                {"related_index": self.related_index}
                if self.related_index is not None else {}
            ),
        }


@dataclass(frozen=True)
class TranscriptAudit:
    issues: tuple[TranscriptIssue, ...] = ()
    tool_round_count: int = 0
    tool_call_count: int = 0
    tool_result_count: int = 0

    @property
    def valid(self) -> bool:
        return not self.issues

    def as_dict(self, *, issue_limit: int = 32) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "tool_round_count": self.tool_round_count,
            "tool_call_count": self.tool_call_count,
            "tool_result_count": self.tool_result_count,
            "issue_count": len(self.issues),
            "issues": [
                issue.as_dict() for issue in self.issues[:issue_limit]
            ],
            "issues_omitted": max(0, len(self.issues) - issue_limit),
        }


@dataclass
class TranscriptRepairReport:
    hoisted_tool_results: int = 0
    removed_tool_calls: int = 0
    removed_tool_results: int = 0
    removed_assistant_messages: int = 0
    duplicate_tool_call_ids: int = 0
    renamed_tool_call_ids: int = 0
    malformed_tool_calls: int = 0
    before_messages: int = 0
    after_messages: int = 0
    audit_before: TranscriptAudit | None = None
    audit_after: TranscriptAudit | None = None

    @property
    def changed(self) -> bool:
        return any((
            self.hoisted_tool_results,
            self.removed_tool_calls,
            self.removed_tool_results,
            self.removed_assistant_messages,
            self.duplicate_tool_call_ids,
            self.renamed_tool_call_ids,
            self.malformed_tool_calls,
            self.before_messages != self.after_messages,
        ))

    def as_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "before_messages": self.before_messages,
            "after_messages": self.after_messages,
            "hoisted_tool_results": self.hoisted_tool_results,
            "removed_tool_calls": self.removed_tool_calls,
            "removed_tool_results": self.removed_tool_results,
            "removed_assistant_messages": self.removed_assistant_messages,
            "duplicate_tool_call_ids": self.duplicate_tool_call_ids,
            "renamed_tool_call_ids": self.renamed_tool_call_ids,
            "malformed_tool_calls": self.malformed_tool_calls,
            "audit_before": (
                self.audit_before.as_dict()
                if self.audit_before is not None else None
            ),
            "audit_after": (
                self.audit_after.as_dict()
                if self.audit_after is not None else None
            ),
        }


@dataclass(frozen=True)
class ToolRoundCloseReport:
    assistant_index: int
    expected_tool_call_ids: tuple[str, ...]
    existing_tool_result_ids: tuple[str, ...]
    synthetic_not_dispatched_ids: tuple[str, ...] = ()
    guidance_count: int = 0
    abort_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "assistant_index": self.assistant_index,
            "expected_tool_call_count": len(self.expected_tool_call_ids),
            "existing_tool_result_count": len(self.existing_tool_result_ids),
            "synthetic_not_dispatched_count": len(
                self.synthetic_not_dispatched_ids
            ),
            "guidance_count": self.guidance_count,
            "aborted": self.abort_reason is not None,
            **(
                {"abort_reason": self.abort_reason}
                if self.abort_reason is not None else {}
            ),
        }


def _tool_call_id(tool_call: Any) -> str:
    if not isinstance(tool_call, dict):
        return ""
    value = tool_call.get("id") or tool_call.get("call_id")
    return str(value or "").strip()


def _tool_result_id(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("tool_call_id") or "").strip()


def audit_provider_transcript(
    messages: Iterable[dict[str, Any]],
    *,
    require_global_unique_ids: bool = True,
) -> TranscriptAudit:
    """Audit OpenAI-shaped internal history as contiguous API transactions."""

    rows = list(messages)
    issues: list[TranscriptIssue] = []
    seen_call_ids: dict[str, int] = {}
    seen_result_ids: dict[str, int] = {}
    pending: dict[str, int] = {}
    tool_round_count = 0
    tool_call_count = 0
    tool_result_count = 0

    for index, message in enumerate(rows):
        if not isinstance(message, dict):
            if pending:
                for call_id, call_index in pending.items():
                    issues.append(TranscriptIssue(
                        "tool_result_batch_interleaved",
                        index,
                        call_id,
                        call_index,
                    ))
                pending.clear()
            continue
        role = str(message.get("role") or "")

        if pending and role != "tool":
            for call_id, call_index in pending.items():
                issues.append(TranscriptIssue(
                    "tool_result_batch_interleaved",
                    index,
                    call_id,
                    call_index,
                ))
            pending.clear()

        if role == "assistant" and message.get("tool_calls") is not None:
            calls = message.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                if calls not in (None, []):
                    issues.append(TranscriptIssue(
                        "malformed_tool_call_batch", index
                    ))
                continue
            tool_round_count += 1
            local_ids: set[str] = set()
            for call in calls:
                call_id = _tool_call_id(call)
                if not call_id:
                    issues.append(TranscriptIssue(
                        "missing_tool_call_id", index
                    ))
                    continue
                tool_call_count += 1
                if call_id in local_ids:
                    issues.append(TranscriptIssue(
                        "duplicate_tool_call_id_in_batch",
                        index,
                        call_id,
                        index,
                    ))
                    continue
                local_ids.add(call_id)
                prior_index = seen_call_ids.get(call_id)
                if prior_index is not None and require_global_unique_ids:
                    issues.append(TranscriptIssue(
                        "duplicate_tool_call_id_in_transcript",
                        index,
                        call_id,
                        prior_index,
                    ))
                    continue
                seen_call_ids.setdefault(call_id, index)
                pending[call_id] = index
            continue

        if role != "tool":
            continue

        tool_result_count += 1
        result_id = _tool_result_id(message)
        if not result_id:
            issues.append(TranscriptIssue(
                "missing_tool_result_id", index
            ))
            continue
        prior_result_index = seen_result_ids.get(result_id)
        if prior_result_index is not None and require_global_unique_ids:
            issues.append(TranscriptIssue(
                "duplicate_tool_result_id",
                index,
                result_id,
                prior_result_index,
            ))
            continue
        seen_result_ids.setdefault(result_id, index)
        if result_id not in pending:
            issues.append(TranscriptIssue(
                "orphan_tool_result", index, result_id
            ))
            continue
        del pending[result_id]

    for call_id, call_index in pending.items():
        issues.append(TranscriptIssue(
            "missing_tool_result", len(rows), call_id, call_index
        ))

    return TranscriptAudit(
        issues=tuple(issues),
        tool_round_count=tool_round_count,
        tool_call_count=tool_call_count,
        tool_result_count=tool_result_count,
    )


def canonicalize_legacy_provider_transcript(
    messages: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], TranscriptRepairReport]:
    """Repair only historical shape, never historical effects.

    Result messages that were interleaved with ordinary messages are hoisted
    beside their assistant batch.  Calls without a unique result are removed
    from the native provider envelope; no result is invented.  Orphan and
    duplicate result messages are discarded.  Ordinary message order within
    the remainder of a round is preserved.
    """

    rows = [copy.deepcopy(message) for message in messages]
    report = TranscriptRepairReport(
        before_messages=len(rows),
        audit_before=audit_provider_transcript(rows),
    )
    output: list[dict[str, Any]] = []
    seen_global_call_ids: set[str] = set()
    index = 0

    while index < len(rows):
        message = rows[index]
        if not isinstance(message, dict):
            output.append(message)
            index += 1
            continue
        if message.get("role") == "tool":
            report.removed_tool_results += 1
            index += 1
            continue
        calls = message.get("tool_calls")
        if message.get("role") != "assistant" or not isinstance(calls, list):
            output.append(message)
            index += 1
            continue
        if not calls:
            cleaned = dict(message)
            cleaned.pop("tool_calls", None)
            output.append(cleaned)
            index += 1
            continue

        segment_end = index + 1
        while (
            segment_end < len(rows)
            and not (
                isinstance(rows[segment_end], dict)
                and rows[segment_end].get("role") == "assistant"
            )
        ):
            segment_end += 1
        segment = rows[index + 1:segment_end]

        results_by_id: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        ordinary: list[dict[str, Any]] = []
        for offset, row in enumerate(segment, start=index + 1):
            if isinstance(row, dict) and row.get("role") == "tool":
                result_id = _tool_result_id(row)
                if not result_id:
                    report.removed_tool_results += 1
                    continue
                results_by_id.setdefault(result_id, []).append((offset, row))
            else:
                ordinary.append(row)

        retained_calls: list[dict[str, Any]] = []
        retained_results: list[dict[str, Any]] = []
        consumed_result_offsets: set[int] = set()
        local_ids: set[str] = set()
        for call in calls:
            call_id = _tool_call_id(call)
            if not call_id:
                report.malformed_tool_calls += 1
                report.removed_tool_calls += 1
                continue
            if call_id in local_ids:
                report.duplicate_tool_call_ids += 1
                report.removed_tool_calls += 1
                continue
            local_ids.add(call_id)
            matches = results_by_id.get(call_id) or []
            if len(matches) != 1:
                report.removed_tool_calls += 1
                report.removed_tool_results += len(matches)
                consumed_result_offsets.update(offset for offset, _ in matches)
                continue
            result_offset, result_message = matches[0]
            effective_call_id = call_id
            retained_call = call
            retained_result = result_message
            if call_id in seen_global_call_ids:
                report.duplicate_tool_call_ids += 1
                effective_call_id = _derived_unique_tool_call_id(
                    call_id,
                    round_index=index,
                    used_ids=seen_global_call_ids | local_ids,
                )
                retained_call = copy.deepcopy(call)
                if "id" in retained_call:
                    retained_call["id"] = effective_call_id
                else:
                    retained_call["call_id"] = effective_call_id
                retained_result = copy.deepcopy(result_message)
                retained_result["tool_call_id"] = effective_call_id
                report.renamed_tool_call_ids += 1
            seen_global_call_ids.add(effective_call_id)
            retained_calls.append(retained_call)
            retained_results.append(retained_result)
            consumed_result_offsets.add(result_offset)
            if result_offset != index + len(retained_results):
                report.hoisted_tool_results += 1

        for matches in results_by_id.values():
            for result_offset, _row in matches:
                if result_offset not in consumed_result_offsets:
                    report.removed_tool_results += 1

        cleaned_assistant = dict(message)
        if retained_calls:
            cleaned_assistant["tool_calls"] = retained_calls
            output.append(cleaned_assistant)
            output.extend(retained_results)
        else:
            cleaned_assistant.pop("tool_calls", None)
            if cleaned_assistant.get("content") not in (None, ""):
                output.append(cleaned_assistant)
            else:
                report.removed_assistant_messages += 1
        output.extend(ordinary)
        index = segment_end

    report.after_messages = len(output)
    report.audit_after = audit_provider_transcript(output)
    return output, report


def _derived_unique_tool_call_id(
    original: str,
    *,
    round_index: int,
    used_ids: set[str],
) -> str:
    """Derive a stable bounded provider-only ID for a repeated later round."""

    base = str(original or "tool-call")[:64]
    attempt = 0
    while True:
        digest = hashlib.sha256(
            f"{round_index}:{attempt}:{original}".encode("utf-8")
        ).hexdigest()[:16]
        candidate = f"{base}__h{digest}"
        if candidate not in used_ids:
            return candidate
        attempt += 1


def project_unique_tool_call_ids(
    messages: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], TranscriptRepairReport]:
    """Make cross-round IDs provider-global without repairing other defects.

    Some OpenAI-compatible providers reuse a tool-call ID in a later response,
    while stricter APIs require transcript-global uniqueness.  A structurally
    valid round can be projected to the strict dialect by renaming the later
    call and its already-paired result in the outbound copy.  Interleaving,
    missing results, same-batch duplicates, and orphans are never repaired by
    this function and remain fail-closed at the caller.
    """

    rows = [copy.deepcopy(message) for message in messages]
    report = TranscriptRepairReport(
        before_messages=len(rows),
        after_messages=len(rows),
        audit_before=audit_provider_transcript(
            rows,
            require_global_unique_ids=False,
        ),
    )
    if report.audit_before is not None and not report.audit_before.valid:
        report.audit_after = audit_provider_transcript(rows)
        return rows, report

    used_ids: set[str] = set()
    index = 0
    while index < len(rows):
        message = rows[index]
        calls = (
            message.get("tool_calls")
            if isinstance(message, dict)
            and message.get("role") == "assistant"
            else None
        )
        if not isinstance(calls, list) or not calls:
            index += 1
            continue

        result_end = index + 1
        results_by_id: dict[str, dict[str, Any]] = {}
        while (
            result_end < len(rows)
            and isinstance(rows[result_end], dict)
            and rows[result_end].get("role") == "tool"
        ):
            result_id = _tool_result_id(rows[result_end])
            results_by_id[result_id] = rows[result_end]
            result_end += 1

        for call in calls:
            call_id = _tool_call_id(call)
            if call_id not in used_ids:
                used_ids.add(call_id)
                continue
            replacement_id = _derived_unique_tool_call_id(
                call_id,
                round_index=index,
                used_ids=used_ids,
            )
            if "id" in call:
                call["id"] = replacement_id
            else:
                call["call_id"] = replacement_id
            results_by_id[call_id]["tool_call_id"] = replacement_id
            used_ids.add(replacement_id)
            report.duplicate_tool_call_ids += 1
            report.renamed_tool_call_ids += 1
        index = result_end

    report.audit_after = audit_provider_transcript(rows)
    return rows, report


def tool_round_spans(
    messages: Iterable[dict[str, Any]],
) -> tuple[tuple[int, int], ...]:
    """Return half-open spans for contiguous assistant/tool transactions."""

    rows = list(messages)
    spans: list[tuple[int, int]] = []
    for index, message in enumerate(rows):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        end = index + 1
        while end < len(rows):
            row = rows[end]
            if not isinstance(row, dict) or row.get("role") != "tool":
                break
            end += 1
        spans.append((index, end))
    return tuple(spans)


def align_tool_round_boundary(
    messages: Iterable[dict[str, Any]],
    index: int,
    *,
    direction: Literal["forward", "backward"],
) -> int:
    """Move a cut point out of an indivisible tool transaction."""

    rows = list(messages)
    bounded = max(0, min(len(rows), int(index)))
    for start, end in tool_round_spans(rows):
        if start < bounded < end:
            return end if direction == "forward" else start
    return bounded


def close_active_tool_round(
    conversation: list[dict[str, Any]],
    assistant_index: int,
    *,
    post_round_user_messages: Iterable[str] = (),
    abort_reason: str | None = None,
) -> ToolRoundCloseReport:
    """Commit one active tool batch and only then append ordinary guidance.

    When an earlier call terminates the run, later calls in the same assistant
    batch receive an explicit local ``not dispatched`` result.  This records
    the real Harness outcome and keeps persisted history resumable without
    claiming that an external effect occurred.
    """

    if assistant_index < 0 or assistant_index >= len(conversation):
        raise ValueError("assistant_index is outside conversation")
    assistant = conversation[assistant_index]
    if not isinstance(assistant, dict) or assistant.get("role") != "assistant":
        raise ValueError("tool round must start at an assistant message")
    calls = assistant.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        raise ValueError("assistant message does not contain a tool-call batch")

    expected_ids: list[str] = []
    seen_expected: set[str] = set()
    for call in calls:
        call_id = _tool_call_id(call)
        if not call_id or call_id in seen_expected:
            raise ValueError("active tool-call batch has missing/duplicate ids")
        seen_expected.add(call_id)
        expected_ids.append(call_id)

    cursor = assistant_index + 1
    existing_ids: list[str] = []
    seen_results: set[str] = set()
    while cursor < len(conversation):
        message = conversation[cursor]
        if not isinstance(message, dict) or message.get("role") != "tool":
            break
        result_id = _tool_result_id(message)
        if (
            not result_id
            or result_id not in seen_expected
            or result_id in seen_results
        ):
            raise ValueError("active tool-call batch has orphan/duplicate results")
        seen_results.add(result_id)
        existing_ids.append(result_id)
        cursor += 1

    if cursor != len(conversation):
        raise ValueError(
            "ordinary messages were appended before active tool batch closure"
        )

    missing_ids = [
        call_id for call_id in expected_ids if call_id not in seen_results
    ]
    if missing_ids and abort_reason is None:
        raise ValueError("active tool-call batch is missing results")
    for call_id in missing_ids:
        conversation.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({
                "status": "error",
                "error_code": "tool_batch_aborted_before_dispatch",
                "error": (
                    "The Harness terminated this tool batch before this call "
                    "crossed the dispatch boundary."
                ),
                "request_sent": False,
                "actual_dispatch_attempted": False,
                "abort_reason": str(abort_reason),
            }, ensure_ascii=False, sort_keys=True),
        })

    guidance = [
        str(message) for message in post_round_user_messages
        if str(message).strip()
    ]
    conversation.extend(
        {"role": "user", "content": message} for message in guidance
    )
    return ToolRoundCloseReport(
        assistant_index=assistant_index,
        expected_tool_call_ids=tuple(expected_ids),
        existing_tool_result_ids=tuple(existing_ids),
        synthetic_not_dispatched_ids=tuple(missing_ids),
        guidance_count=len(guidance),
        abort_reason=abort_reason,
    )
