"""Backend adapter for the isolated DeepSeek Harness Runner Supervisor.

DeepSeek Harness remains an independent upstream runtime.  This module only
translates its durable Session events into ChatDS's stable Engine contract;
it does not patch the upstream agent loop or infer control-plane state from
assistant prose.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import httpx

from .base import (
    ENGINE_ID_DEEPSEEK_HARNESS,
    AgentEngineError,
    AgentEngineRequest,
    EngineDescriptor,
    EngineStreamEvent,
)


def _text_blocks(message: object) -> str:
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    )


def deepseek_tool_result_call_id(message: object) -> str:
    """Extract DSH's stable call identity across native result shapes."""

    if not isinstance(message, Mapping):
        return ""
    direct = message.get("toolCallId") or message.get("tool_call_id")
    if direct:
        return str(direct)
    source = message.get("source")
    if isinstance(source, Mapping):
        nested = source.get("callId") or source.get("call_id")
        if nested:
            return str(nested)
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping):
                continue
            nested = block.get("toolCallId") or block.get("tool_call_id")
            if nested:
                return str(nested)
    return ""


def deepseek_native_tool_identity(
    envelope: Mapping[str, Any],
) -> tuple[str, str, str] | None:
    """Return ``(native_type, call_id, tool_name)`` from a raw DSH event.

    This pure decoder is shared by live projection and repair of derived UI
    rows.  The lossless native ledger remains authoritative and untouched.
    """

    event = envelope.get("event")
    if not isinstance(event, Mapping) or event.get("type") != "deepseek.session.event":
        return None
    native = event.get("session_event")
    if not isinstance(native, Mapping):
        return None
    native_type = str(native.get("type") or "")
    data = native.get("data")
    data = data if isinstance(data, Mapping) else {}
    if native_type == "tool/call":
        return (
            native_type,
            str(data.get("callId") or data.get("call_id") or ""),
            str(data.get("name") or ""),
        )
    if native_type == "tool/result":
        return (
            native_type,
            deepseek_tool_result_call_id(data.get("message")),
            "",
        )
    return None


