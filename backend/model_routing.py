"""Stable model identities for native Agent Engine Conversations."""

from __future__ import annotations

# New agentic conversations prefer the deployment-owned local AgentModel.
# Keep every persisted identifier bound to its original provider; changing the
# default affects only newly created conversations and never rebinds history.
DEFAULT_AGENT_MODEL_ID = "deepseek_v4_pro"
AGENT_MODEL_ID_ALIASES = {"AgentModel": "deepseek_v4_pro"}


def canonical_agent_model_id(model_id: object) -> str:
    candidate = str(model_id or "").strip()
    return AGENT_MODEL_ID_ALIASES.get(candidate, candidate)
