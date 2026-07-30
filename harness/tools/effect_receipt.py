"""Secret-free invocation effect receipts for cancellable delegated tools.

Tool registry metadata describes the *maximum* effect of a capability.  A
particular isolated script invocation can be replay-safe even when the generic
runner is correctly registered as mutating.  These receipts let the trusted
handler narrow that one completed invocation without trusting model prose,
script output, raw arguments, or URLs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any


EFFECT_RECEIPT_VERSION = 1
_READ_ONLY_EXTERNAL_METHODS = frozenset({"GET", "HEAD"})


def _sha256_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_authorized_methods(
    egress_rules: Sequence[Any],
) -> tuple[tuple[str, ...], bool]:
    methods: set[str] = set()
    for rule in egress_rules:
        if isinstance(rule, Mapping):
            raw_methods = rule.get("methods")
        else:
            raw_methods = getattr(rule, "methods", None)
        if not isinstance(raw_methods, (list, tuple, set, frozenset)):
            return (), False
        normalized = tuple(
            str(method).strip().upper()
            for method in raw_methods
            if isinstance(method, str) and str(method).strip()
        )
        # A receipt narrows trusted authority; malformed or unknown methods
        # must never collapse to an empty (and therefore apparently read-only)
        # set.  The policy compiler normally prevents this, but the receipt
        # boundary remains independently fail closed.
        if (
            len(normalized) != len(raw_methods)
            or not normalized
            or any(method not in {
                "GET",
                "HEAD",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "OPTIONS",
            } for method in normalized)
        ):
            return (), False
        methods.update(normalized)
    return tuple(sorted(methods)), True


def build_isolated_execution_effect_receipt(
    *,
    result: Mapping[str, Any],
    egress_rules: Sequence[Any],
    tool_operation_id: str | None,
) -> dict[str, Any]:
    """Build one handler-owned receipt from an isolated executor response."""

    artifacts = result.get("artifacts")
    artifacts_known = isinstance(artifacts, list)
    artifact_count = len(artifacts) if artifacts_known else -1
    methods, methods_known = _normalized_authorized_methods(egress_rules)
    read_only_external = bool(methods_known) and set(methods).issubset(
        _READ_ONLY_EXTERNAL_METHODS
    )
    operation_id = str(tool_operation_id or "").strip()
    effect_known = bool(artifacts_known and methods_known)
    replay_safe = bool(
        effect_known
        and artifact_count == 0
        and read_only_external
    )
    receipt: dict[str, Any] = {
        "version": EFFECT_RECEIPT_VERSION,
        "effect_known": effect_known,
        # Returning from the isolated executor proves that its process/lease
        # teardown and artifact commit boundary completed. Cancellation before
        # this point produces no receipt and therefore remains fail-closed.
        "terminal_response_present": True,
        "process_teardown_complete": True,
        "artifact_commit_count": max(0, artifact_count),
        "workspace_mutation_count": max(0, artifact_count),
        "authorized_external_methods": list(methods),
        "external_effect_class": (
            "read_only"
            if read_only_external
            else "potentially_mutating"
        ),
        "controlled_egress": bool(methods),
        "egress_rule_set_sha256": _sha256_json({
            "rules": [
                (
                    dict(rule)
                    if isinstance(rule, Mapping)
                    else {
                        "url_prefix": str(
                            getattr(rule, "url_prefix", "")
                        ),
                        "methods": sorted(
                            str(method)
                            for method in (
                                getattr(rule, "methods", ()) or ()
                            )
                        ),
                    }
                )
                for rule in egress_rules
            ],
        }),
        "operation_id_sha256": (
            hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
            if operation_id
            else None
        ),
        "replay_safe": replay_safe,
    }
    receipt["receipt_sha256"] = _sha256_json(receipt)
    return receipt


def bind_effect_receipt_to_call(
    receipt: Any,
    *,
    tool_name: str,
    tool_call_id: str,
) -> dict[str, Any] | None:
    """Validate a handler receipt and bind it to one runtime tool call."""

    if not isinstance(receipt, Mapping):
        return None
    base = dict(receipt)
    claimed = str(base.pop("receipt_sha256", "") or "")
    if (
        len(claimed) != 64
        or not hmac.compare_digest(claimed, _sha256_json(base))
    ):
        return None
    bound = {
        **base,
        "receipt_sha256": claimed,
        "tool_name": str(tool_name or ""),
        "tool_call_id_sha256": hashlib.sha256(
            str(tool_call_id or "").encode("utf-8")
        ).hexdigest(),
    }
    bound["binding_sha256"] = _sha256_json(bound)
    return bound


def bound_effect_receipt_is_replay_safe(
    receipt: Any,
    *,
    tool_name: str,
    tool_call_id: str,
) -> bool:
    """Verify a call-bound zero-effect/read-only receipt."""

    if not isinstance(receipt, Mapping):
        return False
    bound = dict(receipt)
    binding_sha = str(bound.pop("binding_sha256", "") or "")
    if (
        len(binding_sha) != 64
        or not hmac.compare_digest(binding_sha, _sha256_json(bound))
    ):
        return False
    base = {
        key: value
        for key, value in bound.items()
        if key not in {"tool_name", "tool_call_id_sha256"}
    }
    receipt_sha = str(base.pop("receipt_sha256", "") or "")
    if (
        len(receipt_sha) != 64
        or not hmac.compare_digest(receipt_sha, _sha256_json(base))
        or bound.get("tool_name") != str(tool_name or "")
        or not hmac.compare_digest(
            str(bound.get("tool_call_id_sha256") or ""),
            hashlib.sha256(
                str(tool_call_id or "").encode("utf-8")
            ).hexdigest(),
        )
    ):
        return False
    methods = bound.get("authorized_external_methods")
    return bool(
        bound.get("version") == EFFECT_RECEIPT_VERSION
        and bound.get("effect_known") is True
        and bound.get("terminal_response_present") is True
        and bound.get("process_teardown_complete") is True
        and bound.get("replay_safe") is True
        and bound.get("external_effect_class") == "read_only"
        and isinstance(methods, list)
        and all(
            isinstance(method, str)
            and method in _READ_ONLY_EXTERNAL_METHODS
            for method in methods
        )
        and bound.get("artifact_commit_count") == 0
        and bound.get("workspace_mutation_count") == 0
    )
