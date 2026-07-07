"""Abstract base class for pluggable context engines.

A context engine controls how conversation context is managed when
approaching the model's token limit. The built-in ContextCompressor
is the default implementation.

Ported from hermes-agent/agent/context_engine.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ContextEngine(ABC):
    """Base class all context engines must implement."""

    # -- Identity ----------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'compressor', 'lcm')."""

    # -- Token state -------------------------------------------------------

    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compression_count: int = 0

    # -- Compaction parameters ---------------------------------------------

    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6

    # -- Core interface ----------------------------------------------------

    @abstractmethod
    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Update tracked token usage from an API response."""

    @abstractmethod
    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        """Return True if compaction should fire this turn."""

    @abstractmethod
    def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
    ) -> list[dict[str, Any]]:
        """Compact the message list and return the new message list."""

    # -- Optional: pre-flight check ----------------------------------------

    def should_compress_preflight(self, messages: list[dict[str, Any]]) -> bool:
        """Quick rough check before the API call (no real token count yet)."""
        return False

    # -- Optional: session lifecycle ---------------------------------------

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """Called when a new conversation session begins."""

    def on_session_end(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Called at real session boundaries."""

    def on_session_reset(self) -> None:
        """Reset per-session state."""
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0

    # -- Optional: status / display ----------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return status dict for display/logging."""
        return {
            "last_prompt_tokens": self.last_prompt_tokens,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": (
                min(100, self.last_prompt_tokens / self.context_length * 100)
                if self.context_length else 0
            ),
            "compression_count": self.compression_count,
        }

    # -- Optional: model switch support ------------------------------------

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
    ) -> None:
        """Called when the user switches models."""
        self.context_length = context_length
        self.threshold_tokens = int(context_length * self.threshold_percent)