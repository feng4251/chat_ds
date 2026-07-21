"""Finite, domain-neutral HTTP retrieval coverage policies.

This module intentionally has no harness-package imports.  The Skill compiler,
delegation boundary, retrieval tracker, and agent loop all depend on the same
closed vocabulary without creating an import cycle through ``skills``.
"""

from __future__ import annotations

import re
from typing import Any


RETRIEVAL_COMPLETENESS_POLICY_BOUNDED = "bounded"
RETRIEVAL_COMPLETENESS_POLICY_EXHAUSTIVE = "exhaustive"
RETRIEVAL_COMPLETENESS_POLICIES = frozenset({
    RETRIEVAL_COMPLETENESS_POLICY_BOUNDED,
    RETRIEVAL_COMPLETENESS_POLICY_EXHAUSTIVE,
})

_RETRIEVAL_COMPLETENESS_POLICY_ALIASES = {
    "bounded": RETRIEVAL_COMPLETENESS_POLICY_BOUNDED,
    "bounded_acquisition": RETRIEVAL_COMPLETENESS_POLICY_BOUNDED,
    "bounded_evidence": RETRIEVAL_COMPLETENESS_POLICY_BOUNDED,
    "best_effort": RETRIEVAL_COMPLETENESS_POLICY_BOUNDED,
    "sample": RETRIEVAL_COMPLETENESS_POLICY_BOUNDED,
    "sampled": RETRIEVAL_COMPLETENESS_POLICY_BOUNDED,
    "exhaustive": RETRIEVAL_COMPLETENESS_POLICY_EXHAUSTIVE,
    "all_pages": RETRIEVAL_COMPLETENESS_POLICY_EXHAUSTIVE,
    "complete_all_pages": RETRIEVAL_COMPLETENESS_POLICY_EXHAUSTIVE,
}


def normalize_retrieval_completeness_policy(
    value: Any,
    *,
    default: str = RETRIEVAL_COMPLETENESS_POLICY_BOUNDED,
) -> str:
    """Return one finite HTTP coverage policy or fail closed.

    A protocol-level next cursor does not itself declare an all-pages task.
    Consequently the default is bounded acquisition, while exhaustive
    traversal must be explicitly declared with one of the finite aliases.
    """

    if default not in RETRIEVAL_COMPLETENESS_POLICIES:
        raise ValueError("invalid default retrieval completeness policy")
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(
            "retrieval_completeness_policy must be a string"
        )
    normalized = re.sub(
        r"[^a-z0-9]+", "_", value.strip().casefold()
    ).strip("_")
    policy = _RETRIEVAL_COMPLETENESS_POLICY_ALIASES.get(normalized)
    if policy is None:
        raise ValueError(
            "retrieval_completeness_policy must be one of: bounded, exhaustive"
        )
    return policy
