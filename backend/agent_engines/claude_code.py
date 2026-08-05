"""Backend adapter for the trusted Claude Runner Supervisor."""

from __future__ import annotations

import json
import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from .base import (
    ENGINE_ID_CLAUDE_CODE,
    AgentEngineError,
    AgentEngineRequest,
    EngineDescriptor,
    EngineStreamEvent,
)
from .claude_events import ClaudeEventProjector


class ClaudeCodeEngine:
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
        return ENGINE_ID_CLAUDE_CODE

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
                display_name="Claude Code",
                available=False,
                capabilities=("skills", "multi_agent", "sandbox", "native_resume"),
                unavailable_reason=type(exc).__name__,
            )
        available = payload.get("status") == "ok"
        return EngineDescriptor(
            id=self.engine_id,
            display_name="Claude Code",
            available=available,
            version=str(payload.get("claude_version") or "") or None,
            capabilities=("skills", "multi_agent", "sandbox", "native_resume"),
            unavailable_reason=None if available else str(payload.get("code") or "unhealthy"),
        )

    def _start_payload(self, request: AgentEngineRequest) -> dict[str, Any]:
        provider = request.provider_config
        # Credentials and arbitrary caller headers never cross this API. The
        # Supervisor resolves its deployment-owned provider profile.
        return {
            "run_id": request.run_id,
            "root_run_id": request.root_run_id,
            "user_id": request.user_id,
            "conversation_id": request.conversation_id,
            "model_id": request.model_id,
            "api_model": request.api_model,
            # This is an explicit deployment binding, not the broad provider
            # family used by Legacy Harness routing.  A model becomes Claude
            # compatible only when its catalog row names one configured
            # Supervisor profile.
            "provider_profile": str(
                provider.get("claude_provider_profile") or ""
            ),
            "provider_base_url": str(provider.get("base_url") or ""),
            "provider_protocol": str(provider.get("protocol") or ""),
            "messages": [dict(item) for item in request.messages],
            "max_output_tokens": request.max_output_tokens,
            "workspace_path": str(request.metadata.get("workspace_path") or ""),
            "skill_view_path": request.skill_view_path,
            "skill_view_sha256": request.skill_view_sha256,
            "native_session_id": request.native_session_id,
            "resume_from_native_session_id": request.resume_from_native_session_id,
            "source": request.source,
            "user_turn_text": str(request.metadata.get("user_turn_text") or ""),
        }

    async def stream(
        self,
        request: AgentEngineRequest,
    ) -> AsyncIterator[EngineStreamEvent]:
        projector = ClaudeEventProjector(request.root_run_id)
        try:
            async with self._client_factory(timeout=300.0) as client:
                response = await client.post(
                    f"{self._base_url}/v1/runs",
                    headers=self._headers,
                    json=self._start_payload(request),
                )
            if response.status_code >= 400:
                raise AgentEngineError(
                    "Claude Runner rejected the run "
                    f"(HTTP {response.status_code}): {response.text[:500]}",
                    code="claude_runner_start_rejected",
                    retryable=response.status_code >= 500,
                )
            start = response.json()
            after = 0
            reconnects = 0
            reconnect_deadline: float | None = None
            terminal = False
            authoritative_terminal_seen = False
            while not terminal:
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
                                body = (await event_response.aread()).decode(
                                    "utf-8", "ignore"
                                )[:500]
                                raise AgentEngineError(
                                    "Claude Runner event stream failed "
                                    f"(HTTP {event_response.status_code}): {body}",
                                    code="claude_runner_event_rejected",
                                    retryable=event_response.status_code >= 500,
                                )
                            async for line in event_response.aiter_lines():
                                if not line.startswith("data: "):
                                    continue
                                value = line[6:]
                                if value == "[DONE]":
                                    terminal = authoritative_terminal_seen
                                    break
                                try:
                                    envelope = json.loads(value)
                                except json.JSONDecodeError:
                                    yield EngineStreamEvent(
                                        "diagnostic",
                                        {"code": "malformed_claude_runner_event"},
                                    )
                                    continue
                                if not isinstance(envelope, dict):
                                    continue
                                seq = envelope.get("seq")
                                if not isinstance(seq, int) or seq <= after:
                                    continue
                                after = seq
                                reconnect_deadline = None
                                reconnects = 0
                                projected_events = projector.project(envelope)
                                if not projected_events:
                                    # Preserve every native envelope even when
                                    # it has no UI projection (pings, block
                                    # stops, hook details, future event types).
                                    projected_events = (EngineStreamEvent(
                                        "diagnostic",
                                        {"code": "claude_native_event_unprojected"},
                                        raw=envelope,
                                    ),)
                                for projection_index, event in enumerate(projected_events):
                                    if event.kind == "agent_event":
                                        projected_data = dict(event.data)
                                        projected_data.setdefault("type", "agent_event")
                                        projected_data.setdefault("root_run_id", request.root_run_id)
                                        # One native envelope can contain a
                                        # batch of tool results.  Give each
                                        # normalized event a stable sub-seq so
                                        # the durable (run,type,seq) key cannot
                                        # collapse siblings from that batch.
                                        if projection_index >= 1_000_000:
                                            raise AgentEngineError(
                                                "A Claude native event expanded beyond the "
                                                "bounded projection space.",
                                                code="claude_projection_batch_too_large",
                                                retryable=False,
                                            )
                                        projected_data.setdefault(
                                            "seq",
                                            int(seq or 0) * 1_000_000
                                            + projection_index,
                                        )
                                        event_type = str(projected_data.get("event_type") or "")
                                        payload = dict(projected_data.get("payload") or {})
                                        for field in (
                                            "finish_reason", "terminal_reason", "error",
                                            "native_task_id", "tool_name", "tool_call_id",
                                        ):
                                            if projected_data.get(field) is not None:
                                                payload.setdefault(field, projected_data[field])
                                        if event_type in {
                                            "run.completed", "run.failed", "run.cancelled",
                                        }:
                                            payload["authoritative"] = True
                                        yield EngineStreamEvent(
                                            kind=event.kind,
                                            data={**projected_data, "payload": payload},
                                            raw=event.raw,
                                            native_event_id=event.native_event_id,
                                        )
                                    else:
                                        yield event
                                native = envelope.get("event")
                                if (
                                    isinstance(native, dict)
                                    and native.get("type") == "chatds.supervisor.terminal"
                                ):
                                    authoritative_terminal_seen = True
                                    terminal = True
                    if not terminal:
                        reconnect_deadline = reconnect_deadline or (
                            time.monotonic() + 120.0
                        )
                        reconnects += 1
                        if time.monotonic() >= reconnect_deadline:
                            raise AgentEngineError(
                                "Claude Runner stream ended without an authoritative terminal.",
                                code="claude_runner_missing_terminal",
                                retryable=False,
                            )
                        await asyncio.sleep(
                            min(5.0, 0.25 * (2 ** min(reconnects, 5)))
                        )
                except AgentEngineError as exc:
                    reconnect_deadline = reconnect_deadline or (time.monotonic() + 120.0)
                    if not exc.retryable or time.monotonic() >= reconnect_deadline:
                        raise
                    reconnects += 1
                    await asyncio.sleep(min(5.0, 0.25 * (2 ** min(reconnects, 5))))
                    continue
                except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
                    reconnect_deadline = reconnect_deadline or (time.monotonic() + 120.0)
                    reconnects += 1
                    if time.monotonic() >= reconnect_deadline:
                        raise
                    # Sequence replay reconnects transport only; it never
                    # re-dispatches the model Turn or duplicates effects.
                    await asyncio.sleep(min(5.0, 0.25 * (2 ** min(reconnects, 5))))
                    continue
            if start.get("native_session_id"):
                yield EngineStreamEvent(
                    "diagnostic",
                    {
                        "code": "claude_native_session",
                        "native_session_id": str(start["native_session_id"]),
                    },
                )
        except AgentEngineError:
            raise
        except httpx.TimeoutException as exc:
            raise AgentEngineError(
                "The Claude Runner event stream timed out.",
                code="claude_runner_timeout",
                retryable=False,
                exception_class=type(exc).__name__,
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentEngineError(
                "The Claude Runner transport failed.",
                code="claude_runner_transport_error",
                retryable=True,
                exception_class=type(exc).__name__,
            ) from exc

    async def cancel_run(
        self,
        *,
        user_id: str,
        conversation_id: str,
        run_id: str,
    ) -> bool:
        async with self._client_factory(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url}/v1/runs/{run_id}/cancel",
                headers=self._headers,
                json={"user_id": user_id, "conversation_id": conversation_id},
            )
        return response.status_code < 400 and bool(response.json().get("success"))

    async def cleanup_session(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        async with self._client_factory(timeout=180.0) as client:
            response = await client.post(
                f"{self._base_url}/v1/sessions/{conversation_id}/cleanup",
                headers=self._headers,
                json={"user_id": user_id},
            )
        if response.status_code >= 400:
            return {"success": False, "error": f"claude_runner_http_{response.status_code}"}
        payload = response.json()
        return payload if isinstance(payload, dict) else {"success": False}
