"""Explicit execution context for session-aware agent tools.

Tool routing must never infer tenant/session identity from model arguments or
handler signatures. The runtime owns this context and passes it alongside
every dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Identity and routing information for one agent turn."""

    user_id: str = "default"
    session_id: str = "default"
    model_id: str = ""
    provider_config: dict[str, Any] | None = None
    fallback_configs: tuple[dict[str, Any], ...] = ()
    enabled_tools: tuple[str, ...] = ()
    source: str = "chat"
    enabled_user_skills: tuple[str, ...] = ()