class DeepSeekEventProjector:
    """Losslessly project native DSH Session events into ChatDS events."""

    def __init__(self, root_run_id: str) -> None:
        self.root_run_id = root_run_id
        self._root_session_id: str | None = None
        self._labels: dict[str, str] = {}
        self._started: set[str] = set()
        self._tool_names: dict[tuple[str, str], str] = {}

    def _run_id(self, session_id: str, depth: int) -> str:
        if depth <= 0:
            self._root_session_id = self._root_session_id or session_id
            return self.root_run_id
        return hashlib.sha256(
            b"chatds.deepseek-child.v1\0"
            + self.root_run_id.encode("ascii")
            + b"\0"
            + session_id.encode("utf-8")
        ).hexdigest()[:32]

    def _agent_event(
        self,
        *,
        event_type: str,
        run_id: str,
        seq: int,
        payload: Mapping[str, Any],
        depth: int,
        session_id: str,
    ) -> EngineStreamEvent:
        parent_run_id = None if depth <= 0 else self.root_run_id
        return EngineStreamEvent("agent_event", {
            "event_type": event_type,
            "run_id": run_id,
            "root_run_id": self.root_run_id,
            "parent_run_id": parent_run_id,
            "seq": seq,
            "agent_kind": "primary" if depth <= 0 else "worker",
            "agent_name": (
                "primary"
                if depth <= 0
                else self._labels.get(session_id) or f"DeepSeek worker {run_id[:8]}"
            ),
            "display_name": (
                "DeepSeek Harness"
                if depth <= 0
                else self._labels.get(session_id) or f"DeepSeek worker {run_id[:8]}"
            ),
            "depth": max(0, depth),
            "workspace_scope": "shared_session",
            "payload": dict(payload),
        })

    def project(self, envelope: Mapping[str, Any]) -> tuple[EngineStreamEvent, ...]:
        event = envelope.get("event")
        if not isinstance(event, Mapping):
            return ()
        seq = envelope.get("seq")
        if not isinstance(seq, int) or seq < 1:
            return ()
        event_type = str(event.get("type") or "")
        if event_type == "chatds.supervisor.terminal":
            status = str(event.get("status") or "failed")
            projected_type = {
                "succeeded": "run.completed",
                "cancelled": "run.cancelled",
            }.get(status, "run.failed")
            payload = {
                "authoritative": True,
                "finish_reason": (
                    "stop" if status == "succeeded" else status
                ),
                "terminal_reason": event.get("error") or status,
                "error": event.get("error"),
            }
            return (
                self._agent_event(
                    event_type=projected_type,
                    run_id=self.root_run_id,
                    seq=seq * 1_000_000,
                    payload=payload,
                    depth=0,
                    session_id=self._root_session_id or "root",
                ),
                EngineStreamEvent(
                    "finish",
                    {"finish_reason": payload["finish_reason"]},
                    raw=dict(envelope),
                ),
            )
        if event_type == "chatds.deepseek.artifact":
            return (self._agent_event(
                event_type="artifact.produced",
                run_id=self.root_run_id,
                seq=seq * 1_000_000,
                payload={
                    "path": str(event.get("path") or ""),
                    "title": str(event.get("path") or ""),
                    "size_bytes": int(event.get("size_bytes") or 0),
                    "sha256": str(event.get("sha256") or ""),
                },
                depth=0,
                session_id=self._root_session_id or "root",
            ),)
        if event_type != "deepseek.session.event":
            return ()
        native = event.get("session_event")
        if not isinstance(native, Mapping):
            return ()
        session_id = str(event.get("session_id") or "")
        depth_value = event.get("delegation_depth")
        depth = depth_value if isinstance(depth_value, int) and depth_value >= 0 else 0
        run_id = self._run_id(session_id, depth)
        native_type = str(native.get("type") or "")
        data = native.get("data")
        data = data if isinstance(data, Mapping) else {}
        projected: list[EngineStreamEvent] = []
        sub_seq = seq * 1_000_000

        if native_type == "subagent/descriptor":
            label = str(data.get("label") or "").strip()
            if label:
                self._labels[session_id] = label[:128]
        if native_type == "turn/start" and run_id not in self._started:
            self._started.add(run_id)
            projected.append(self._agent_event(
                event_type="run.started",
                run_id=run_id,
                seq=sub_seq,
                payload={"native_session_id": session_id},
                depth=depth,
                session_id=session_id,
            ))
        elif native_type == "assistant/chunk":
            chunk = data.get("chunk")
            if isinstance(chunk, Mapping):
                chunk_type = str(chunk.get("type") or "")
                text = str(chunk.get("text") or "")
                if text and chunk_type == "text-delta" and depth <= 0:
                    projected.append(EngineStreamEvent(
                        "content", {"text": text}, raw=dict(envelope)
                    ))
                elif text and chunk_type == "reasoning-delta" and depth <= 0:
                    projected.append(EngineStreamEvent(
                        "reasoning", {"text": text}, raw=dict(envelope)
                    ))
                elif text and depth > 0:
                    # A child Session is observable workflow state, not a
                    # second author of the root assistant message.  Preserve
                    # a bounded preview on the child run card without
                    # interleaving it into the user's final answer.
                    projected.append(self._agent_event(
                        event_type="run.progress",
                        run_id=run_id,
                        seq=sub_seq,
                        payload={
                            "stage": "assistant_reasoning"
                            if chunk_type == "reasoning-delta"
                            else "assistant_output",
                            "preview": text[-2_000:],
                        },
                        depth=depth,
                        session_id=session_id,
                    ))
        elif native_type == "assistant/message":
            usage = data.get("usage")
            if depth <= 0 and isinstance(usage, Mapping):
                input_tokens = int(usage.get("inputTokens") or usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("outputTokens") or usage.get("output_tokens") or 0)
                projected.append(EngineStreamEvent("usage", {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }, raw=dict(envelope)))
        elif native_type == "tool/call":
            tool_name = str(data.get("name") or "")
            tool_call_id = str(data.get("callId") or "")
            if not tool_name or not tool_call_id:
                projected.append(self._agent_event(
                    event_type="tool.failed",
                    run_id=run_id,
                    seq=sub_seq,
                    payload={
                        "tool_name": tool_name or None,
                        "tool_call_id": tool_call_id or None,
                        "detail": "DeepSeek Harness emitted an invalid tool call",
                        "error": "invalid_tool_call",
                    },
                    depth=depth,
                    session_id=session_id,
                ))
            else:
                self._tool_names[(run_id, tool_call_id)] = tool_name
                projected.append(self._agent_event(
                    event_type="tool.started",
                    run_id=run_id,
                    seq=sub_seq,
                    payload={
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "detail": "DeepSeek Harness tool call started",
                    },
                    depth=depth,
                    session_id=session_id,
                ))
        elif native_type == "tool/result":
            message = data.get("message")
            error = data.get("error")
            tool_call_id = deepseek_tool_result_call_id(message)
            tool_name = self._tool_names.get((run_id, tool_call_id), "")
            projected.append(self._agent_event(
                event_type="tool.failed" if isinstance(error, Mapping) else "tool.completed",
                run_id=run_id,
                seq=sub_seq,
                payload={
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name or None,
                    "detail": (
                        str(error.get("code") or error.get("name") or "tool_failed")
                        if isinstance(error, Mapping) else _text_blocks(message)[:2000]
                    ),
                    "error": (
                        str(error.get("code") or error.get("name") or "tool_failed")
                        if isinstance(error, Mapping) else None
                    ),
                },
                depth=depth,
                session_id=session_id,
            ))
        elif native_type == "approval/asked":
            # The native event is the immutable audit record. The control
            # bridge republishes the same id as `chatds/approval/requested`
            # only after it has installed an answerable Web I/O waiter. Do
            # not expose two pending cards for the same native decision.
            projected.append(EngineStreamEvent(
                "diagnostic",
                {"code": "deepseek_native_approval_audited"},
                raw=dict(envelope),
            ))
        elif native_type == "approval/decided":
            request_id = str(data.get("id") or "")
            outcome = str(data.get("outcome") or "")
            if request_id and depth <= 0:
                status = "allowed" if outcome == "allowed-once" else "denied"
                projected.append(EngineStreamEvent(
                    "approval",
                    {
                        "request_id": request_id,
                        "request_seq": sub_seq,
                        "status": status,
                    },
                    raw=dict(envelope)
                ))
        elif native_type == "chatds/approval/requested":
            request_id = str(data.get("request_id") or "")
            tool_name = str(data.get("tool_name") or "")
            reason = data.get("reason")
            if request_id and depth <= 0:
                projected.append(EngineStreamEvent(
                    "approval",
                    {
                        "request_id": request_id,
                        "request_seq": sub_seq,
                        "status": "pending",
                        "tool_name": tool_name,
                        "title": f"Approve {tool_name}",
                        "description": str(reason) if reason else f"The agent needs permission to use {tool_name}",
                    },
                    raw=dict(envelope)
                ))
        elif native_type == "chatds/question/requested":
            request_id = str(data.get("request_id") or "")
            intent_kind = data.get("intent_kind")
            intent_approve = data.get("intent_approve")
            if (
                request_id
                and depth <= 0
                and intent_kind == "plan-review"
                and isinstance(intent_approve, str)
                and bool(intent_approve)
            ):
                question = str(data.get("question") or "Review the plan")
                detail = data.get("detail")
                projected.append(EngineStreamEvent(
                    "approval",
                    {
                        "request_id": request_id,
                        "request_seq": sub_seq,
                        "status": "pending",
                        "tool_name": "exit_plan_mode",
                        "interaction_kind": "user_action",
                        "title": question,
                        "description": str(detail) if detail else "Review and approve the implementation plan",
                        "decision_reason": None,
                    },
                    raw=dict(envelope)
                ))
            elif request_id and depth <= 0:
                question = str(data.get("question") or "")[:4_000]
                raw_options = data.get("options")
                options = [
                    {
                        "label": str(row.get("label") or "")[:256],
                        "description": str(
                            row.get("description") or ""
                        )[:2_000],
                    }
                    for row in raw_options
                    if isinstance(row, Mapping) and row.get("label")
                ] if isinstance(raw_options, list) else []
                labels = [row["label"] for row in options]
                if (
                    question
                    and 2 <= len(options) <= 4
                    and len(labels) == len(set(labels))
                ):
                    projected.append(EngineStreamEvent(
                        "approval",
                        {
                            "request_id": request_id,
                            "request_seq": sub_seq,
                            "status": "pending",
                            "tool_name": "ask_user_question",
                            "interaction_kind": "question",
                            "questions": [{
                                "question": question,
                                "header": str(data.get("header") or "")[:256],
                                "multi_select": data.get("multi_select") is True,
                                "options": options,
                            }],
                        },
                        raw=dict(envelope),
                    ))
        elif native_type == "chatds/question/decided":
            request_id = str(data.get("request_id") or "")
            if request_id and depth <= 0:
                projected.append(EngineStreamEvent(
                    "approval",
                    {
                        "request_id": request_id,
                        "request_seq": sub_seq,
                        "status": (
                            "allowed"
                            if data.get("decision") == "allow"
                            else "denied"
                        ),
                        "tool_name": str(
                            data.get("tool_name") or "ask_user_question"
                        )[:512],
                        "interaction_kind": str(
                            data.get("interaction_kind") or "question"
                        )[:32],
                    },
                    raw=dict(envelope),
                ))
        elif native_type == "llm/retry":
            failure = data.get("failure")
            projected.append(self._agent_event(
                event_type="run.progress",
                run_id=run_id,
                seq=sub_seq,
                payload={
                    "stage": "provider_retry",
                    "attempt": data.get("retry"),
                    "detail": (
                        str(failure.get("message") or failure.get("code") or "provider_retry")
                        if isinstance(failure, Mapping) else "provider_retry"
                    ),
                },
                depth=depth,
                session_id=session_id,
            ))
        elif native_type == "turn/end":
            reason = data.get("reason")
            reason = reason if isinstance(reason, Mapping) else {}
            kind = str(reason.get("kind") or "error")
            succeeded = kind == "completed"
            cancelled = kind in {"aborted", "disposed", "interrupted"}
            projected.append(self._agent_event(
                event_type=(
                    "run.progress" if depth <= 0
                    else "run.completed" if succeeded
                    else "run.cancelled" if cancelled
                    else "run.failed"
                ),
                run_id=run_id,
                seq=sub_seq,
                payload={
                    "stage": "native_turn_settled" if depth <= 0 else "terminal",
                    "finish_reason": "stop" if succeeded else kind,
                    "terminal_reason": kind,
                    "error": None if succeeded else str(reason.get("error") or kind),
                },
                depth=depth,
                session_id=session_id,
            ))
        return tuple(projected)


