"""Shared types and abstract base for provider transports."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Normalized types ────────────────────────────────────────────────────


@dataclass
class ToolCall:
    """A normalized tool call from any provider."""

    id: str | None
    name: str
    arguments: str  # JSON string
    provider_data: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def type(self) -> str:
        return "function"

    @property
    def function(self) -> ToolCall:
        """Return self so tc.function.name / tc.function.arguments work."""
        return self


@dataclass
class Usage:
    """Token usage from an API response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class NormalizedResponse:
    """Normalized API response from any provider."""

    content: str | None
    tool_calls: list[ToolCall] | None
    finish_reason: str  # "stop", "tool_calls", "length", "content_filter"
    reasoning: str | None = None
    usage: Usage | None = None
    provider_data: dict[str, Any] | None = field(default=None, repr=False)


def build_tool_call(
    id: str | None,
    name: str,
    arguments: Any,
    **provider_fields: Any,
) -> ToolCall:
    """Build a ToolCall, auto-serialising arguments if it's a dict."""
    args_str = (
        json.dumps(arguments, ensure_ascii=False)
        if isinstance(arguments, dict)
        else str(arguments)
    )
    pd = dict(provider_fields) if provider_fields else None
    return ToolCall(id=id, name=name, arguments=args_str, provider_data=pd)


# ── Abstract transport ──────────────────────────────────────────────────


class ProviderTransport(ABC):
    """Base class for provider-specific format conversion and normalization."""

    @property
    @abstractmethod
    def api_mode(self) -> str:
        """The api_mode string this transport handles (e.g. 'chat_completions')."""
        ...

    @abstractmethod
    def convert_messages(
        self, messages: List[Dict[str, Any]], **kwargs
    ) -> Any:
        """Convert OpenAI-format messages to provider-native format."""
        ...

    @abstractmethod
    def convert_tools(
        self, tools: List[Dict[str, Any]]
    ) -> Any:
        """Convert OpenAI-format tool definitions to provider-native format."""
        ...

    @abstractmethod
    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **params,
    ) -> Dict[str, Any]:
        """Build the complete API call kwargs dict."""
        ...

    @abstractmethod
    def normalize_response(
        self, response: Any, **kwargs
    ) -> NormalizedResponse:
        """Normalize a raw provider response to NormalizedResponse."""
        ...

    def validate_response(self, response: Any) -> bool:
        """Optional: check if the raw response is structurally valid."""
        return True

    def map_finish_reason(self, raw_reason: str) -> str:
        """Optional: map provider-specific stop reason to OpenAI equivalent."""
        return raw_reason