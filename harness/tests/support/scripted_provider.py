"""Deterministic OpenAI-compatible provider fixture.

The fixture deliberately models the HTTP/SSE boundary instead of mocking
Harness internals.  A test supplies an ordered script of provider turns and
can then assert the exact request sequence that the real agent loop emitted.
No request headers are retained, so API keys and other credentials cannot
enter test diagnostics.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import copy
import json
from typing import Any, Callable, Iterable


RequestAssertion = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class ScriptedTurn:
    """One bounded provider response and its optional request assertion."""

    lines: tuple[str, ...]
    status_code: int = 200
    assert_request: RequestAssertion | None = None
    stream_error_after_lines: int | None = None
    stream_error: BaseException | None = None


def _data(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def stop_turn(
    content: str,
    *,
    finish_reason: str = "stop",
    reasoning: str = "",
    assert_request: RequestAssertion | None = None,
) -> ScriptedTurn:
    """Return one ordinary visible terminal response."""

    delta: dict[str, Any] = {}
    if content:
        delta["content"] = content
    if reasoning:
        delta["reasoning_content"] = reasoning
    return ScriptedTurn(
        lines=(
            _data({
                "choices": [{
                    "delta": delta,
                    "finish_reason": None,
                }],
            }),
            _data({
                "choices": [{
                    "delta": {},
                    "finish_reason": finish_reason,
                }],
            }),
            "data: [DONE]",
        ),
        assert_request=assert_request,
    )


def tool_call_turn(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    call_id: str,
    assert_request: RequestAssertion | None = None,
) -> ScriptedTurn:
    """Return one complete atomic streamed tool-call batch."""

    return ScriptedTurn(
        lines=(
            _data({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(
                                    arguments,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }),
            _data({
                "choices": [{
                    "delta": {},
                    "finish_reason": "tool_calls",
                }],
            }),
            "data: [DONE]",
        ),
        assert_request=assert_request,
    )


def interrupted_turn(
    visible_prefix: str,
    error: BaseException,
    *,
    assert_request: RequestAssertion | None = None,
) -> ScriptedTurn:
    """Emit one visible SSE delta and then raise a transport exception."""

    return ScriptedTurn(
        lines=(
            _data({
                "choices": [{
                    "delta": {"content": visible_prefix},
                    "finish_reason": None,
                }],
            }),
        ),
        assert_request=assert_request,
        stream_error_after_lines=1,
        stream_error=error,
    )


class _ScriptedResponse:
    def __init__(self, turn: ScriptedTurn):
        self.status_code = turn.status_code
        self._turn = turn

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for index, line in enumerate(self._turn.lines, start=1):
            yield line
            if line.startswith("data:"):
                # Match httpx' event separator behavior used by production.
                yield ""
            if (
                self._turn.stream_error_after_lines is not None
                and index >= self._turn.stream_error_after_lines
            ):
                raise (
                    self._turn.stream_error
                    or RuntimeError("scripted provider stream interruption")
                )


class _ScriptedAsyncClient:
    def __init__(self, provider: "ScriptedProvider"):
        self._provider = provider

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str, **kwargs):
        return self._provider._next_response(
            method=method,
            url=url,
            request_body=kwargs.get("json"),
            transport="stream",
        )


class ScriptedProvider:
    """Ordered, assertion-friendly provider double for agent-loop tests."""

    def __init__(self, turns: Iterable[ScriptedTurn]):
        self._turns = deque(turns)
        self.requests: list[dict[str, Any]] = []

    def client_factory(self, *args, **kwargs):
        # Constructor args can contain transport objects or timeouts. They are
        # irrelevant to scripted semantics and intentionally not retained.
        return _ScriptedAsyncClient(self)

    def _next_response(
        self,
        *,
        method: str,
        url: str,
        request_body: Any,
        transport: str,
    ) -> _ScriptedResponse:
        if not self._turns:
            raise AssertionError(
                "ScriptedProvider received an unexpected extra request"
            )
        if not isinstance(request_body, dict):
            raise AssertionError(
                "ScriptedProvider requires one JSON object request body"
            )
        body = copy.deepcopy(request_body)
        turn = self._turns.popleft()
        if turn.assert_request is not None:
            turn.assert_request(body)
        self.requests.append({
            "method": method,
            "url_path": url.rsplit("/", 1)[-1],
            "transport": transport,
            "body": body,
        })
        return _ScriptedResponse(turn)

    @property
    def remaining_turns(self) -> int:
        return len(self._turns)

    def assert_exhausted(self) -> None:
        if self._turns:
            raise AssertionError(
                f"ScriptedProvider has {len(self._turns)} unused turn(s)"
            )