class DeepSeekHarnessEngine:
    def __init__(
        self,
        *,
        base_url: str,
        internal_token: str,
        timeout_seconds: float,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._internal_token = internal_token
        self._timeout_seconds = float(timeout_seconds)
        self._client_factory = client_factory or httpx.AsyncClient

    @property
    def engine_id(self) -> str:
        return ENGINE_ID_DEEPSEEK_HARNESS

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Internal-Token": self._internal_token,
        }

    async def describe(self) -> EngineDescriptor:
        try:
            async with self._client_factory(timeout=5.0) as client:
                response = await client.get(
                    f"{self._base_url}/health", headers=self._headers
                )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return EngineDescriptor(
                id=self.engine_id,
                display_name="DeepSeek Harness",
                available=False,
                capabilities=("skills", "multi_agent", "sandbox", "web_search"),
                unavailable_reason=type(exc).__name__,
            )
        available = payload.get("status") == "ok"
        return EngineDescriptor(
            id=self.engine_id,
            display_name="DeepSeek Harness",
            available=available,
            version=str(payload.get("deepseek_harness_version") or "") or None,
            capabilities=("skills", "multi_agent", "sandbox", "web_search"),
            unavailable_reason=None if available else str(payload.get("code") or "unhealthy"),
        )

    def _start_payload(self, request: AgentEngineRequest) -> dict[str, Any]:
        provider = request.provider_config
        expected_native_session_id = f"chatds-{request.conversation_id}"
        if request.native_session_id != expected_native_session_id:
            raise AgentEngineError(
                "The DeepSeek Harness native Session identity is not bound to this Conversation.",
                code="deepseek_native_session_identity_invalid",
            )
        context_window_tokens = provider.get("context_length")
        if (
            type(context_window_tokens) is not int
            or context_window_tokens < 32_000
            or context_window_tokens > 4_000_000
        ):
            raise AgentEngineError(
                "The selected model lacks a valid DeepSeek Harness context binding.",
                code="deepseek_model_capability_invalid",
            )
        return {
            "run_id": request.run_id,
            "root_run_id": request.root_run_id,
            "user_id": request.user_id,
            "conversation_id": request.conversation_id,
            "native_session_id": expected_native_session_id,
            "model_id": request.model_id,
            "api_model": request.api_model,
            "provider_profile": str(provider.get("deepseek_harness_provider_profile") or ""),
            "provider_base_url": str(provider.get("base_url") or ""),
            "provider_protocol": str(provider.get("protocol") or ""),
            "messages": [dict(item) for item in request.messages],
            "max_output_tokens": request.max_output_tokens,
            "context_window_tokens": context_window_tokens,
            "workspace_path": str(request.metadata.get("workspace_path") or ""),
            "skill_view_path": request.skill_view_path,
            "skill_view_sha256": request.skill_view_sha256,
            "source": request.source,
            "user_turn_text": str(request.metadata.get("user_turn_text") or ""),
            "permission_preset": str(request.metadata.get("permission_preset") or "workspace_write"),
        }

    async def stream(self, request: AgentEngineRequest) -> AsyncIterator[EngineStreamEvent]:
        projector = DeepSeekEventProjector(request.root_run_id)
        try:
            async with self._client_factory(timeout=httpx.Timeout(120.0)) as client:
                response = await client.post(
                    f"{self._base_url}/v1/runs",
                    headers=self._headers,
                    json=self._start_payload(request),
                )
            if response.status_code >= 400:
                raise AgentEngineError(
                    f"DeepSeek Runner rejected the run (HTTP {response.status_code}): {response.text[:500]}",
                    code="deepseek_runner_start_rejected",
                    retryable=response.status_code >= 500,
                )
            after = 0
            reconnect_deadline: float | None = None
            authoritative_terminal = False
            while not authoritative_terminal:
                try:
                    timeout = httpx.Timeout(
                        connect=10.0,
                        read=self._timeout_seconds,
                        write=30.0,
                        pool=10.0,
                    )
                    async with self._client_factory(timeout=timeout) as client:
                        async with client.stream(
                            "GET",
                            f"{self._base_url}/v1/runs/{request.run_id}/events",
                            headers=self._headers,
                            params={"after": after},
                        ) as event_response:
                            if event_response.status_code >= 400:
                                raise AgentEngineError(
                                    f"DeepSeek Runner event stream failed (HTTP {event_response.status_code}).",
                                    code="deepseek_runner_event_rejected",
                                    retryable=event_response.status_code >= 500,
                                )
                            async for line in event_response.aiter_lines():
                                if not line.startswith("data: "):
                                    continue
                                value = line[6:]
                                if value == "[DONE]":
                                    break
                                try:
                                    envelope = json.loads(value)
                                except json.JSONDecodeError:
                                    yield EngineStreamEvent(
                                        "diagnostic", {"code": "malformed_deepseek_runner_event"}
                                    )
                                    continue
                                if not isinstance(envelope, dict):
                                    continue
                                seq = envelope.get("seq")
                                if not isinstance(seq, int) or seq <= after:
                                    continue
                                after = seq
                                reconnect_deadline = None
                                projections = projector.project(envelope)
                                if not projections:
                                    projections = (EngineStreamEvent(
                                        "diagnostic",
                                        {"code": "deepseek_native_event_unprojected"},
                                        raw=envelope,
                                    ),)
                                for event in projections:
                                    if event.raw is None:
                                        yield EngineStreamEvent(
                                            event.kind,
                                            event.data,
                                            raw=envelope,
                                            native_event_id=event.native_event_id,
                                        )
                                    else:
                                        yield event
                                native = envelope.get("event")
                                if (
                                    isinstance(native, dict)
                                    and native.get("type") == "chatds.supervisor.terminal"
                                ):
                                    authoritative_terminal = True
                    if not authoritative_terminal:
                        reconnect_deadline = reconnect_deadline or (time.monotonic() + 120.0)
                        if time.monotonic() >= reconnect_deadline:
                            raise AgentEngineError(
                                "DeepSeek Runner stream ended without an authoritative terminal.",
                                code="deepseek_runner_missing_terminal",
                            )
                        await asyncio.sleep(0.5)
                except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
                    reconnect_deadline = reconnect_deadline or (time.monotonic() + 120.0)
                    if time.monotonic() >= reconnect_deadline:
                        raise
                    await asyncio.sleep(0.5)
        except AgentEngineError:
            raise
        except httpx.TimeoutException as exc:
            raise AgentEngineError(
                "The DeepSeek Runner event stream became inactive.",
                code="deepseek_runner_event_stream_timeout",
                exception_class=type(exc).__name__,
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentEngineError(
                "The DeepSeek Runner transport failed.",
                code="deepseek_runner_transport_error",
                retryable=True,
                exception_class=type(exc).__name__,
            ) from exc

    async def decide_approval(
        self,
        *,
        user_id: str,
        conversation_id: str,
        run_id: str,
        request_id: str,
        request_seq: int,
        decision: str,
        answers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
        async with self._client_factory(timeout=timeout) as client:
            response = await client.post(
                f"{self._base_url}/v1/runs/{run_id}/approvals/{request_id}",
                headers=self._headers,
                json={
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "decision": decision,
                    "request_seq": request_seq,
                    **(
                        {"answers": dict(answers)}
                        if answers is not None
                        else {}
                    ),
                },
            )
        if response.status_code >= 400:
            raise AgentEngineError(
                f"DeepSeek Runner rejected the approval decision (HTTP {response.status_code}).",
                code="deepseek_runner_approval_rejected",
                retryable=response.status_code >= 500,
            )
        return response.json()

    async def cancel_run(
        self, *, user_id: str, conversation_id: str, run_id: str
    ) -> bool:
        async with self._client_factory(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url}/v1/runs/{run_id}/cancel",
                headers=self._headers,
                json={"user_id": user_id, "conversation_id": conversation_id},
            )
        return response.status_code < 400 and bool(response.json().get("success"))

    async def recover_terminal(
        self, *, user_id: str, conversation_id: str, run_id: str
    ) -> Mapping[str, Any] | None:
        """Read the supervisor-owned terminal after a Backend stream loss."""

        async with self._client_factory(timeout=120.0) as client:
            response = await client.post(
                f"{self._base_url}/v1/runs/{run_id}/terminal",
                headers=self._headers,
                json={
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                },
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise AgentEngineError(
                "DeepSeek Runner terminal recovery failed.",
                code="deepseek_runner_terminal_recovery_failed",
                retryable=response.status_code >= 500,
            )
        payload = response.json()
        terminal = payload.get("terminal") if isinstance(payload, Mapping) else None
        if terminal is None:
            return None
        if not isinstance(terminal, Mapping):
            raise AgentEngineError(
                "DeepSeek Runner returned an invalid terminal receipt.",
                code="deepseek_runner_terminal_receipt_invalid",
            )
        return dict(terminal)

    async def cleanup_session(
        self, *, user_id: str, conversation_id: str
    ) -> dict[str, Any]:
        async with self._client_factory(timeout=180.0) as client:
            response = await client.post(
                f"{self._base_url}/v1/sessions/{conversation_id}/cleanup",
                headers=self._headers,
                json={"user_id": user_id},
            )
        if response.status_code >= 400:
            return {"success": False, "error": f"deepseek_runner_http_{response.status_code}"}
        payload = response.json()
        return payload if isinstance(payload, dict) else {"success": False}
