"""Deterministic model routing boundaries for agentic harness runs.

The public chat endpoint may expose both a primary tool-capable model and an
auxiliary multimodal model.  A selected auxiliary model is allowed to answer
an explicit, tools-closed image question directly, but it must not silently
become the execution model for Skill workflows, delegates, or fallback chains.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


def _provider_id(config: Mapping[str, Any] | None) -> str:
    if not isinstance(config, Mapping):
        return ""
    return str(config.get("id") or config.get("api_model") or "").strip()


def is_auxiliary_only_provider(config: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(config, Mapping)
        and config.get("agentic_auxiliary_only") is True
    )


@dataclass(frozen=True, slots=True)
class AgenticModelRoutingDecision:
    provider: dict[str, Any]
    fallback_providers: tuple[dict[str, Any], ...]
    requested_model_id: str
    requested_provider_id: str
    effective_provider_id: str
    normalized: bool
    reason: str
    filtered_fallback_provider_ids: tuple[str, ...]
    explicit_direct_multimodal_auxiliary: bool

    def audit_payload(self) -> dict[str, Any]:
        return {
            "requested_model_id": self.requested_model_id,
            "requested_provider_id": self.requested_provider_id,
            "effective_provider_id": self.effective_provider_id,
            "effective_api_model": str(
                self.provider.get("api_model") or ""
            ),
            "normalized": self.normalized,
            "reason": self.reason,
            "filtered_fallback_provider_ids": list(
                self.filtered_fallback_provider_ids
            ),
            "explicit_direct_multimodal_auxiliary": (
                self.explicit_direct_multimodal_auxiliary
            ),
        }


def resolve_agentic_model_routing(
    *,
    requested_model_id: str,
    requested_provider: Mapping[str, Any],
    fallback_providers: Sequence[Mapping[str, Any]],
    primary_provider: Mapping[str, Any],
    agent_kind: str,
    execution_mode: str,
    has_image_input: bool,
    effective_tools: Sequence[str],
    auxiliary_provider_ids: Sequence[str] = (),
) -> AgenticModelRoutingDecision:
    """Return one closed routing decision before the first model request.

    Auxiliary providers are identified by trusted catalog metadata, never by
    prose or endpoint names.  Only a primary, direct-chat, image-bearing,
    tools-closed turn that explicitly requested that same auxiliary provider
    may keep it as the execution model.  Auxiliary providers are always
    removed from fallback chains, because a fallback is not an explicit
    multimodal selection.
    """

    auxiliary_ids = {
        str(provider_id or "").strip()
        for provider_id in auxiliary_provider_ids
        if str(provider_id or "").strip()
    }

    def auxiliary_only(config: Mapping[str, Any] | None) -> bool:
        return bool(
            is_auxiliary_only_provider(config)
            or _provider_id(config) in auxiliary_ids
            or (
                isinstance(config, Mapping)
                and str(config.get("api_model") or "").strip()
                in auxiliary_ids
            )
        )

    requested = dict(requested_provider)
    primary = dict(primary_provider)
    requested_provider_id = _provider_id(requested)
    requested_model = str(requested_model_id or "").strip()
    requested_auxiliary = auxiliary_only(requested)
    explicit_auxiliary = bool(
        requested_auxiliary
        and requested_model
        and requested_model in {
            requested_provider_id,
            str(requested.get("api_model") or "").strip(),
        }
    )
    direct_multimodal_auxiliary = bool(
        explicit_auxiliary
        and str(agent_kind or "").strip().casefold() == "primary"
        and str(execution_mode or "").strip().casefold() == "direct_chat"
        and has_image_input
        and not tuple(effective_tools or ())
    )

    filtered_ids: list[str] = []
    retained_fallbacks: list[dict[str, Any]] = []
    for raw_fallback in fallback_providers or ():
        fallback = dict(raw_fallback)
        if auxiliary_only(fallback):
            filtered_ids.append(_provider_id(fallback) or "<auxiliary>")
            continue
        retained_fallbacks.append(fallback)

    provider = requested
    normalized = False
    if requested_auxiliary and not direct_multimodal_auxiliary:
        provider = primary
        normalized = True
        if str(agent_kind or "").strip().casefold() != "primary":
            reason = "delegated_agent_requires_primary_model"
        elif str(execution_mode or "").strip().casefold() != "direct_chat":
            reason = "agentic_or_skill_workflow_requires_primary_model"
        elif not has_image_input:
            reason = "auxiliary_model_requires_image_input"
        elif tuple(effective_tools or ()):
            reason = "tool_using_turn_requires_primary_model"
        else:
            reason = "auxiliary_model_not_explicitly_requested"
    elif direct_multimodal_auxiliary:
        reason = "explicit_tools_closed_direct_multimodal_request"
    elif filtered_ids:
        reason = "auxiliary_agentic_fallbacks_filtered"
    else:
        reason = "requested_agentic_model_retained"

    effective_id = _provider_id(provider)
    retained_fallbacks = [
        fallback
        for fallback in retained_fallbacks
        if _provider_id(fallback) != effective_id
    ]
    return AgenticModelRoutingDecision(
        provider=provider,
        fallback_providers=tuple(retained_fallbacks),
        requested_model_id=requested_model,
        requested_provider_id=requested_provider_id,
        effective_provider_id=effective_id,
        normalized=normalized,
        reason=reason,
        filtered_fallback_provider_ids=tuple(filtered_ids),
        explicit_direct_multimodal_auxiliary=direct_multimodal_auxiliary,
    )
