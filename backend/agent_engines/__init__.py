"""Backend-facing Agent Engine contract and concrete adapters.

The ChatDS application layer owns durable users, conversations, events and
workspaces.  An Agent Engine owns one model/tool runtime.  Keeping this seam
small prevents either runtime's private orchestration state from leaking into
the other while still giving the Backend one lossless stream contract.
"""

from .base import (
    AgentEngine,
    AgentEngineError,
    AgentEngineRequest,
    EngineDescriptor,
    EngineStreamEvent,
)
from .registry import AgentEngineRegistry, build_agent_engine_registry
from .claude_events import ClaudeEventProjector

__all__ = [
    "AgentEngine",
    "AgentEngineError",
    "AgentEngineRequest",
    "AgentEngineRegistry",
    "EngineDescriptor",
    "EngineStreamEvent",
    "ClaudeEventProjector",
    "build_agent_engine_registry",
]
