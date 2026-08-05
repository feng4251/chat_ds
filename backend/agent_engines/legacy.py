"""Adapter for the existing ChatDS Harness service."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from .base import (
    ENGINE_ID_LEGACY,
    AgentEngineError,
    AgentEngineRequest,
    EngineDescriptor,
    EngineStreamEvent,
)


class LegacyHarnessEngine:
    def __init__(
        self,
        *,
        base_url: str,
        internal_token: str,
        timeout_seconds: float,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._internal_token = internal_token
        self._timeout_seconds = float(timeout_seconds)
        self._client_factory = client_factory

    @property
    def engine_id(self) -> str:
        return ENGINE_ID_LEGACY

    async def describe(self) -> EngineDescriptor:
        try:
            async with self._client_factory(timeout=5.0) as client:
                response = await client.get(f"{self._base_url}/health")
            response.raise_for_status()
        except Exception as exc:
            return EngineDescriptor(
                id=self.engine_id,
                display_name="ChatDS Legacy Harness",
                available=False,
                capabilities=("skills", "multi_agent", "mcp", "sandbox"),
                unavailable_reason=type(exc).__name__,
            )
        return EngineDescriptor(
            id=self.engine_id,
            display_name="ChatDS Legacy Harness",
            available=True,
            capabilities=("skills", "multi_agent", "mcp", "sandbox"),
        )

    def _wire_payload(self, request: AgentEngineRequest) -> dict[str, Any]:
        return {
            "model": request.model_id,
            "messages": list(request.messages),
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "stream": True,
            "tools": list(request.tools),
            "session_id": request.conversation_id,
            "user": request.user_id,
            "provider_config": dict(request.provider_config),
            "fallback_configs": [dict(item) for item in request.fallback_configs],
            "source": request.source,
            "enabled_user_skills": list(request.enabled_user_skills),
            "session_skill_registry": [
                dict(item) for item in request.session_skill_registry
            ],
            "event_schema": "chatds.agent.v2",
            "run_metadata": {
                "run_id": request.run_id,
                "root_run_id": request.root_run_id,
                "agent_kind": "primary",
                "agent_name": "primary",
                "depth": 0,
                "workspace_scope": "shared_session",
                **dict(request.metadata),
            },
        }

    async def stream(
        self,
        request: AgentEngineRequest,
    ) -> AsyncIterator[EngineStreamEvent]:
        try:
            async with self._client_factory(
                timeout=self._timeout_seconds,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/v1/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "X-Internal-Token": self._internal_token,
                    },
                    json=self._wire_payload(request),
                ) as response:
                    if response.status_code >= 400:
                        body = (
                            await response.aread()
                        ).decode("utf-8", "ignore")[:300]
                        raise AgentEngineError(
                            f"Legacy Harness returned HTTP {response.status_code}: {body}",
                            code="engine_http_error",
                            retryable=response.status_code >= 500,
                        )
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        encoded = line[6:]
                        if encoded == "[DONE]":
                            return
                        try:
                            payload = json.loads(encoded)
                            choice = payload.get("choices", [{}])[0]
                            delta = choice.get("delta") or {}
                        except (json.JSONDecodeError, IndexError, TypeError):
                            yield EngineStreamEvent(
                                kind="diagnostic",
                                data={"code": "malformed_engine_stream_event"},
                            )
                            continue
                        if isinstance(delta.get("agent_event"), dict):
                            event = delta["agent_event"]
                            yield EngineStreamEvent(
                                kind="agent_event",
                                data=event,
                                raw=payload,
                                native_event_id=_native_event_id(event),
                            )
                        if delta.get("tool_progress"):
                            yield EngineStreamEvent(
                                kind="tool_progress",
                                data={"text": str(delta["tool_progress"])},
                                raw=payload,
                            )
                        if delta.get("reasoning"):
                            yield EngineStreamEvent(
                                kind="reasoning",
                                data={"text": str(delta["reasoning"])},
                                raw=payload,
                            )
                        if delta.get("content"):
                            yield EngineStreamEvent(
                                kind="content",
                                data={"text": str(delta["content"])},
                                raw=payload,
                            )
                        if isinstance(delta.get("usage"), dict):
                            yield EngineStreamEvent(
                                kind="usage",
                                data=delta["usage"],
                                raw=payload,
                            )
                        if isinstance(delta.get("model_switch"), dict):
                            yield EngineStreamEvent(
                                kind="model",
                                data=delta["model_switch"],
                                raw=payload,
                            )
                        if payload.get("model"):
                            yield EngineStreamEvent(
                                kind="model",
                                data={"resolved_model_id": payload["model"]},
                                raw=payload,
                            )
                        if "error" in payload or delta.get("error"):
                            yield EngineStreamEvent(
                                kind="diagnostic",
                                data={
                                    "code": "engine_stream_error",
                                    "message": str(
                                        payload.get("error") or delta.get("error")
                                    ),
                                },
                                raw=payload,
                            )
                        if choice.get("finish_reason"):
                            yield EngineStreamEvent(
                                kind="finish",
                                data={"finish_reason": choice["finish_reason"]},
                                raw=payload,
                            )
        except AgentEngineError:
            raise
        except httpx.ConnectError as exc:
            raise AgentEngineError(
                "Could not connect to the Legacy Harness service.",
                code="engine_connect_error",
                retryable=True,
                exception_class=type(exc).__name__,
            ) from exc
        except httpx.TimeoutException as exc:
            raise AgentEngineError(
                "The Legacy Harness stream timed out.",
                code="engine_timeout",
                retryable=False,
                exception_class=type(exc).__name__,
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentEngineError(
                "The Legacy Harness transport failed.",
                code="engine_transport_error",
                retryable=False,
                exception_class=type(exc).__name__,
            ) from exc

    async def cancel_run(
        self,
        *,
        user_id: str,
        conversation_id: str,
        run_id: str,
    ) -> bool:
        # Legacy cancellation remains owned by its existing session cleanup
        # transaction. A run-specific endpoint can be added without changing
        # the Backend contract when the Legacy runtime exposes one.
        return False

    async def cleanup_session(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        async with self._client_factory(timeout=httpx.Timeout(180.0)) as client:
            response = await client.post(
                f"{self._base_url}/internal/session/cleanup",
                headers={"X-Internal-Token": self._internal_token},
                params={"user_id": user_id, "session_id": conversation_id},
            )
        if response.status_code >= 400:
            return {
                "success": False,
                "error": f"legacy_harness_http_{response.status_code}",
            }
        payload = response.json()
        return payload if isinstance(payload, dict) else {"success": False}


def _native_event_id(event: dict[str, Any]) -> str | None:
    run_id = event.get("run_id")
    event_type = event.get("event_type")
    seq = event.get("seq")
    if run_id and event_type and isinstance(seq, int):
        return f"{run_id}:{event_type}:{seq}"
    return None
