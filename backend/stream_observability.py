"""Safe attribution and durable diagnostics for Backend SSE streams.

Only bounded counts, enum-like labels and timestamps are retained. Model text,
tool arguments, URLs, request headers and credentials are deliberately absent.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
from datetime import datetime, timezone

from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from workspace import ensure_workspace


logger = logging.getLogger(__name__)

_service_shutdown_started = False
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


def set_service_shutdown_started(value: bool) -> None:
    """Expose the locally observed Backend lifespan state to active streams."""

    global _service_shutdown_started
    _service_shutdown_started = bool(value)


def _observed_at() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _safe_diagnostic_label(value: object) -> str | None:
    """Return a bounded label, hashing arbitrary/untrusted text."""

    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if _SAFE_LABEL.fullmatch(value):
        return value
    return "sha256:" + hashlib.sha256(
        value.encode("utf-8", "replace")
    ).hexdigest()[:24]


def _root_phase_after_event(current: str, event_type: str) -> str:
    if event_type in {"agent.spawned", "run.planned"}:
        return "planned"
    if event_type == "run.started":
        return "executing"
    if event_type in {"verifier.requested", "verifier.failed"}:
        return "verifying"
    if event_type == "verifier.followup_requested":
        return "executing"
    if event_type in {
        "verifier.completed",
        "run.committing",
        "run.commit_requested",
    }:
        return "committing"
    if event_type in {"run.completed", "run.failed", "run.cancelled"}:
        return "terminal"
    return current


class _StreamObservation:
    """Safe counters and connection facts for one Backend SSE bridge."""

    def __init__(
        self,
        *,
        run_id: str,
        ingress_request_id: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.ingress_request_id = (
            _safe_diagnostic_label(ingress_request_id)
            or "not_provided"
        )
        self.started_at = _observed_at()
        self.upstream_state = "not_opened"
        self.downstream_state = "connected"
        self.http_disconnect_observed = False
        self.downstream_signal_at: str | None = None
        self.downstream_exception_class: str | None = None
        self.upstream_sse_lines = 0
        self.upstream_data_chunks = 0
        self.upstream_bytes = 0
        self.upstream_parse_errors = 0
        self.produced_chunks = 0
        self.produced_bytes = 0
        self.relayed_chunks = 0
        self.relayed_bytes = 0
        self.downstream_chunks = 0
        self.downstream_bytes = 0
        self.last_event_seq: int | None = None
        self.last_event_type: str | None = None
        self.last_event_at: str | None = None
        self.last_root_event_seq: int | None = None
        self.last_root_event_type: str | None = None
        self.last_root_event_at: str | None = None
        self.root_phase = "not_started"
        self.terminal_envelope_planned = False
        self._bound_payloads: list[dict] = []

    def observe_upstream_line(self, line: str) -> None:
        self.upstream_sse_lines += 1
        self.upstream_bytes += len(line.encode("utf-8", "replace"))

    def observe_upstream_data(self) -> None:
        self.upstream_data_chunks += 1

    def observe_parse_error(self) -> None:
        self.upstream_parse_errors += 1

    @staticmethod
    def _chunk_size(chunk: str | bytes | bytearray | memoryview) -> int:
        if isinstance(chunk, str):
            return len(chunk.encode("utf-8", "replace"))
        return len(bytes(chunk))

    def observe_produced_chunk(self, chunk: str) -> str:
        self.produced_chunks += 1
        self.produced_bytes += self._chunk_size(chunk)
        return chunk

    def observe_relayed_chunk(
        self,
        chunk: str | bytes | bytearray | memoryview,
    ) -> None:
        self.relayed_chunks += 1
        self.relayed_bytes += self._chunk_size(chunk)

    def observe_downstream_chunk(
        self,
        chunk: str | bytes | bytearray | memoryview,
    ) -> None:
        self.downstream_chunks += 1
        self.downstream_bytes += self._chunk_size(chunk)

    def observe_agent_event(self, event: dict) -> None:
        observed_at = _observed_at()
        event_type = _safe_diagnostic_label(
            event.get("event_type")
        ) or "unknown"
        try:
            seq = max(0, int(event.get("seq") or 0))
        except (TypeError, ValueError, OverflowError):
            seq = 0
        self.last_event_seq = seq
        self.last_event_type = event_type
        self.last_event_at = observed_at
        if str(event.get("run_id") or "") != self.run_id:
            return
        self.last_root_event_seq = seq
        self.last_root_event_type = event_type
        self.last_root_event_at = observed_at
        self.root_phase = _root_phase_after_event(
            self.root_phase,
            event_type,
        )

    def bind_termination_payload(self, payload: dict) -> None:
        self._bound_payloads.append(payload)

    def mark_downstream(
        self,
        state: str,
        *,
        exception_class: str | None = None,
    ) -> None:
        # An ASGI disconnect is stronger evidence than the send OSError that
        # often immediately precedes it.
        priority = {
            "connected": 0,
            "generator_closed": 1,
            "asyncio_cancelled_unknown": 1,
            "subscriber_backpressure_detached": 2,
            "downstream_send_failed": 2,
            "client_disconnected": 3,
            "service_shutdown": 4,
        }
        if priority.get(state, 0) < priority.get(
            self.downstream_state,
            0,
        ):
            return
        self.downstream_state = state
        self.downstream_signal_at = _observed_at()
        if exception_class:
            self.downstream_exception_class = _safe_diagnostic_label(
                exception_class
            )
        for payload in self._bound_payloads:
            connection = payload.get("connection_state")
            if isinstance(connection, dict):
                connection.update({
                    "downstream": self.downstream_state,
                    "http_disconnect_observed": (
                        self.http_disconnect_observed
                    ),
                    "downstream_signal_at": self.downstream_signal_at,
                    "downstream_exception_class": (
                        self.downstream_exception_class
                    ),
                })

    def snapshot(self) -> dict:
        return {
            "stream_started_at": self.started_at,
            "correlation": {
                "backend_run_id": self.run_id,
                "ingress_request_id": self.ingress_request_id,
            },
            "last_event_seq": self.last_event_seq,
            "last_event_type": self.last_event_type,
            "last_event_at": self.last_event_at,
            "last_root_event_seq": self.last_root_event_seq,
            "last_root_event_type": self.last_root_event_type,
            "last_root_event_at": self.last_root_event_at,
            "root_phase": self.root_phase,
            "stream_counts": {
                "upstream_sse_lines": self.upstream_sse_lines,
                "upstream_data_chunks": self.upstream_data_chunks,
                "upstream_bytes": self.upstream_bytes,
                "upstream_parse_errors": self.upstream_parse_errors,
                "produced_chunks": self.produced_chunks,
                "produced_bytes": self.produced_bytes,
                "relayed_chunks": self.relayed_chunks,
                "relayed_bytes": self.relayed_bytes,
                "downstream_chunks": self.downstream_chunks,
                "downstream_bytes": self.downstream_bytes,
            },
            "connection_state": {
                "upstream": self.upstream_state,
                "downstream": self.downstream_state,
                "http_disconnect_observed": (
                    self.http_disconnect_observed
                ),
                "downstream_signal_at": self.downstream_signal_at,
                "downstream_exception_class": (
                    self.downstream_exception_class
                ),
                # Planned means Backend prepared a terminal SSE envelope. The
                # field never claims a browser or reverse proxy received it.
                "terminal_envelope_planned": (
                    self.terminal_envelope_planned
                ),
            },
        }

    def observe_http_disconnect(
        self,
        *,
        exception_class: str | None = None,
    ) -> None:
        """Record the only positive evidence for a client disconnect."""

        self.http_disconnect_observed = True
        self.mark_downstream(
            "client_disconnected",
            exception_class=exception_class,
        )

    def observe_client_disconnect_exception(
        self,
        *,
        exception_class: str,
    ) -> None:
        """Classify Starlette's wrapper without inventing client evidence.

        ASGI spec 2.4 permits a downstream send failure to surface as
        ``ClientDisconnect``.  Only a separately observed ``http.disconnect``
        message proves the receive-side client-disconnect boundary.
        """

        if self.http_disconnect_observed:
            self.mark_downstream(
                "client_disconnected",
                exception_class=exception_class,
            )
            return
        self.mark_downstream(
            "downstream_send_failed",
            exception_class=exception_class,
        )


class _ObservedStreamingResponse(StreamingResponse):
    """StreamingResponse that retains positive ASGI disconnect evidence."""

    def __init__(
        self,
        *args,
        observation: _StreamObservation,
        on_downstream_final=None,
        **kwargs,
    ) -> None:
        self._stream_observation = observation
        self._on_downstream_final = on_downstream_final
        super().__init__(*args, **kwargs)

    async def listen_for_disconnect(self, receive) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self._stream_observation.observe_http_disconnect()
                break

    async def stream_response(self, send) -> None:
        original_iterator = self.body_iterator

        async def observed_iterator():
            async for chunk in original_iterator:
                self._stream_observation.observe_relayed_chunk(chunk)
                yield chunk

        async def observed_send(message) -> None:
            await send(message)
            if (
                message.get("type") == "http.response.body"
                and message.get("body")
            ):
                self._stream_observation.observe_downstream_chunk(
                    message["body"]
                )

        self.body_iterator = observed_iterator()
        try:
            await super().stream_response(observed_send)
        except OSError as exc:
            self._stream_observation.mark_downstream(
                "downstream_send_failed",
                exception_class=type(exc).__name__,
            )
            raise

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except ClientDisconnect as exc:
            self._stream_observation.observe_client_disconnect_exception(
                exception_class=type(exc).__name__,
            )
            close = getattr(self.body_iterator, "aclose", None)
            if callable(close):
                try:
                    await close()
                except (
                    RuntimeError,
                    asyncio.CancelledError,
                    GeneratorExit,
                ):
                    pass
            raise
        finally:
            if self._stream_observation.downstream_state == "connected":
                self._stream_observation.mark_downstream(
                    "response_completed"
                )
            callback = self._on_downstream_final
            if callable(callback):
                try:
                    result = callback(self._stream_observation)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception(
                        "Backend downstream-final observation callback failed "
                        "run=%s",
                        self._stream_observation.run_id,
                    )


def _event_payload(event: dict) -> dict:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _event_is_authoritative(event: dict) -> bool:
    top_level = event.get("authoritative")
    if isinstance(top_level, bool):
        return top_level
    payload = _event_payload(event)
    nested = payload.get("authoritative")
    if isinstance(nested, bool):
        return nested
    return payload.get("provisional_terminal") is not True


def _root_terminal_event(
    events: list[dict],
    *,
    run_id: str,
) -> dict | None:
    for event in sorted(
        events or [],
        key=lambda item: int(item.get("seq") or 0),
    ):
        if str(event.get("run_id") or "") != run_id:
            continue
        if (
            event.get("event_type")
            in {"run.completed", "run.failed", "run.cancelled"}
            and _event_is_authoritative(event)
        ):
            return event
    return None


def _safe_count(value: object) -> int | None:
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def _unsatisfied_contract_summary(
    events: list[dict],
    *,
    run_id: str,
) -> dict:
    """Extract receipt/contract counts without copying receipt bodies."""

    quality: dict | None = None
    observations: dict[str, object] = {}
    latest_type: str | None = None
    for event in sorted(
        events or [],
        key=lambda item: int(item.get("seq") or 0),
    ):
        if str(event.get("run_id") or "") != run_id:
            continue
        payload = _event_payload(event)
        event_type = _safe_diagnostic_label(event.get("event_type"))
        for key in (
            "unresolved_call_count",
            "required_logical_call_count",
            "completion_blocker_count",
            "required_failed_count",
            "pending_dispatch_count",
        ):
            count = _safe_count(payload.get(key))
            if count is not None:
                observations[key] = count
                latest_type = event_type
        for source, target in (
            ("missing_tool_requirements", "missing_tool_requirement_count"),
            ("required_tool_groups", "required_tool_group_count"),
        ):
            value = payload.get(source)
            if isinstance(value, (list, tuple, dict)):
                observations[target] = len(value)
                latest_type = event_type
        run_contract = payload.get("run_contract")
        if isinstance(run_contract, dict):
            candidate = run_contract.get("quality_ledger")
            if isinstance(candidate, dict):
                quality = candidate
        candidate = payload.get("completion_contract")
        if isinstance(candidate, dict):
            quality = candidate

    if quality is None:
        if not observations:
            return {
                "reported": False,
                "status": "not_reported_by_harness",
            }
        unsatisfied = any(
            int(observations.get(key, 0) or 0) > 0
            for key in (
                "unresolved_call_count",
                "completion_blocker_count",
                "required_failed_count",
                "pending_dispatch_count",
                "missing_tool_requirement_count",
            )
        )
        return {
            "reported": True,
            "status": (
                "unsatisfied" if unsatisfied else "partial_observation"
            ),
            "sealed_contract_snapshot": False,
            "latest_observation_type": latest_type,
            **observations,
        }

    summary: dict[str, object] = {
        "reported": True,
        "status": "observed",
    }
    for key in (
        "entry_count_active",
        "active_nonverified_count",
        "required_failed_count",
        "pending_dispatch_count",
        "completion_blocker_count",
    ):
        count = _safe_count(quality.get(key))
        if count is not None:
            summary[key] = count
    for key in (
        "completion_allowed",
        "sealed",
        "active_nonverified_omitted",
        "required_failed_omitted",
        "pending_dispatch_omitted",
        "completion_blocker_omitted",
    ):
        if isinstance(quality.get(key), bool):
            summary[key] = quality[key]
    quality_label = _safe_diagnostic_label(quality.get("quality"))
    if quality_label:
        summary["quality"] = quality_label
    blockers = _safe_count(summary.get("completion_blocker_count"))
    nonverified = _safe_count(summary.get("active_nonverified_count"))
    if blockers and blockers > 0:
        summary["status"] = "unsatisfied"
    elif summary.get("completion_allowed") is True and nonverified == 0:
        summary["status"] = "satisfied"
    return summary


def _provider_failure_summary(
    events: list[dict],
    *,
    run_id: str,
) -> dict:
    """Attribute provider failure only from typed Harness terminal evidence."""

    terminal = _root_terminal_event(events, run_id=run_id)
    if terminal is None or terminal.get("event_type") != "run.failed":
        return {"reported": False, "status": "not_observed"}
    payload = _event_payload(terminal)
    finish_reason = _safe_diagnostic_label(payload.get("finish_reason"))
    failure_class = _safe_diagnostic_label(payload.get("failure_class"))
    terminal_reason = _safe_diagnostic_label(payload.get("terminal_reason"))
    reported = bool(
        (finish_reason and finish_reason.startswith("provider_"))
        or (terminal_reason and terminal_reason.startswith("provider_"))
        or failure_class in {
            "provider_protocol",
            "provider_transport",
            "provider_timeout",
        }
    )
    summary: dict[str, object] = {
        "reported": reported,
        "status": (
            "failure_reported_by_harness"
            if reported
            else "not_attributed_by_harness"
        ),
    }
    for key, value in (
        ("finish_reason", finish_reason),
        ("failure_class", failure_class),
        ("terminal_reason", terminal_reason),
    ):
        if value:
            summary[key] = value
    return summary


def _append_backend_stream_termination_event(
    events: list[dict],
    *,
    run_id: str,
    observation: _StreamObservation,
    termination_source: str,
    exception_class: str | None,
    root_terminal_status: str | None,
) -> dict:
    """Append one safe Backend-observed termination boundary."""

    root_seq = max(
        (
            int(event.get("seq") or 0)
            for event in events
            if str(event.get("run_id") or "") == run_id
        ),
        default=0,
    )
    payload = {
        "schema": "chatds.backend-stream-termination.v1",
        "termination_source": _safe_diagnostic_label(
            termination_source
        ) or "unknown",
        "exception_class": _safe_diagnostic_label(exception_class),
        "root_terminal_status": root_terminal_status or "missing",
        "provider_failure": _provider_failure_summary(
            events,
            run_id=run_id,
        ),
        "unsatisfied_contract": _unsatisfied_contract_summary(
            events,
            run_id=run_id,
        ),
        **observation.snapshot(),
    }
    event = {
        "type": "agent_event",
        "event_type": "debug.backend_stream.terminated",
        "run_id": run_id,
        "root_run_id": run_id,
        "parent_run_id": None,
        "agent_kind": "primary",
        "agent_name": "primary",
        "depth": 0,
        "workspace_scope": "shared_session",
        "seq": root_seq + 1,
        "payload": payload,
        "authoritative": False,
    }
    events.append(event)
    observation.bind_termination_payload(payload)
    return event


def _append_backend_stream_debug_file(
    *,
    user_id: str,
    session_id: str,
    run_id: str,
    event: dict,
) -> None:
    """Best-effort JSONL mirror beside, never inside, Harness logs."""

    try:
        safe_run_id = (
            run_id
            if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id)
            else hashlib.sha256(
                run_id.encode("utf-8", "replace")
            ).hexdigest()
        )
        debug_dir = (
            ensure_workspace(user_id, session_id)
            / "debug"
            / "backend_streams"
        )
        debug_dir.mkdir(parents=True, exist_ok=True)
        line = (
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        fd = os.open(
            debug_dir / f"{safe_run_id}.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        logger.exception(
            "Backend stream debug mirror failed conv=%s run=%s",
            session_id,
            run_id,
        )


def _local_abort_source(
    exc: BaseException,
    observation: _StreamObservation,
) -> str:
    if _service_shutdown_started:
        observation.mark_downstream(
            "service_shutdown",
            exception_class=type(exc).__name__,
        )
        return "service_shutdown"
    if observation.downstream_state == "client_disconnected":
        return "client_disconnected"
    if observation.downstream_state == "downstream_send_failed":
        return "downstream_send_failed"
    if isinstance(exc, GeneratorExit):
        observation.mark_downstream(
            "generator_closed",
            exception_class=type(exc).__name__,
        )
        return "generator_closed"
    observation.mark_downstream(
        "asyncio_cancelled_unknown",
        exception_class=type(exc).__name__,
    )
    return "asyncio_cancelled_unknown"


def _cancellation_interruption_message(source: str) -> str:
    if source == "client_disconnected":
        return "服务端在根任务完成前观测到客户端连接断开。"
    if source == "downstream_send_failed":
        return "服务端在根任务完成前向下游发送流数据失败。"
    if source == "generator_closed":
        return "响应生成器在根任务完成前被关闭。"
    if source == "service_shutdown":
        return "Backend 服务关闭过程在根任务完成前取消了响应。"
    return (
        "响应任务在根任务完成前被取消；服务端未观测到足以证明"
        "客户端断连或服务关闭的信号。"
    )


def _cancellation_diagnostic_fields(payload: dict) -> dict:
    """Copy only the approved safe summary fields into run.cancelled."""

    keys = (
        "last_event_seq",
        "last_event_type",
        "last_event_at",
        "last_root_event_seq",
        "last_root_event_type",
        "last_root_event_at",
        "root_phase",
        "stream_counts",
        "connection_state",
        "unsatisfied_contract",
        "provider_failure",
        "correlation",
    )
    return {key: payload.get(key) for key in keys}


__all__ = [
    "_ObservedStreamingResponse",
    "_StreamObservation",
    "_append_backend_stream_debug_file",
    "_append_backend_stream_termination_event",
    "_cancellation_diagnostic_fields",
    "_cancellation_interruption_message",
    "_local_abort_source",
    "_provider_failure_summary",
    "_safe_diagnostic_label",
    "_unsatisfied_contract_summary",
    "set_service_shutdown_started",
]
