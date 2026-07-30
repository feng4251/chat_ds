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


LEGACY_EFFECT_RECEIPT_VERSION = 1
EFFECT_RECEIPT_VERSION = 2
_READ_ONLY_EXTERNAL_METHODS = frozenset({"GET", "HEAD"})
_SUPPORTED_EXTERNAL_METHODS = frozenset({
    "GET",
    "HEAD",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
})
_EGRESS_AUDIT_PROFILE = "bounded_controlled_exchange"
_EGRESS_AUDIT_VERSION = 1
_EGRESS_AUDIT_KEYS = frozenset({
    "profile",
    "version",
    "budget_scope_sha256",
    "call_id_sha256",
    "rules_sha256",
    "counts",
    "limits",
    "exhausted",
    "receipt_sha256",
})
_EGRESS_AUDIT_COUNT_KEYS = frozenset({
    "accepted_connections",
    "client_to_proxy_wire_bytes",
    "proxy_to_client_wire_bytes",
    "budget_rejections",
    "clean_closes",
})
_EGRESS_AUDIT_LIMIT_KEYS = frozenset({
    "max_outbound_bytes",
    "max_requests",
    "max_response_wire_bytes",
})
_V2_EFFECT_RECEIPT_KEYS = frozenset({
    "version",
    "effect_known",
    "terminal_response_present",
    "process_teardown_complete",
    "artifact_commit_count",
    "workspace_mutation_count",
    "authorized_external_methods",
    "external_effect_class",
    "controlled_egress",
    "egress_rule_set_sha256",
    "operation_id_sha256",
    "egress_policy_version",
    "egress_audit_receipt_present",
    "egress_audit_receipt_valid",
    "egress_audit_profile",
    "egress_audit_version",
    "egress_audit_receipt_sha256",
    "egress_audit_rules_sha256",
    "egress_observation_complete",
    "external_disclosure_occurred",
    "accepted_connections",
    "client_to_proxy_wire_bytes",
    "proxy_to_client_wire_bytes",
    "budget_rejections",
    "clean_closes",
    "max_outbound_bytes",
    "max_requests",
    "max_response_wire_bytes",
    "egress_budget_exhausted",
    "replay_safe",
    "receipt_sha256",
})


def _sha256_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _is_positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _matches_sha256_json(claimed: Any, value: Mapping[str, Any]) -> bool:
    if not _is_sha256(claimed):
        return False
    try:
        computed = _sha256_json(value)
    except (TypeError, ValueError, UnicodeError):
        return False
    return hmac.compare_digest(claimed, computed)


def _validated_egress_audit_receipt(
    value: Any,
) -> dict[str, Any] | None:
    """Validate one disclosure-free executor/proxy audit receipt.

    The receipt digest is an integrity checksum for the trusted internal
    transport, not a substitute for the authenticated executor/proxy channel.
    Runtime scope and call identifiers remain inside the audit receipt and are
    deliberately not projected into the model-visible effect receipt.
    """

    if not isinstance(value, Mapping) or set(value) != _EGRESS_AUDIT_KEYS:
        return None
    counts = value.get("counts")
    limits = value.get("limits")
    if (
        not isinstance(counts, Mapping)
        or set(counts) != _EGRESS_AUDIT_COUNT_KEYS
        or not isinstance(limits, Mapping)
        or set(limits) != _EGRESS_AUDIT_LIMIT_KEYS
        or value.get("profile") != _EGRESS_AUDIT_PROFILE
        or type(value.get("version")) is not int
        or value.get("version") != _EGRESS_AUDIT_VERSION
        or type(value.get("exhausted")) is not bool
        or not all(
            _is_sha256(value.get(field))
            for field in (
                "budget_scope_sha256",
                "call_id_sha256",
                "rules_sha256",
                "receipt_sha256",
            )
        )
        or not all(
            _is_nonnegative_int(counts.get(field))
            for field in _EGRESS_AUDIT_COUNT_KEYS
        )
        or not all(
            _is_positive_int(limits.get(field))
            for field in _EGRESS_AUDIT_LIMIT_KEYS
        )
    ):
        return None

    canonical_counts = {
        field: counts[field]
        for field in sorted(_EGRESS_AUDIT_COUNT_KEYS)
    }
    canonical_limits = {
        field: limits[field]
        for field in sorted(_EGRESS_AUDIT_LIMIT_KEYS)
    }
    canonical: dict[str, Any] = {
        "profile": value["profile"],
        "version": value["version"],
        "budget_scope_sha256": value["budget_scope_sha256"],
        "call_id_sha256": value["call_id_sha256"],
        "rules_sha256": value["rules_sha256"],
        "counts": canonical_counts,
        "limits": canonical_limits,
        "exhausted": value["exhausted"],
    }
    if not _matches_sha256_json(
        value["receipt_sha256"],
        canonical,
    ):
        return None

    accepted = canonical_counts["accepted_connections"]
    outbound = canonical_counts["client_to_proxy_wire_bytes"]
    inbound = canonical_counts["proxy_to_client_wire_bytes"]
    rejections = canonical_counts["budget_rejections"]
    clean_closes = canonical_counts["clean_closes"]
    if (
        accepted > canonical_limits["max_requests"]
        or outbound > canonical_limits["max_outbound_bytes"]
        or inbound > canonical_limits["max_response_wire_bytes"]
        or clean_closes > accepted
        or (
            value["exhausted"] is False
            and (
                rejections > 0
                or accepted >= canonical_limits["max_requests"]
                or outbound >= canonical_limits["max_outbound_bytes"]
                or inbound
                >= canonical_limits["max_response_wire_bytes"]
            )
        )
    ):
        return None

    return {
        **canonical,
        "receipt_sha256": value["receipt_sha256"],
    }


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
            or any(
                method not in _SUPPORTED_EXTERNAL_METHODS
                for method in normalized
            )
        ):
            return (), False
        methods.update(normalized)
    return tuple(sorted(methods)), True


