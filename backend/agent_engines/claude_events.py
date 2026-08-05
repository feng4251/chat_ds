"""Project Claude Code stream-json beside its lossless native envelope."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .base import EngineStreamEvent


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _child_run_id(root_run_id: str, native_task_id: object) -> str:
    return hashlib.sha256(
        f"chatds.claude.task.v1\0{root_run_id}\0{native_task_id}".encode()
    ).hexdigest()[:32]


@dataclass(slots=True)
class ClaudeEventProjector:
    """Stateful de-duplicating projection of one native Claude run."""

    root_run_id: str
    _streamed_text_messages: set[str] = field(default_factory=set)
    _streamed_reasoning_messages: set[str] = field(default_factory=set)
    _emitted_text: bool = False
    _terminal_seen: bool = False
    _result_seen: bool = False
    _result_failed: bool = False
    _result_finish_reason: str = "stop"
    _result_error: str | None = None
    _started_tasks: dict[str, str] = field(default_factory=dict)
    _task_by_tool_use_id: dict[str, str] = field(default_factory=dict)
    _started_tools: set[str] = field(default_factory=set)
    _terminal_tools: set[str] = field(default_factory=set)

    def project(self, envelope: Mapping[str, Any]) -> tuple[EngineStreamEvent, ...]:
        native = _mapping(envelope.get("event"))
        if not native:
            return (EngineStreamEvent(
                kind="diagnostic",
                data={
                    "code": "claude_non_json_output",
                    "channel": str(envelope.get("channel") or "unknown"),
                    "text": str(envelope.get("text") or "")[:4000],
                },
                raw=envelope,
            ),)
        event_type = str(native.get("type") or "")
        if event_type == "stream_event":
            return tuple(self._project_stream_event(native, envelope))
        if event_type == "assistant":
            return tuple(self._project_assistant(native, envelope))
        if event_type == "user":
            return tuple(self._project_user(native, envelope))
        if event_type == "system":
            return tuple(self._project_system(native, envelope))
        if event_type == "tool_progress":
            return (EngineStreamEvent(
                "tool_progress", {"text": _tool_progress_text(native)}, envelope
            ),)
        if event_type == "tool_use_summary":
            return (EngineStreamEvent(
                "tool_progress",
                {"text": str(native.get("summary") or "Tool completed")},
                envelope,
            ),)
        if event_type == "result":
            return tuple(self._project_result(native, envelope))
        if event_type == "chatds.supervisor.terminal":
            return tuple(self._project_supervisor_terminal(native, envelope))
        return (EngineStreamEvent(
            "diagnostic",
            {"code": "claude_native_event", "native_type": event_type},
            envelope,
        ),)

    def _project_stream_event(
        self,
        native: Mapping[str, Any],
        envelope: Mapping[str, Any],
    ) -> Iterable[EngineStreamEvent]:
        event = _mapping(native.get("event"))
        nested_type = str(event.get("type") or "")
        delta = _mapping(event.get("delta"))
        delta_type = str(delta.get("type") or "")
        message_identity = str(native.get("uuid") or "")
        run_id = self._native_run_id(native)
        if nested_type == "content_block_delta" and delta_type == "text_delta":
            text = str(delta.get("text") or "")
            if text:
                self._streamed_text_messages.add(message_identity)
                self._emitted_text = True
                return (EngineStreamEvent("content", {"text": text}, envelope),)
        if nested_type == "content_block_delta" and delta_type in {
            "thinking_delta", "signature_delta",
        }:
            text = str(delta.get("thinking") or delta.get("text") or "")
            if text:
                self._streamed_reasoning_messages.add(message_identity)
                return (EngineStreamEvent("reasoning", {"text": text}, envelope),)
        if nested_type == "content_block_start":
            block = _mapping(event.get("content_block"))
            if block.get("type") in {"tool_use", "server_tool_use"}:
                name = str(block.get("name") or "tool")
                tool_id = str(block.get("id") or "") or None
                if tool_id and tool_id in self._started_tools:
                    return ()
                if tool_id:
                    self._started_tools.add(tool_id)
                return (
                    EngineStreamEvent("tool_progress", {"text": f"🔧 {name} started"}, envelope),
                    EngineStreamEvent(
                        "agent_event",
                        {
                            "event_type": "tool.started",
                            "run_id": run_id,
                            "root_run_id": self.root_run_id,
                            "tool_name": name,
                            "tool_call_id": tool_id,
                        },
                        envelope,
                        native_event_id=tool_id,
                    ),
                )
        if nested_type == "message_start":
            message = _mapping(event.get("message"))
            values: list[EngineStreamEvent] = []
            usage = _usage(_mapping(message.get("usage")))
            if usage:
                values.append(EngineStreamEvent("usage", usage, envelope))
            if message.get("model"):
                values.append(EngineStreamEvent(
                    "model", {"resolved_model_id": str(message["model"])}, envelope
                ))
            return tuple(values)
        if nested_type == "message_delta":
            values = []
            usage = _usage(_mapping(event.get("usage")))
            if usage:
                values.append(EngineStreamEvent("usage", usage, envelope))
            return tuple(values)
        return ()

    def _project_assistant(
        self,
        native: Mapping[str, Any],
        envelope: Mapping[str, Any],
    ) -> Iterable[EngineStreamEvent]:
        message = _mapping(native.get("message"))
        message_identity = str(native.get("uuid") or "")
        run_id = self._native_run_id(native)
        values: list[EngineStreamEvent] = []
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        for block in message.get("content") or ():
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type") or "")
            if (
                block_type == "text"
                and message_identity not in self._streamed_text_messages
            ):
                text = str(block.get("text") or "")
                if text:
                    text_parts.append(text)
            elif (
                block_type == "thinking"
                and message_identity not in self._streamed_reasoning_messages
            ):
                text = str(block.get("thinking") or "")
                if text:
                    reasoning_parts.append(text)
            elif block_type in {"tool_use", "server_tool_use"}:
                name = str(block.get("name") or "tool")
                tool_id = str(block.get("id") or "") or None
                if tool_id and tool_id in self._started_tools:
                    continue
                if tool_id:
                    self._started_tools.add(tool_id)
                values.append(EngineStreamEvent(
                    "agent_event",
                    {
                        "event_type": "tool.started",
                        "run_id": run_id,
                        "root_run_id": self.root_run_id,
                        "tool_name": name,
                        "tool_call_id": tool_id,
                    },
                    envelope,
                    native_event_id=tool_id,
                ))
        if text_parts:
            values.insert(0, EngineStreamEvent(
                "content", {"text": "".join(text_parts)}, envelope
            ))
            self._emitted_text = True
        if reasoning_parts:
            values.insert(0, EngineStreamEvent(
                "reasoning", {"text": "".join(reasoning_parts)}, envelope
            ))
        usage = _usage(_mapping(message.get("usage")))
        if usage:
            values.append(EngineStreamEvent("usage", usage, envelope))
        if message.get("model"):
            values.append(EngineStreamEvent(
                "model", {"resolved_model_id": str(message["model"])}, envelope
            ))
        return tuple(values)

    def _project_user(
        self,
        native: Mapping[str, Any],
        envelope: Mapping[str, Any],
    ) -> Iterable[EngineStreamEvent]:
        message = _mapping(native.get("message"))
        run_id = self._native_run_id(native)
        values: list[EngineStreamEvent] = []
        for block in message.get("content") or ():
            if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                continue
            failed = bool(block.get("is_error"))
            tool_id = str(block.get("tool_use_id") or "") or None
            if tool_id and tool_id in self._terminal_tools:
                continue
            if tool_id:
                self._terminal_tools.add(tool_id)
            values.append(EngineStreamEvent(
                "agent_event",
                {
                    "event_type": "tool.failed" if failed else "tool.completed",
                    "run_id": run_id,
                    "root_run_id": self.root_run_id,
                    "tool_call_id": tool_id,
                    "error": "Claude tool returned an error" if failed else None,
                },
                envelope,
                native_event_id=tool_id,
            ))
        return tuple(values)

    def _project_system(
        self,
        native: Mapping[str, Any],
        envelope: Mapping[str, Any],
    ) -> Iterable[EngineStreamEvent]:
        subtype = str(native.get("subtype") or "")
        if subtype == "init":
            values = [EngineStreamEvent(
                "agent_event",
                {
                    "event_type": "run.started",
                    "run_id": self.root_run_id,
                    "root_run_id": self.root_run_id,
                    "agent_kind": "primary",
                    "agent_name": "Claude Code",
                    "depth": 0,
                    "workspace_scope": "shared_session",
                    "payload": {
                        "model_id": str(native.get("model") or ""),
                        "enabled_tools": [
                            str(value) for value in native.get("tools") or ()
                        ],
                        "skills": [
                            str(value) for value in native.get("skills") or ()
                        ],
                        "mcp_servers": [
                            dict(value)
                            for value in native.get("mcp_servers") or ()
                            if isinstance(value, Mapping)
                        ],
                        "claude_code_version": str(
                            native.get("claude_code_version") or ""
                        ),
                    },
                },
                envelope,
            )]
            if native.get("model"):
                values.append(EngineStreamEvent(
                    "model", {"resolved_model_id": str(native["model"])}, envelope
                ))
            return tuple(values)
        if subtype == "task_started":
            task_id = str(native.get("task_id") or native.get("id") or "")
            child = _child_run_id(self.root_run_id, task_id)
            self._started_tasks[task_id] = child
            tool_use_id = str(native.get("tool_use_id") or "")
            if tool_use_id:
                self._task_by_tool_use_id[tool_use_id] = child
            description = str(
                native.get("description") or native.get("prompt") or "Claude sub-agent"
            )[:256]
            identity = {
                "run_id": child,
                "root_run_id": self.root_run_id,
                "parent_run_id": self.root_run_id,
                "agent_kind": "delegate",
                "agent_name": description,
                "depth": 1,
                "workspace_scope": "shared_session",
                "native_task_id": task_id,
            }
            return (
                EngineStreamEvent(
                    "agent_event",
                    {
                        **identity,
                        "event_type": "agent.spawned",
                        "payload": {
                            "goal": description,
                            "native_task_id": task_id,
                            "tool_call_id": tool_use_id or None,
                        },
                    },
                    envelope,
                    native_event_id=task_id or None,
                ),
                EngineStreamEvent(
                    "agent_event",
                    {**identity, "event_type": "run.started"},
                    envelope,
                    native_event_id=task_id or None,
                ),
            )
        if subtype in {"task_notification", "task_completed", "task_failed"}:
            task_id = str(native.get("task_id") or native.get("id") or "")
            child = self._started_tasks.get(task_id) or _child_run_id(self.root_run_id, task_id)
            task_status = str(native.get("status") or "")
            failed = subtype == "task_failed" or task_status == "failed"
            stopped = task_status == "stopped"
            event_type = (
                "run.cancelled"
                if stopped
                else "run.failed"
                if failed
                else "run.completed"
            )
            summary = str(native.get("summary") or native.get("error") or "")
            return (EngineStreamEvent(
                "agent_event",
                {
                    "event_type": event_type,
                    "run_id": child,
                    "root_run_id": self.root_run_id,
                    "parent_run_id": self.root_run_id,
                    "finish_reason": (
                        "cancelled" if stopped else "error" if failed else "stop"
                    ),
                    "error": (summary or None) if failed else None,
                    "native_task_id": task_id,
                    "payload": {
                        "summary": summary,
                        "output_file": str(native.get("output_file") or ""),
                        "usage": dict(native.get("usage") or {})
                        if isinstance(native.get("usage"), Mapping)
                        else {},
                    },
                },
                envelope,
                native_event_id=task_id or None,
            ),)
        if subtype in {"task_progress", "status", "api_retry"}:
            return (EngineStreamEvent(
                "tool_progress", {"text": _system_progress_text(native, subtype)}, envelope
            ),)
        return (EngineStreamEvent(
            "diagnostic", {"code": "claude_system_event", "subtype": subtype}, envelope
        ),)

    def _project_result(
        self,
        native: Mapping[str, Any],
        envelope: Mapping[str, Any],
    ) -> Iterable[EngineStreamEvent]:
        values: list[EngineStreamEvent] = []
        result = str(native.get("result") or "")
        if result and not self._emitted_text:
            values.append(EngineStreamEvent("content", {"text": result}, envelope))
            self._emitted_text = True
        usage = _usage(_mapping(native.get("usage")))
        if usage:
            values.append(EngineStreamEvent("usage", usage, envelope))
        failed = bool(native.get("is_error")) or str(native.get("subtype") or "") not in {
            "", "success",
        }
        self._result_seen = True
        self._result_failed = failed
        self._result_finish_reason = (
            "error" if failed else str(native.get("stop_reason") or "stop")
        )
        errors = native.get("errors")
        error_text = "; ".join(
            str(value) for value in errors if value is not None
        ) if isinstance(errors, (list, tuple)) else ""
        self._result_error = (
            str(
                native.get("error")
                or error_text
                or result
                or native.get("subtype")
                or "error"
            )
            if failed
            else None
        )
        if failed:
            values.append(EngineStreamEvent(
                "diagnostic",
                {
                    "code": "claude_result_error",
                    "message": self._result_error or "Claude Code returned an error",
                },
                envelope,
            ))
        # ``result`` is a native candidate, not the authoritative process
        # terminal.  The controller still has to seal the egress bridge,
        # verify cleanup and observe the CLI exit.  Publishing a root terminal
        # here could turn a later containment/audit failure into false success.
        return tuple(values)

    def _native_run_id(self, native: Mapping[str, Any]) -> str:
        parent_tool_use_id = str(native.get("parent_tool_use_id") or "")
        return self._task_by_tool_use_id.get(
            parent_tool_use_id, self.root_run_id
        )

    def _project_supervisor_terminal(
        self,
        native: Mapping[str, Any],
        envelope: Mapping[str, Any],
    ) -> Iterable[EngineStreamEvent]:
        if self._terminal_seen:
            return ()
        status = str(native.get("status") or "failed")
        if status == "succeeded" and self._result_failed:
            status = "failed"
        if status == "succeeded" and not self._result_seen:
            status = "failed"
            self._result_error = "Claude runner succeeded without a native result"
        if status not in {"succeeded", "cancelled"}:
            message = str(
                native.get("error")
                or self._result_error
                or f"Claude runner exited with status {status}"
            )
            self._terminal_seen = True
            return (
                EngineStreamEvent(
                    "diagnostic", {"code": "claude_runner_failed", "message": message}, envelope
                ),
                EngineStreamEvent(
                    "agent_event",
                    {
                        "event_type": "run.failed",
                        "run_id": self.root_run_id,
                        "root_run_id": self.root_run_id,
                        "finish_reason": "error",
                        "error": message,
                    },
                    envelope,
                ),
                EngineStreamEvent("finish", {"finish_reason": "error"}, envelope),
            )
        finish_reason = (
            "cancelled" if status == "cancelled" else self._result_finish_reason
        )
        self._terminal_seen = True
        return (
            EngineStreamEvent(
                "agent_event",
                {
                    "event_type": "run.cancelled" if status == "cancelled" else "run.completed",
                    "run_id": self.root_run_id,
                    "root_run_id": self.root_run_id,
                    "finish_reason": finish_reason,
                },
                envelope,
            ),
            EngineStreamEvent(
                "finish",
                {"finish_reason": finish_reason},
                envelope,
            ),
        )


def _usage(value: Mapping[str, Any]) -> dict[str, int]:
    input_tokens = _bounded_usage_int(value.get("input_tokens"))
    output_tokens = _bounded_usage_int(value.get("output_tokens"))
    cache_creation = _bounded_usage_int(value.get("cache_creation_input_tokens"))
    cache_read = _bounded_usage_int(value.get("cache_read_input_tokens"))
    if not any((input_tokens, output_tokens, cache_creation, cache_read)):
        return {}
    return {
        "input_tokens": input_tokens + cache_creation + cache_read,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens + cache_creation + cache_read,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }


def _bounded_usage_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if 0 <= parsed <= 10**12 else 0


def _tool_progress_text(native: Mapping[str, Any]) -> str:
    name = str(native.get("tool_name") or native.get("name") or "tool")
    elapsed = native.get("elapsed_time_seconds")
    suffix = f" ({elapsed}s)" if isinstance(elapsed, (int, float)) else ""
    return f"🔧 {name} running{suffix}"


def _system_progress_text(native: Mapping[str, Any], subtype: str) -> str:
    if subtype == "api_retry":
        attempt = native.get("attempt") or native.get("retry_attempt")
        return f"↻ Claude provider retry{f' #{attempt}' if attempt else ''}"
    return str(native.get("description") or native.get("status") or subtype)[:1000]
