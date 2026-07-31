"""Stable, secret-free failure classification for workflow control.

Localized prose remains useful to operators, but scheduling decisions must use
bounded machine fields.  This module deliberately knows nothing about a
particular Skill, domain, route, worker name, or provider.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


FAILURE_TAXONOMY_VERSION = 1
_COMMON_MODE_ORIGINS = frozenset({"harness", "validator"})
_HEX_OR_UUID_RE = re.compile(
    r"\b(?:[0-9a-f]{64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
)
_NUMBER_RE = re.compile(r"\b\d+\b")


def normalize_failure_token(value: Any, *, max_chars: int = 160) -> str:
    text = str(value or "").strip().casefold().replace("-", "_")
    text = re.sub(r"[^a-z0-9_.]+", "_", text).strip("_")
    return text[:max_chars]


def classify_failure_origin(failure_class: Any) -> str:
    normalized = normalize_failure_token(failure_class)
    if normalized in {
        "harness_invariant",
        "harness_invariant_common_mode",
        "deterministic_prerequisite",
    }:
        return "harness"
    if normalized in {"contract_validation", "validator"}:
        return "validator"
    if normalized in {"provider_protocol", "provider_transport"}:
        return "provider"
    if normalized in {
        "agent_contract_noncompliance",
        "completion_quality",
        "model_output_limit",
    }:
        return "model"
    if normalized in {
        "transient_external",
        "network",
        "rate_limit",
        "schema_error",
        "unauthorized",
    }:
        return "external"
    if normalized == "side_effect_state_uncertain":
        return "effect"
    if normalized.startswith("policy") or normalized.endswith("_denied"):
        return "policy"
    return "runtime"


def _error_shape(error: Any) -> str:
    text = " ".join(str(error or "").strip().casefold().split())
    text = _URL_RE.sub("<url>", text)
    text = _HEX_OR_UUID_RE.sub("<identity>", text)
    text = _ABSOLUTE_PATH_RE.sub("<path>", text)
    text = _NUMBER_RE.sub("<n>", text)
    return text[:1_024]


def build_failure_fingerprint(
    *,
    failure_class: Any,
    terminal_reason: Any,
    error: Any,
    failure_origin: Any = "",
) -> dict[str, str | int | bool]:
    origin = (
        normalize_failure_token(failure_origin)
        or classify_failure_origin(failure_class)
    )
    projection = {
        "version": FAILURE_TAXONOMY_VERSION,
        "origin": origin,
        "failure_class": normalize_failure_token(failure_class),
        "terminal_reason": normalize_failure_token(terminal_reason),
        "error_shape": _error_shape(error),
    }
    digest = hashlib.sha256(json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "failure_taxonomy_version": FAILURE_TAXONOMY_VERSION,
        "failure_origin": origin,
        "failure_fingerprint": digest,
        "common_mode_breaker_eligible": (
            origin in _COMMON_MODE_ORIGINS
        ),
    }


def common_mode_breaker_eligible(
    failure_origin: Any,
    failure_fingerprint: Any,
) -> bool:
    return bool(
        normalize_failure_token(failure_origin) in _COMMON_MODE_ORIGINS
        and isinstance(failure_fingerprint, str)
        and re.fullmatch(r"[0-9a-f]{64}", failure_fingerprint)
    )
