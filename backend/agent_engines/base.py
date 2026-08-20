"""Stable Backend-to-Agent-Engine protocol.

The protocol deliberately models observable effects rather than either
engine's internal prompt loop.  Raw native payloads travel alongside the
normalized projection and are persisted separately by the Backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Protocol, runtime_checkable


ENGINE_ID_LEGACY = "legacy"
ENGINE_ID_CLAUDE_CODE = "claude_code"
ENGINE_ID_DEEPSEEK_HARNESS = "deepseek_harness"
SUPPORTED_ENGINE_IDS = frozenset({
    ENGINE_ID_CLAUDE_CODE,
    ENGINE_ID_DEEPSEEK_HARNESS,
})
RETIRED_ENGINE_IDS = frozenset({ENGINE_ID_LEGACY})


@dataclass(frozen=True, slots=True)
class EngineDescriptor:
    id: str
    display_name: str
    available: bool
    version: str | None = None
    capabilities: tuple[str, ...] = ()
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AgentEngineRequest:
    run_id: str
    root_run_id: str
    user_id: str
    conversation_id: str
    model_id: str
    api_model: str
    messages: tuple[Mapping[str, Any], ...]
    max_output_tokens: int
    temperature: float
    provider_config: Mapping[str, Any]
    # Immutable, engine-neutral receipts for binary inputs that were lowered
    # into the exact Session workspace before dispatch. Messages reference
    # these receipts by value; raw attachment bytes never cross the engine
    # control plane.
    input_attachments: tuple[Mapping[str, Any], ...] = ()
    enabled_user_skills: tuple[str, ...] = ()
    session_skill_registry: tuple[Mapping[str, Any], ...] = ()
    skill_view_path: str | None = None
    skill_view_sha256: str | None = None
    # ``native_session_id`` is the fresh, transaction-local transcript
    # checkpoint that this Turn may publish.  Outcome authority remains in
    # durable receipts: an outer contract failure may still publish a fully
    # observed native transcript boundary so the next Turn does not roll back
    # to stale conversational context.
    native_session_id: str | None = None
    resume_from_native_session_id: str | None = None
    source: str = "chat"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EngineStreamEvent:
    """One ordered, normalized engine observation.

    ``kind`` is one of ``content``, ``reasoning``, ``tool_progress``,
    ``agent_event``, ``approval``, ``usage``, ``model``, ``finish``, or
    ``diagnostic``.
    ``raw`` retains the complete native event for a lossless audit stream.
    """

    kind: str
    data: Mapping[str, Any]
    raw: Mapping[str, Any] | None = None
    native_event_id: str | None = None


class AgentEngineError(RuntimeError):
    """Typed transport/runtime failure from an Agent Engine adapter."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        exception_class: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.exception_class = exception_class


@runtime_checkable
class AgentEngine(Protocol):
    @property
    def engine_id(self) -> str: ...

    async def describe(self) -> EngineDescriptor: ...

    def stream(
        self,
        request: AgentEngineRequest,
    ) -> AsyncIterator[EngineStreamEvent]: ...

    async def cancel_run(
        self,
        *,
        user_id: str,
        conversation_id: str,
        run_id: str,
    ) -> bool: ...

    async def cleanup_session(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> Mapping[str, Any]: ...