def _valid_v2_effect_receipt(receipt: Mapping[str, Any]) -> bool:
    """Validate the complete secret-free v2 receipt projection."""

    if set(receipt) != _V2_EFFECT_RECEIPT_KEYS:
        return False
    methods = receipt.get("authorized_external_methods")
    if (
        receipt.get("version") != EFFECT_RECEIPT_VERSION
        or any(
            type(receipt.get(field)) is not bool
            for field in (
                "effect_known",
                "terminal_response_present",
                "process_teardown_complete",
                "controlled_egress",
                "egress_audit_receipt_present",
                "egress_audit_receipt_valid",
                "egress_observation_complete",
                "replay_safe",
            )
        )
        or not _is_nonnegative_int(receipt.get("artifact_commit_count"))
        or not _is_nonnegative_int(receipt.get("workspace_mutation_count"))
        or not isinstance(methods, list)
        or any(
            not isinstance(method, str)
            or method not in _SUPPORTED_EXTERNAL_METHODS
            for method in methods
        )
        or methods != sorted(set(methods))
        or receipt.get("external_effect_class")
        not in {"read_only", "potentially_mutating"}
        or not _is_sha256(receipt.get("egress_rule_set_sha256"))
        or (
            receipt.get("operation_id_sha256") is not None
            and not _is_sha256(receipt.get("operation_id_sha256"))
        )
        or (
            receipt.get("external_disclosure_occurred") is not None
            and type(receipt.get("external_disclosure_occurred")) is not bool
        )
        or (
            receipt.get("egress_budget_exhausted") is not None
            and type(receipt.get("egress_budget_exhausted")) is not bool
        )
        or not _is_sha256(receipt.get("receipt_sha256"))
    ):
        return False
    if (
        receipt["workspace_mutation_count"]
        != receipt["artifact_commit_count"]
        or receipt["terminal_response_present"] is not True
        or receipt["process_teardown_complete"] is not True
    ):
        return False

    controlled_egress = receipt["controlled_egress"]
    read_only_external = bool(
        (not controlled_egress or methods)
        and set(methods).issubset(_READ_ONLY_EXTERNAL_METHODS)
    )
    if (
        receipt.get("external_effect_class")
        != ("read_only" if read_only_external else "potentially_mutating")
        or (bool(methods) and not controlled_egress)
    ):
        return False

    metric_fields = (
        "accepted_connections",
        "client_to_proxy_wire_bytes",
        "proxy_to_client_wire_bytes",
        "budget_rejections",
        "clean_closes",
    )
    limit_fields = (
        "max_outbound_bytes",
        "max_requests",
        "max_response_wire_bytes",
    )
    if controlled_egress:
        audit_valid = receipt["egress_audit_receipt_valid"]
        if receipt["egress_audit_receipt_present"] is False and audit_valid:
            return False
        if audit_valid:
            if (
                receipt.get("egress_policy_version") != 3
                or receipt.get("egress_audit_profile")
                != _EGRESS_AUDIT_PROFILE
                or receipt.get("egress_audit_version")
                != _EGRESS_AUDIT_VERSION
                or not _is_sha256(
                    receipt.get("egress_audit_receipt_sha256")
                )
                or not _is_sha256(
                    receipt.get("egress_audit_rules_sha256")
                )
                or not all(
                    _is_nonnegative_int(receipt.get(field))
                    for field in metric_fields
                )
                or not all(
                    _is_positive_int(receipt.get(field))
                    for field in limit_fields
                )
            ):
                return False
            accepted = receipt["accepted_connections"]
            outbound = receipt["client_to_proxy_wire_bytes"]
            inbound = receipt["proxy_to_client_wire_bytes"]
            rejections = receipt["budget_rejections"]
            clean_closes = receipt["clean_closes"]
            exhausted = receipt["egress_budget_exhausted"]
            if (
                accepted > receipt["max_requests"]
                or outbound > receipt["max_outbound_bytes"]
                or inbound > receipt["max_response_wire_bytes"]
                or clean_closes > accepted
                or receipt["egress_observation_complete"]
                != (clean_closes == accepted)
                or receipt["external_disclosure_occurred"]
                != (outbound > 0)
                # The bridge can attest only invocation-local counters. The
                # proxy owns the cross-call scope ledger and does not yet
                # return a terminal aggregate attestation, so its exhaustion
                # state must remain unknown rather than borrowing the local
                # bridge's similarly bounded limit.
                or exhausted is not None
            ):
                return False
        elif any(
            receipt.get(field) is not None
            for field in (
                "egress_policy_version",
                "egress_audit_profile",
                "egress_audit_version",
                "egress_audit_receipt_sha256",
                "egress_audit_rules_sha256",
                *metric_fields,
                *limit_fields,
                "egress_budget_exhausted",
                "external_disclosure_occurred",
            )
        ) or receipt["egress_observation_complete"] is not False:
            return False
    elif (
        receipt["egress_audit_receipt_valid"]
        or receipt.get("egress_policy_version") is not None
        or receipt.get("egress_audit_profile") is not None
        or receipt.get("egress_audit_version") is not None
        or receipt.get("egress_audit_receipt_sha256") is not None
        or receipt.get("egress_audit_rules_sha256") is not None
        or any(receipt.get(field) != 0 for field in metric_fields)
        or any(receipt.get(field) is not None for field in limit_fields)
        or receipt.get("egress_budget_exhausted") is not False
        or receipt.get("external_disclosure_occurred") is not False
        or receipt.get("egress_observation_complete") is not True
    ):
        return False

    if controlled_egress and (
        receipt["effect_known"] is not False
        or receipt["replay_safe"] is not False
    ):
        return False
    if receipt["effect_known"] and (
        receipt["egress_observation_complete"] is not True
        or (
            receipt["controlled_egress"]
            and (
                receipt["egress_audit_receipt_valid"] is not True
                or receipt.get("egress_budget_exhausted") is not False
                or receipt.get("budget_rejections") != 0
            )
        )
    ):
        return False
    if receipt["replay_safe"] and (
        receipt["effect_known"] is not True
        or receipt["artifact_commit_count"] != 0
        or receipt["workspace_mutation_count"] != 0
        or not read_only_external
    ):
        return False
    return True


