"""Shared backend policy for models that are auxiliary to agent execution."""

from __future__ import annotations

from collections.abc import Iterable


# This identifier remains selectable for an explicit direct multimodal turn.
# It must not enter an agentic failover chain, because a fallback is neither an
# explicit visual selection nor a stable execution model for delegated work.
AGENTIC_AUXILIARY_ONLY_MODEL_IDS = frozenset({"qwen3_5"})
# New conversations use the externally hosted GLM-5.2 test route.  Keep the
# historic ``AgentModel`` alias pinned to the original local route: changing a
# deployment default must never silently rebind an already persisted model
# identity to another provider, credential, or capacity envelope.
DEFAULT_AGENT_MODEL_ID = "shaiengine_glm_5_2"
AGENT_MODEL_ID_ALIASES = {"AgentModel": "deepseek_v4_pro"}


def canonical_agent_model_id(model_id: object) -> str:
    candidate = str(model_id or "").strip()
    return AGENT_MODEL_ID_ALIASES.get(candidate, candidate)


def is_agentic_auxiliary_only_model(model_id: object) -> bool:
    return str(model_id or "").strip() in AGENTIC_AUXILIARY_ONLY_MODEL_IDS


def filter_agentic_fallback_model_ids(
    model_ids: Iterable[object],
    *,
    requested_model_id: object = "",
) -> tuple[list[str], list[str]]:
    """Return stable unique (allowed, removed) fallback identifiers."""

    requested = canonical_agent_model_id(requested_model_id)
    allowed: list[str] = []
    removed: list[str] = []
    seen: set[str] = set()
    for raw_model_id in model_ids:
        model_id = canonical_agent_model_id(raw_model_id)
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        if model_id == requested or is_agentic_auxiliary_only_model(model_id):
            removed.append(model_id)
            continue
        allowed.append(model_id)
    return allowed, removed