def build_isolated_execution_effect_receipt(
    *,
    result: Mapping[str, Any],
    egress_rules: Sequence[Any],
    tool_operation_id: str | None,
) -> dict[str, Any]:
    """Build one handler-owned receipt from an isolated executor response."""

    raw_egress_rules = tuple(egress_rules)
    has_egress_rules = bool(raw_egress_rules)
    artifacts = result.get("artifacts")
    artifacts_known = isinstance(artifacts, list)
    artifact_count = len(artifacts) if artifacts_known else -1
    methods, methods_known = _normalized_authorized_methods(
        raw_egress_rules
    )
    read_only_external = bool(methods_known) and set(methods).issubset(
        _READ_ONLY_EXTERNAL_METHODS
    )
    operation_id = str(tool_operation_id or "").strip()
    audit_present = "egress_audit_receipt" in result
    audit = (
        _validated_egress_audit_receipt(
            result.get("egress_audit_receipt")
        )
        if has_egress_rules
        else None
    )
    audit_valid = audit is not None
    if audit is None:
        counts: Mapping[str, Any] | None = None
        limits: Mapping[str, Any] | None = None
        observation_complete = not has_egress_rules
        disclosure_occurred: bool | None = (
            False if not has_egress_rules else None
        )
        egress_effect_known = not has_egress_rules
    else:
        counts = audit["counts"]
        limits = audit["limits"]
        observation_complete = (
            counts["clean_closes"] == counts["accepted_connections"]
        )
        disclosure_occurred = bool(
            counts["client_to_proxy_wire_bytes"] > 0
        )
        # This receipt is emitted by the invocation-local bridge. The proxy's
        # cross-call scope ledger can reject or truncate a later request
        # without a structured terminal signal reaching the bridge. Preserve
        # the local counters as telemetry, but do not use them to authorize a
        # replay until the proxy supplies its own aggregate attestation.
        egress_effect_known = False
    effect_known = bool(
        artifacts_known
        and methods_known
        and egress_effect_known
    )
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
        "controlled_egress": has_egress_rules,
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
                for rule in raw_egress_rules
            ],
        }),
        "operation_id_sha256": (
            hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
            if operation_id
            else None
        ),
        "egress_policy_version": 3 if audit_valid else None,
        "egress_audit_receipt_present": audit_present,
        "egress_audit_receipt_valid": audit_valid,
        "egress_audit_profile": (
            audit["profile"] if audit is not None else None
        ),
        "egress_audit_version": (
            audit["version"] if audit is not None else None
        ),
        "egress_audit_receipt_sha256": (
            audit["receipt_sha256"] if audit is not None else None
        ),
        "egress_audit_rules_sha256": (
            audit["rules_sha256"] if audit is not None else None
        ),
        "egress_observation_complete": observation_complete,
        "external_disclosure_occurred": disclosure_occurred,
        "accepted_connections": (
            counts["accepted_connections"] if counts is not None else (
                0 if not has_egress_rules else None
            )
        ),
        "client_to_proxy_wire_bytes": (
            counts["client_to_proxy_wire_bytes"] if counts is not None else (
                0 if not has_egress_rules else None
            )
        ),
        "proxy_to_client_wire_bytes": (
            counts["proxy_to_client_wire_bytes"] if counts is not None else (
                0 if not has_egress_rules else None
            )
        ),
        "budget_rejections": (
            counts["budget_rejections"] if counts is not None else (
                0 if not has_egress_rules else None
            )
        ),
        "clean_closes": (
            counts["clean_closes"] if counts is not None else (
                0 if not has_egress_rules else None
            )
        ),
        "max_outbound_bytes": (
            limits["max_outbound_bytes"] if limits is not None else None
        ),
        "max_requests": (
            limits["max_requests"] if limits is not None else None
        ),
        "max_response_wire_bytes": (
            limits["max_response_wire_bytes"]
            if limits is not None
            else None
        ),
        "egress_budget_exhausted": (
            None if audit is not None else (
                False if not has_egress_rules else None
            )
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
    claimed = base.pop("receipt_sha256", None)
    if not _matches_sha256_json(claimed, base):
        return None
    version = base.get("version")
    complete_receipt = {
        **base,
        "receipt_sha256": claimed,
    }
    if (
        version == EFFECT_RECEIPT_VERSION
        and not _valid_v2_effect_receipt(complete_receipt)
    ):
        return None
    if version not in {
        LEGACY_EFFECT_RECEIPT_VERSION,
        EFFECT_RECEIPT_VERSION,
    }:
        return None
    bound = {
        **complete_receipt,
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
    binding_sha = bound.pop("binding_sha256", None)
    if not _matches_sha256_json(binding_sha, bound):
        return False
    base = {
        key: value
        for key, value in bound.items()
        if key not in {"tool_name", "tool_call_id_sha256"}
    }
    receipt_sha = base.pop("receipt_sha256", None)
    if (
        not _matches_sha256_json(receipt_sha, base)
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
    version = bound.get("version")
    if version == EFFECT_RECEIPT_VERSION:
        complete_receipt = {
            **base,
            "receipt_sha256": receipt_sha,
        }
        if not _valid_v2_effect_receipt(complete_receipt):
            return False
        if (
            bound.get("egress_observation_complete") is not True
            or (
                bound.get("controlled_egress") is True
                and (
                    bound.get("egress_policy_version") != 3
                    or bound.get("egress_audit_receipt_valid") is not True
                    or bound.get("egress_budget_exhausted") is not False
                    or bound.get("budget_rejections") != 0
                )
            )
        ):
            return False
    elif version == LEGACY_EFFECT_RECEIPT_VERSION:
        # Legacy receipts never carried a proxy-owned aggregate terminal
        # attestation. Keep parsing compatibility, but do not let a
        # controlled-egress v1 receipt authorize automatic replay.
        if bound.get("controlled_egress") is True:
            return False
    else:
        return False
    return bool(
        bound.get("effect_known") is True
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
