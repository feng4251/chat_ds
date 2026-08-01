"""Bounded, domain-neutral completeness receipts for delegated HTTP evidence.

The HTTP bridge remains the authority for network access.  This module only
describes what a completed response says about local truncation and common,
machine-readable pagination signals, then tracks whether a delegated evidence
chain was actually closed.  It never follows a URL, grants a host/path, or
interprets domain-specific records.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from retrieval_policy import (
    RETRIEVAL_COMPLETENESS_POLICY_BOUNDED,
    RETRIEVAL_COMPLETENESS_POLICY_EXHAUSTIVE,
    normalize_retrieval_completeness_policy,
)
from skills.http_grants import canonical_https_request_url


RETRIEVAL_RECEIPT_VERSION = 1
EVIDENCE_LEDGER_VERSION = 1
RETRIEVAL_QUALITY_IMPACT_ADVISORY = "advisory"
RETRIEVAL_QUALITY_IMPACT_DEGRADED = "degraded"
MAX_PAGINATION_SCAN_DEPTH = 8
MAX_PAGINATION_SCAN_NODES = 1_024
MAX_PAGINATION_HINTS = 4
MAX_PAGINATION_HINT_CHARS = 2_048
CONTINUATION_ACTION_VERSION = 1
MAX_COLLECTION_CANDIDATES = 64
MAX_COLLECTION_PATH_CHARS = 256
MAX_LEDGER_COLLECTION_PATHS = 4

# These are protocol conventions, not API- or Skill-specific field names.
_NEXT_TOKEN_KEYS = frozenset({
    "after",
    "continuation",
    "continuationtoken",
    "endcursor",
    "nextcursor",
    "nextpagecursor",
    "nextpagetoken",
    "nexttoken",
    "pagetoken",
})
_NEXT_URL_KEYS = frozenset({
    "nextlink",
    "nextpageurl",
    "nexturl",
})
_GENERIC_NEXT_KEYS = frozenset({"next"})
_HAS_MORE_KEYS = frozenset({
    "hasmore",
    "hasnext",
    "hasnextpage",
    "moreavailable",
})
_REQUEST_PAGE_KEYS = frozenset({
    *_NEXT_TOKEN_KEYS,
    "before",
    "cursor",
    "first",
    "last",
    "limit",
    "offset",
    "page",
    "pageindex",
    "pagenumber",
    "pagesize",
    "perpage",
    "skip",
    "start",
})
_REQUEST_NAVIGATION_KEYS = frozenset({
    *_NEXT_TOKEN_KEYS,
    "before",
    "cursor",
    "offset",
    "page",
    "pageindex",
    "pagenumber",
    "skip",
    "start",
})
_REQUEST_PAGE_WINDOW_KEYS = frozenset({
    "first",
    "last",
    "limit",
    "pagesize",
    "perpage",
})

# These names describe common wire-format collection envelopes.  They are
# deliberately protocol-level rather than domain-level: an unknown collection
# is still accepted when it is the response's only array, while multiple
# unknown arrays fail safe as ambiguous.  Diagnostic arrays are never promoted
# merely because they are the only array in an otherwise scalar response.
_PRIMARY_COLLECTION_KEYS = frozenset({
    "data",
    "documents",
    "edges",
    "elements",
    "entities",
    "entries",
    "hits",
    "items",
    "members",
    "nodes",
    "objects",
    "records",
    "resources",
    "results",
    "rows",
    "values",
})
_AUXILIARY_COLLECTION_KEYS = frozenset({
    "errors",
    "facets",
    "messages",
    "notices",
    "warnings",
})
# A number is source-declared collection cardinality only when its field name
# explicitly says so.  Bare ``count`` is intentionally excluded because it is
# frequently a page, bucket, or aggregation count rather than a result total.
_SOURCE_TOTAL_KEYS = frozenset({
    "grandtotal",
    "numbermatched",
    "overallcount",
    "total",
    "totalcount",
    "totalelements",
    "totalentries",
    "totalitems",
    "totalrecords",
    "totalresults",
    "totalrows",
})
_TOTAL_METADATA_CONTAINER_KEYS = frozenset({
    "meta",
    "metadata",
    "pageinfo",
    "pagination",
    "summary",
})


def retrieval_receipt_affects_completion_quality(value: Any) -> bool:
    """Whether a persisted unresolved-retrieval receipt degrades completion.

    Only a producer-classified bounded optional frontier is advisory. Legacy
    receipts without ``quality_impact``, malformed projections, and unknown
    future values remain fail-closed as quality-degrading.
    """

    if value is None or value is False:
        return False
    return not (
        isinstance(value, Mapping)
        and value.get("quality_impact")
        == RETRIEVAL_QUALITY_IMPACT_ADVISORY
    )


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _has_dynamic_navigation_suffix(value: Any, prefix: str) -> bool:
    """Match branch-qualified cursor variables without broad prefix guesses.

    GraphQL commonly names independent cursor variables ``afterA`` or
    ``after_a``.  Treating every key beginning with ``page``/``start``/etc. as
    pagination would also erase ordinary filters such as ``pageTitle`` and
    ``startDate`` from the request family.  Preserve only the narrow cursor
    prefixes whose suffix is visibly qualified.
    """

    rendered = str(value or "").strip()
    if len(rendered) <= len(prefix):
        return False
    if rendered[:len(prefix)].casefold() != prefix.casefold():
        return False
    suffix = rendered[len(prefix):]
    return bool(
        suffix
        and (
            suffix[0] in "_-"
            or suffix[0].isupper()
            or suffix[0].isdigit()
        )
    )


def _is_request_page_key(value: Any) -> bool:
    key = _normalized_key(value)
    return bool(
        key in _REQUEST_PAGE_KEYS
        or any(
            _has_dynamic_navigation_suffix(value, prefix)
            for prefix in ("after", "before", "continuation", "cursor")
        )
    )


def _is_request_navigation_key(value: Any) -> bool:
    key = _normalized_key(value)
    return bool(
        key in _REQUEST_NAVIGATION_KEYS
        or any(
            _has_dynamic_navigation_suffix(value, prefix)
            for prefix in ("after", "before", "continuation", "cursor")
        )
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_pointer(parts: tuple[str, ...]) -> tuple[str, str]:
    """Return a bounded display pointer and the hash of its exact form."""

    exact = "$" + "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1")
        for part in parts
    )
    digest = _sha256_text(exact)
    if len(exact) <= MAX_COLLECTION_PATH_CHARS:
        return exact, digest
    suffix = "...#" + digest[:16]
    return exact[:MAX_COLLECTION_PATH_CHARS - len(suffix)] + suffix, digest


def _canonical_json_sha256(value: Any) -> str:
    """Hash parsed JSON deterministically without retaining its values."""

    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        # ``json.loads`` can accept non-standard NaN values.  They remain safe
        # to hash, but are not re-emitted in any receipt.
        try:
            rendered = repr(value)
        except (TypeError, ValueError, OverflowError, RecursionError):
            rendered = f"<bounded-unrenderable:{type(value).__name__}>"
    return _sha256_text(rendered)


def _explicit_nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and value.is_integer():
        parsed = int(value)
    else:
        return None
    if parsed < 0 or parsed > 9_007_199_254_740_991:
        return None
    return parsed


def _mapping_has_pagination_signal(value: Mapping[str, Any]) -> bool:
    for raw_key, child in value.items():
        key = _normalized_key(raw_key)
        if (
            key in _HAS_MORE_KEYS
            or key in _NEXT_URL_KEYS
            or key in _NEXT_TOKEN_KEYS
            or key in _GENERIC_NEXT_KEYS
        ):
            if isinstance(child, bool) or child is None:
                return True
            if _bounded_hint_value(child) is not None or isinstance(child, dict):
                return True
    return False


def _source_total_evidence(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Find an explicitly numeric total associated with one collection.

    The nearest scope containing an explicit numeric total wins.  An ancestor
    scope is eligible only when the path down to the selected collection is its
    sole non-metadata/non-diagnostic structured branch.  This permits common
    ``{totalCount, data: {records: [...]}}`` envelopes without borrowing an
    unrelated root total from a response that contains sibling structures.
    """

    parent_chain = candidate.get("parent_chain")
    if not isinstance(parent_chain, tuple):
        return {"status": "absent"}
    candidate_parts = candidate.get("parts")
    if not isinstance(candidate_parts, tuple):
        return {"status": "absent"}
    for chain_index in range(len(parent_chain) - 1, -1, -1):
        entry = parent_chain[chain_index]
        if not (
            isinstance(entry, tuple)
            and len(entry) == 2
            and isinstance(entry[0], tuple)
            and isinstance(entry[1], Mapping)
        ):
            continue
        parts, mapping = entry
        observations: list[dict[str, Any]] = []
        containers: list[tuple[tuple[str, ...], Mapping[str, Any]]] = [
            (parts, mapping)
        ]
        for raw_key, child in mapping.items():
            if (
                _normalized_key(raw_key) in _TOTAL_METADATA_CONTAINER_KEYS
                and isinstance(child, Mapping)
            ):
                containers.append((parts + (str(raw_key),), child))
        for container_parts, container in containers:
            for raw_key, raw_value in container.items():
                if _normalized_key(raw_key) not in _SOURCE_TOTAL_KEYS:
                    continue
                total = _explicit_nonnegative_integer(raw_value)
                if total is None:
                    continue
                path, path_sha = _json_pointer(
                    container_parts + (str(raw_key),)
                )
                observations.append({
                    "value": total,
                    "path": path,
                    "path_sha256": path_sha,
                })
        if not observations:
            continue

        # The direct collection container is intrinsically associated. At any
        # higher scope, reject the total if another structured sibling could
        # plausibly own it. Metadata and diagnostic branches are non-primary.
        is_direct_parent = chain_index == len(parent_chain) - 1
        if not is_direct_parent:
            next_parts = (
                parent_chain[chain_index + 1][0]
                if chain_index + 1 < len(parent_chain)
                and isinstance(parent_chain[chain_index + 1], tuple)
                else candidate_parts
            )
            branch_key = (
                str(next_parts[len(parts)])
                if isinstance(next_parts, tuple)
                and len(next_parts) > len(parts)
                else ""
            )
            ambiguous_siblings = 0
            for raw_key, child in mapping.items():
                key = _normalized_key(raw_key)
                if str(raw_key) == branch_key:
                    continue
                if (
                    key in _TOTAL_METADATA_CONTAINER_KEYS
                    or key in _AUXILIARY_COLLECTION_KEYS
                ):
                    continue
                if isinstance(child, (Mapping, list)):
                    ambiguous_siblings += 1
            if ambiguous_siblings:
                return {
                    "status": "ambiguous_scope",
                    "explicit_numeric_field_count": len(observations),
                    "structured_sibling_count": ambiguous_siblings,
                }

        distinct = {int(item["value"]) for item in observations}
        if len(distinct) != 1:
            return {
                "status": "conflict",
                "explicit_numeric_field_count": len(observations),
                "distinct_value_count": len(distinct),
            }
        chosen = min(
            observations,
            key=lambda item: str(item.get("path_sha256") or ""),
        )
        return {
            "status": "observed",
            "value": int(chosen["value"]),
            "path": str(chosen["path"]),
            "path_sha256": str(chosen["path_sha256"]),
        }
    return {"status": "absent"}


def _collection_evidence(response_body: str | None) -> dict[str, Any]:
    """Describe one response's primary JSON collection without raw records.

    Selection is intentionally conservative.  A root array or the sole
    non-diagnostic array is unambiguous.  With multiple arrays, only a unique
    protocol-level collection candidate wins; ties and bounded scans produce
    no count.  Arrays are never traversed, preventing record-local arrays from
    being mistaken for response pagination.
    """

    base: dict[str, Any] = {"version": EVIDENCE_LEDGER_VERSION}
    if response_body is None:
        return {**base, "status": "unavailable_partial_wire"}
    try:
        parsed = json.loads(response_body)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return {**base, "status": "not_json"}

    if isinstance(parsed, list):
        path, path_sha = _json_pointer(())
        collection_sha = _canonical_json_sha256(parsed)
        observation_sha = _sha256_text(
            f"{path_sha}\0{collection_sha}\0{len(parsed)}"
        )
        return {
            **base,
            "status": "observed",
            "candidate_count": 1,
            "primary_collection": {
                "path": path,
                "path_sha256": path_sha,
                "observed_items": len(parsed),
                "collection_sha256": collection_sha,
                "page_observation_sha256": observation_sha,
            },
            "source_declared_total": {"status": "absent"},
        }
    if not isinstance(parsed, Mapping):
        return {**base, "status": "no_collection", "candidate_count": 0}

    candidates: list[dict[str, Any]] = []
    nodes = 0
    scan_bounded = False
    root_entry = ((), parsed)
    stack: list[
        tuple[Mapping[str, Any], tuple[str, ...], int, tuple[Any, ...]]
    ] = [(parsed, (), 0, (root_entry,))]
    while stack:
        current, parts, depth, parent_chain = stack.pop()
        nodes += 1
        if nodes > MAX_PAGINATION_SCAN_NODES:
            scan_bounded = True
            break
        parent_has_pagination = _mapping_has_pagination_signal(current)
        for raw_key, child in current.items():
            nodes += 1
            if nodes > MAX_PAGINATION_SCAN_NODES:
                scan_bounded = True
                break
            child_parts = parts + (str(raw_key),)
            key = _normalized_key(raw_key)
            if isinstance(child, list):
                if len(candidates) >= MAX_COLLECTION_CANDIDATES:
                    scan_bounded = True
                    continue
                score = 0
                if parent_has_pagination:
                    score += 100
                if key in _PRIMARY_COLLECTION_KEYS:
                    score += 40
                if depth == 0:
                    score += 10
                if key in _AUXILIARY_COLLECTION_KEYS:
                    score -= 200
                candidates.append({
                    "value": child,
                    "parts": child_parts,
                    "key": key,
                    "score": score,
                    "parent_chain": parent_chain,
                })
            elif isinstance(child, Mapping):
                if depth < MAX_PAGINATION_SCAN_DEPTH:
                    stack.append((
                        child,
                        child_parts,
                        depth + 1,
                        parent_chain + ((child_parts, child),),
                    ))
                elif child:
                    scan_bounded = True

    non_auxiliary = [
        item for item in candidates
        if str(item.get("key") or "") not in _AUXILIARY_COLLECTION_KEYS
    ]
    selected: dict[str, Any] | None = None
    if not scan_bounded and len(non_auxiliary) == 1:
        selected = non_auxiliary[0]
    elif not scan_bounded and len(non_auxiliary) > 1:
        best_score = max(int(item.get("score") or 0) for item in non_auxiliary)
        best = [
            item for item in non_auxiliary
            if int(item.get("score") or 0) == best_score
        ]
        # Multiple unknown/direct arrays have no principled primary collection.
        # A unique positive protocol signal is required to break that tie.
        if len(best) == 1 and best_score > 0:
            selected = best[0]

    if scan_bounded:
        return {
            **base,
            "status": "scan_bounded",
            "candidate_count": len(candidates),
        }
    if selected is None:
        return {
            **base,
            "status": (
                "no_collection" if not non_auxiliary else "ambiguous"
            ),
            "candidate_count": len(candidates),
            "eligible_candidate_count": len(non_auxiliary),
        }

    collection = selected["value"]
    path, path_sha = _json_pointer(selected["parts"])
    collection_sha = _canonical_json_sha256(collection)
    observation_sha = _sha256_text(
        f"{path_sha}\0{collection_sha}\0{len(collection)}"
    )
    return {
        **base,
        "status": "observed",
        "candidate_count": len(candidates),
        "primary_collection": {
            "path": path,
            "path_sha256": path_sha,
            "observed_items": len(collection),
            "collection_sha256": collection_sha,
            "page_observation_sha256": observation_sha,
        },
        "source_declared_total": _source_total_evidence(selected),
    }


def _bounded_hint_value(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return None
    rendered = str(value).strip()
    if not rendered or len(rendered) > MAX_PAGINATION_HINT_CHARS:
        return None
    return rendered


def _canonical_next_url(value: Any, request_url: str) -> str | None:
    rendered = _bounded_hint_value(value)
    if rendered is None:
        return None
    # A generic JSON ``next`` scalar is frequently a page number or cursor.
    # Treat only syntactically URL-like references as URLs; otherwise the
    # continuation is matched against page/cursor request parameters.
    if not (
        rendered.startswith(("https://", "/", "./", "../", "?"))
    ):
        return None
    return canonical_https_request_url(urljoin(request_url, rendered))


def _append_hint(
    hints: list[dict[str, Any]],
    *,
    kind: str,
    path: str,
    value: str,
) -> str:
    digest = _sha256_text(value)
    if any(
        item.get("kind") == kind and item.get("sha256") == digest
        for item in hints
    ):
        return "duplicate"
    if len(hints) >= MAX_PAGINATION_HINTS:
        return "overflow"
    hints.append({
        "kind": kind,
        "path": path[:256],
        "value": value,
        "sha256": digest,
    })
    return "added"


def _pagination_signals(
    response_body: str,
    request_url: str,
) -> dict[str, Any]:
    """Extract bounded, explicit JSON pagination signals.

    Arrays are deliberately not traversed: cursor-looking fields in individual
    records are not collection pagination.  Nested mapping envelopes (including
    GraphQL ``data.*.pageInfo``) are scanned under strict depth/node bounds.
    """

    try:
        parsed = json.loads(response_body)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return {
            "json_parsed": False,
            "pagination_detected": False,
            "has_more": None,
            "explicit_end": False,
            "signals_conflict": False,
            "next_hints": [],
        }
    if not isinstance(parsed, dict):
        return {
            "json_parsed": True,
            "pagination_detected": False,
            "has_more": None,
            "explicit_end": False,
            "signals_conflict": False,
            "next_hints": [],
        }

    hints: list[dict[str, Any]] = []
    has_more_values: list[bool] = []
    explicit_end = False
    hint_overflow_count = 0
    scan_bounded = False
    nodes = 0
    stack: list[tuple[Mapping[str, Any], str, int]] = [(parsed, "$", 0)]
    while stack:
        current, prefix, depth = stack.pop()
        nodes += 1
        if nodes > MAX_PAGINATION_SCAN_NODES:
            scan_bounded = True
            break
        for raw_key, value in current.items():
            nodes += 1
            if nodes > MAX_PAGINATION_SCAN_NODES:
                scan_bounded = True
                break
            key = _normalized_key(raw_key)
            path = f"{prefix}.{raw_key}"
            if key in _HAS_MORE_KEYS and isinstance(value, bool):
                has_more_values.append(value)

            if key in _NEXT_URL_KEYS:
                next_url = _canonical_next_url(value, request_url)
                if next_url is not None:
                    outcome = _append_hint(
                        hints, kind="url", path=path, value=next_url
                    )
                    hint_overflow_count += int(outcome == "overflow")
                elif value in (None, ""):
                    explicit_end = True
            elif key in _NEXT_TOKEN_KEYS:
                rendered = _bounded_hint_value(value)
                if rendered is not None:
                    outcome = _append_hint(
                        hints, kind="cursor", path=path, value=rendered
                    )
                    hint_overflow_count += int(outcome == "overflow")
                elif value in (None, ""):
                    explicit_end = True
            elif key in _GENERIC_NEXT_KEYS:
                candidate = value
                if isinstance(candidate, dict):
                    candidate = (
                        candidate.get("href")
                        or candidate.get("url")
                        or candidate.get("uri")
                    )
                next_url = _canonical_next_url(candidate, request_url)
                if next_url is not None:
                    outcome = _append_hint(
                        hints, kind="url", path=path, value=next_url
                    )
                    hint_overflow_count += int(outcome == "overflow")
                else:
                    rendered = _bounded_hint_value(candidate)
                    if rendered is not None:
                        outcome = _append_hint(
                            hints,
                            kind="cursor",
                            path=path,
                            value=rendered,
                        )
                        hint_overflow_count += int(outcome == "overflow")
                    elif candidate in (None, ""):
                        explicit_end = True

            if (
                depth < MAX_PAGINATION_SCAN_DEPTH
                and isinstance(value, dict)
            ):
                stack.append((value, path, depth + 1))
            elif (
                depth >= MAX_PAGINATION_SCAN_DEPTH
                and isinstance(value, dict)
                and value
            ):
                scan_bounded = True

    has_more: bool | None = None
    if any(has_more_values):
        has_more = True
    elif has_more_values:
        has_more = False
    if hints:
        has_more = True
    signals_conflict = bool(hints and False in has_more_values)
    return {
        "json_parsed": True,
        "pagination_detected": bool(
            hints or has_more_values or explicit_end
        ),
        "has_more": has_more,
        "explicit_end": bool(
            explicit_end or (has_more is False and not hints)
        ),
        "signals_conflict": signals_conflict,
        "next_hints": hints,
        "frontier_truncated": hint_overflow_count > 0,
        "hint_overflow_count": hint_overflow_count,
        "scan_bounded": scan_bounded,
    }


def _request_cursor_hashes(
    method: str,
    request_url: str,
    request_body: Mapping[str, Any] | None,
) -> list[str]:
    values: list[str] = []
    for key, value in parse_qsl(
        urlsplit(request_url).query,
        keep_blank_values=True,
        max_num_fields=256,
    ):
        if _is_request_navigation_key(key) and value:
            values.append(value)

    if method == "POST" and isinstance(request_body, Mapping):
        nodes = 0
        stack: list[tuple[Mapping[str, Any], int]] = [(request_body, 0)]
        while stack:
            current, depth = stack.pop()
            nodes += 1
            if nodes > MAX_PAGINATION_SCAN_NODES:
                break
            for key, value in current.items():
                if _is_request_navigation_key(key):
                    rendered = _bounded_hint_value(value)
                    if rendered is not None:
                        values.append(rendered)
                if (
                    depth < MAX_PAGINATION_SCAN_DEPTH
                    and isinstance(value, dict)
                ):
                    stack.append((value, depth + 1))
    return sorted({_sha256_text(value) for value in values})


def _normalized_post_family_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_PAGINATION_SCAN_DEPTH:
        return "<bounded-depth>"
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, child in sorted(value.items(), key=lambda item: str(item[0])):
            if _is_request_page_key(key):
                normalized[str(key)] = "<pagination>"
            else:
                normalized[str(key)] = _normalized_post_family_value(
                    child, depth=depth + 1
                )
        return normalized
    if isinstance(value, list):
        return [
            _normalized_post_family_value(item, depth=depth + 1)
            for item in value[:256]
        ]
    return value


def _retrieval_family_sha256(
    method: str,
    request_url: str,
    request_body: Mapping[str, Any] | None,
) -> str:
    parsed = urlsplit(request_url)
    stable_query = sorted(
        (key, value)
        for key, value in parse_qsl(
            parsed.query,
            keep_blank_values=True,
            max_num_fields=256,
        )
        if not _is_request_page_key(key)
    )
    stable_url = urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(stable_query, doseq=True),
        "",
    ))
    family: dict[str, Any] = {"method": method, "url": stable_url}
    if method == "POST" and isinstance(request_body, Mapping):
        family["body"] = _normalized_post_family_value(request_body)
    encoded = json.dumps(
        family,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return _sha256_text(encoded)


def _request_identity_sha256(
    method: str,
    request_url: str,
    request_body: Mapping[str, Any] | None,
) -> str:
    identity: dict[str, Any] = {
        "method": method,
        "url": request_url,
    }
    if method == "POST":
        identity["body"] = request_body
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(encoded)


def _positive_page_window(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    if not 1 <= parsed <= 1_000_000_000:
        return None
    return parsed


def _smaller_page_window(
    current: int,
    *,
    response_char_limit: int,
    response_chars_read: int,
) -> int | None:
    """Return one conservative, strictly smaller server page window.

    The response size is only a sizing signal; it never changes the retrieval
    family or grants a request.  A 20 percent safety margin avoids repeatedly
    landing exactly on the local presentation bound.  When the wire response
    itself hit its byte cap, ``response_chars_read`` is a lower bound and the
    same formula therefore remains conservative rather than optimistic.
    """

    if current <= 1:
        return None
    limit = max(1, int(response_char_limit or 1))
    observed = max(limit + 1, int(response_chars_read or 0))
    scaled = (current * limit * 4) // (observed * 5)
    return max(1, min(current - 1, scaled or 1))


def _query_page_window_candidates(
    request_url: str,
) -> list[dict[str, Any]]:
    parsed = urlsplit(request_url)
    try:
        items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            max_num_fields=256,
        )
    except ValueError:
        return []
    candidates: list[dict[str, Any]] = []
    for index, (key, value) in enumerate(items):
        if _normalized_key(key) not in _REQUEST_PAGE_WINDOW_KEYS:
            continue
        parsed_value = _positive_page_window(value)
        if parsed_value is None:
            continue
        candidates.append({
            "location": "query",
            "index": index,
            "key": key,
            "value": parsed_value,
            "items": items,
        })
    return candidates


def _post_page_window_candidates(
    request_body: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(request_body, Mapping):
        return []
    candidates: list[dict[str, Any]] = []
    nodes = 0
    stack: list[tuple[Mapping[str, Any], tuple[str, ...], int]] = [
        (request_body, (), 0)
    ]
    while stack:
        current, prefix, depth = stack.pop()
        nodes += 1
        if nodes > MAX_PAGINATION_SCAN_NODES:
            return []
        for raw_key, value in current.items():
            nodes += 1
            if nodes > MAX_PAGINATION_SCAN_NODES:
                return []
            key = str(raw_key)
            path = (*prefix, key)
            if _normalized_key(key) in _REQUEST_PAGE_WINDOW_KEYS:
                parsed_value = _positive_page_window(value)
                if parsed_value is not None:
                    candidates.append({
                        "location": "body",
                        "path": path,
                        "key": key,
                        "value": parsed_value,
                        "string_value": isinstance(value, str),
                    })
            if depth < MAX_PAGINATION_SCAN_DEPTH and isinstance(value, dict):
                stack.append((value, path, depth + 1))
            elif depth >= MAX_PAGINATION_SCAN_DEPTH and isinstance(value, dict):
                return []
    return candidates


def _replace_query_page_window(
    request_url: str,
    candidate: Mapping[str, Any],
    replacement: int,
) -> str | None:
    parsed = urlsplit(request_url)
    items = list(candidate.get("items") or [])
    try:
        index = int(candidate.get("index"))
        key, _old_value = items[index]
        items[index] = (key, str(replacement))
    except (IndexError, TypeError, ValueError):
        return None
    return canonical_https_request_url(urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(items, doseq=True),
        "",
    )))


def _replace_post_page_window(
    request_body: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
    replacement: int,
) -> dict[str, Any] | None:
    if not isinstance(request_body, Mapping):
        return None
    try:
        cloned = copy.deepcopy(dict(request_body))
    except (TypeError, ValueError, RecursionError):
        return None
    cursor: Any = cloned
    path = tuple(str(item) for item in (candidate.get("path") or ()))
    if not path:
        return None
    try:
        for key in path[:-1]:
            cursor = cursor[key]
        cursor[path[-1]] = (
            str(replacement)
            if candidate.get("string_value") is True
            else replacement
        )
    except (KeyError, TypeError):
        return None
    return cloned


def _request_navigation_candidates(
    method: str,
    request_url: str,
    request_body: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if method == "GET":
        parsed = urlsplit(request_url)
        try:
            items = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                max_num_fields=256,
            )
        except ValueError:
            return []
        return [
            {
                "location": "query",
                "index": index,
                "key": key,
                "items": items,
            }
            for index, (key, _value) in enumerate(items)
            if _is_request_navigation_key(key)
        ]

    if method != "POST" or not isinstance(request_body, Mapping):
        return []
    candidates: list[dict[str, Any]] = []
    nodes = 0
    stack: list[tuple[Mapping[str, Any], tuple[str, ...], int]] = [
        (request_body, (), 0)
    ]
    while stack:
        current, prefix, depth = stack.pop()
        nodes += 1
        if nodes > MAX_PAGINATION_SCAN_NODES:
            return []
        for raw_key, value in current.items():
            nodes += 1
            if nodes > MAX_PAGINATION_SCAN_NODES:
                return []
            key = str(raw_key)
            path = (*prefix, key)
            if _is_request_navigation_key(key):
                candidates.append({
                    "location": "body",
                    "path": path,
                    "key": key,
                    "string_value": isinstance(value, str),
                })
            if depth < MAX_PAGINATION_SCAN_DEPTH and isinstance(value, dict):
                stack.append((value, path, depth + 1))
            elif depth >= MAX_PAGINATION_SCAN_DEPTH and isinstance(value, dict):
                return []
    return candidates


def _derived_cursor_query_key(hint: Mapping[str, Any]) -> str | None:
    path = str(hint.get("path") or "")
    raw_key = path.rsplit(".", 1)[-1].strip()
    if not raw_key.casefold().startswith("next") or len(raw_key) <= 4:
        return None
    suffix = raw_key[4:]
    candidate = suffix[:1].lower() + suffix[1:]
    return candidate if _is_request_navigation_key(candidate) else None


def _cursor_followup_request(
    method: str,
    request_url: str,
    request_body: Mapping[str, Any] | None,
    hint: Mapping[str, Any],
) -> tuple[str, dict[str, Any] | None, dict[str, Any]] | None:
    value = _bounded_hint_value(hint.get("value"))
    if value is None:
        return None
    candidates = _request_navigation_candidates(
        method, request_url, request_body
    )
    if method == "GET":
        parsed = urlsplit(request_url)
        derived_key = _derived_cursor_query_key(hint)
        matching_candidates = [
            candidate for candidate in candidates
            if derived_key is not None
            and _normalized_key(candidate.get("key"))
            == _normalized_key(derived_key)
        ]
        if len(matching_candidates) == 1:
            candidate = matching_candidates[0]
            items = list(candidate.get("items") or [])
            index = int(candidate.get("index"))
            key = str(candidate.get("key") or "")
            items[index] = (key, value)
        elif derived_key is not None and not matching_candidates:
            key = derived_key
            try:
                items = parse_qsl(
                    parsed.query,
                    keep_blank_values=True,
                    max_num_fields=256,
                )
            except ValueError:
                return None
            items.append((key, value))
        elif len(candidates) == 1:
            candidate = candidates[0]
            items = list(candidate.get("items") or [])
            index = int(candidate.get("index"))
            key = str(candidate.get("key") or "")
            items[index] = (key, value)
        else:
            return None
        next_url = canonical_https_request_url(urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(items, doseq=True),
            "",
        )))
        if next_url is None:
            return None
        return next_url, None, {
            "location": "query",
            "key": key,
            "cursor_sha256": str(hint.get("sha256") or ""),
        }

    if method == "POST" and len(candidates) == 1:
        candidate = candidates[0]
        try:
            cloned = copy.deepcopy(dict(request_body or {}))
            cursor: Any = cloned
            path = tuple(str(item) for item in candidate.get("path") or ())
            for key in path[:-1]:
                cursor = cursor[key]
            cursor[path[-1]] = value
        except (KeyError, TypeError, ValueError, RecursionError):
            return None
        return request_url, cloned, {
            "location": "body",
            "path": ".".join(path),
            "cursor_sha256": str(hint.get("sha256") or ""),
        }
    return None


def _action_args(
    method: str,
    request_url: str,
    request_body: Mapping[str, Any] | None,
    *,
    max_chars: int,
    timeout: int | None,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "url": request_url,
        "max_chars": max(1, int(max_chars)),
    }
    if timeout is not None:
        args["timeout"] = max(1, int(timeout))
    if method == "POST" and isinstance(request_body, Mapping):
        args["body"] = copy.deepcopy(dict(request_body))
    return args


def _build_continuation_action(
    *,
    method: str,
    request_url: str,
    request_body: Mapping[str, Any] | None,
    request_timeout: int | None,
    body_truncated: bool,
    wire_body_complete: bool,
    response_chars_read: int,
    response_char_limit: int,
    response_char_hard_limit: int | None,
    pagination: Mapping[str, Any],
) -> dict[str, Any] | None:
    tool_name = (
        "skill_http_post_json" if method == "POST" else "skill_http_get"
    )
    if body_truncated and response_char_hard_limit is None:
        # Legacy/custom receipt builders did not expose their schema ceiling.
        # Preserve their existing tracker behavior instead of inventing a
        # terminal or page-window rewrite without a proven bound.
        return None
    hard_limit = max(
        response_char_limit,
        int(response_char_hard_limit or response_char_limit),
    )

    if body_truncated:
        # A byte-capped wire response cannot be repaired by increasing only the
        # model-visible character bound.  A presentation-only truncation can.
        if wire_body_complete and response_char_limit < hard_limit:
            target = min(
                hard_limit,
                max(
                    response_char_limit + 1,
                    response_chars_read,
                    response_char_limit * 2,
                ),
            )
            args = _action_args(
                method,
                request_url,
                request_body,
                max_chars=target,
                timeout=request_timeout,
            )
            return {
                "version": CONTINUATION_ACTION_VERSION,
                "kind": "retry_with_larger_visible_limit",
                "tool_name": tool_name,
                "args": args,
                "reason": "visible_body_truncated_below_schema_max",
                "expected_request_identity_sha256": (
                    _request_identity_sha256(method, request_url, request_body)
                ),
            }

        candidates = (
            _query_page_window_candidates(request_url)
            if method == "GET"
            else _post_page_window_candidates(request_body)
        )
        if len(candidates) == 1:
            candidate = candidates[0]
            current = int(candidate.get("value") or 0)
            replacement = _smaller_page_window(
                current,
                response_char_limit=hard_limit,
                response_chars_read=response_chars_read,
            )
            next_url = request_url
            next_body = (
                dict(request_body) if isinstance(request_body, Mapping) else None
            )
            if replacement is not None and method == "GET":
                next_url = _replace_query_page_window(
                    request_url, candidate, replacement
                ) or ""
            elif replacement is not None and method == "POST":
                next_body = _replace_post_page_window(
                    request_body, candidate, replacement
                )
            if (
                replacement is not None
                and next_url
                and (method != "POST" or next_body is not None)
            ):
                args = _action_args(
                    method,
                    next_url,
                    next_body,
                    max_chars=hard_limit,
                    timeout=request_timeout,
                )
                return {
                    "version": CONTINUATION_ACTION_VERSION,
                    "kind": "restart_with_smaller_page",
                    "tool_name": tool_name,
                    "args": args,
                    "reason": (
                        "wire_body_byte_limit"
                        if not wire_body_complete
                        else "visible_body_truncated_at_schema_max"
                    ),
                    "page_window": {
                        "location": str(candidate.get("location") or ""),
                        "key": str(candidate.get("key") or ""),
                        "path": ".".join(
                            str(item) for item in candidate.get("path") or ()
                        ),
                        "from": current,
                        "to": replacement,
                    },
                    "expected_request_identity_sha256": (
                        _request_identity_sha256(method, next_url, next_body)
                    ),
                }

        return {
            "version": CONTINUATION_ACTION_VERSION,
            "kind": "degrade",
            "tool_name": tool_name,
            "reason": (
                "response_exceeds_wire_byte_limit_no_safe_page_window"
                if not wire_body_complete
                else "response_exceeds_visible_limit_no_safe_page_window"
            ),
        }

    hints = [
        item for item in (pagination.get("next_hints") or [])
        if isinstance(item, Mapping)
    ]
    if len(hints) != 1:
        return None
    hint = hints[0]
    if hint.get("kind") == "url" and method == "GET":
        next_url = canonical_https_request_url(str(hint.get("value") or ""))
        if next_url is None:
            return None
        args = _action_args(
            method,
            next_url,
            None,
            max_chars=response_char_limit,
            timeout=request_timeout,
        )
        return {
            "version": CONTINUATION_ACTION_VERSION,
            "kind": "follow_next_url",
            "tool_name": tool_name,
            "args": args,
            "reason": "explicit_next_url",
            "cursor": {"url_sha256": str(hint.get("sha256") or "")},
            "expected_request_identity_sha256": (
                _request_identity_sha256(method, next_url, None)
            ),
        }
    if hint.get("kind") != "cursor":
        return None
    followup = _cursor_followup_request(
        method, request_url, request_body, hint
    )
    if followup is None:
        return None
    next_url, next_body, cursor_record = followup
    args = _action_args(
        method,
        next_url,
        next_body,
        max_chars=response_char_limit,
        timeout=request_timeout,
    )
    return {
        "version": CONTINUATION_ACTION_VERSION,
        "kind": "follow_pagination_cursor",
        "tool_name": tool_name,
        "args": args,
        "reason": "explicit_pagination_cursor",
        "cursor": cursor_record,
        "expected_request_identity_sha256": (
            _request_identity_sha256(method, next_url, next_body)
        ),
    }


def build_http_retrieval_receipt(
    *,
    method: str,
    request_url: str,
    request_body: Mapping[str, Any] | None,
    response_body: str,
    body_truncated: bool,
    body_spilled_complete: bool = False,
    response_bytes_read: int,
    response_byte_limit: int,
    response_chars_returned: int,
    response_char_limit: int,
    response_char_hard_limit: int | None = None,
    response_chars_read: int | None = None,
    wire_body_complete: bool | None = None,
    pagination_scan_body: str | None = None,
    request_timeout: int | None = None,
    request_number: int,
    request_run_hop_limit: int,
    request_elapsed_ms: int,
) -> dict[str, Any]:
    method = str(method or "GET").upper()
    visible_body_complete = not bool(body_truncated)
    if wire_body_complete is None:
        # Backward-compatible callers that provide only the model-visible body
        # cannot prove that a truncated payload was completely read.
        wire_body_complete = visible_body_complete
    wire_body_complete = bool(wire_body_complete)
    body_spilled_complete = bool(
        body_spilled_complete and wire_body_complete
    )
    body_retrievable_complete = bool(
        visible_body_complete or body_spilled_complete
    )
    scan_body = (
        pagination_scan_body
        if wire_body_complete and isinstance(pagination_scan_body, str)
        else (response_body if wire_body_complete else None)
    )
    if scan_body is not None:
        signals = _pagination_signals(scan_body, request_url)
        collection_evidence = _collection_evidence(scan_body)
        pagination_scan_source = (
            "visible_body"
            if visible_body_complete and scan_body == response_body
            else "complete_wire_body"
        )
    else:
        signals = {
            "json_parsed": False,
            "pagination_detected": False,
            "has_more": None,
            "explicit_end": False,
            "signals_conflict": False,
            "next_hints": [],
        }
        collection_evidence = _collection_evidence(None)
        pagination_scan_source = "none_partial_wire"
    chars_read = max(
        len(response_body),
        int(
            response_chars_read
            if response_chars_read is not None
            else len(scan_body or response_body)
        ),
    )
    reasons: list[str] = []
    if body_truncated and not body_spilled_complete:
        reasons.append("body_truncated")
    if signals.get("has_more") is True:
        reasons.append("pagination_more_available")
    if signals.get("signals_conflict") is True:
        reasons.append("pagination_signal_conflict")
    if signals.get("frontier_truncated") is True:
        reasons.append("pagination_frontier_truncated")
    if signals.get("scan_bounded") is True:
        reasons.append("pagination_scan_bounded")

    next_hints = list(signals.get("next_hints") or [])
    request_url_sha256 = _sha256_text(request_url)
    request_identity_sha256 = _request_identity_sha256(
        method, request_url, request_body
    )
    family_sha256 = _retrieval_family_sha256(
        method, request_url, request_body
    )
    pagination_record = {
        "detected": bool(signals.get("pagination_detected")),
        "has_more": signals.get("has_more"),
        "explicit_end": bool(signals.get("explicit_end")),
        "signals_conflict": bool(signals.get("signals_conflict")),
        "frontier_truncated": bool(signals.get("frontier_truncated")),
        "hint_overflow_count": max(
            0, int(signals.get("hint_overflow_count") or 0)
        ),
        "scan_bounded": bool(signals.get("scan_bounded")),
        "scan_source": pagination_scan_source,
        "next_hints": next_hints,
    }
    continuation_action = _build_continuation_action(
        method=method,
        request_url=request_url,
        request_body=request_body,
        request_timeout=request_timeout,
        body_truncated=bool(
            body_truncated and not body_spilled_complete
        ),
        wire_body_complete=wire_body_complete,
        response_chars_read=chars_read,
        response_char_limit=max(1, int(response_char_limit)),
        response_char_hard_limit=response_char_hard_limit,
        pagination=pagination_record,
    )
    receipt_identity = json.dumps({
        "method": method,
        "request_identity_sha256": request_identity_sha256,
        "request_number": request_number,
        "family_sha256": family_sha256,
    }, sort_keys=True, separators=(",", ":"))
    return {
        "version": RETRIEVAL_RECEIPT_VERSION,
        "state": "incomplete" if reasons else "complete",
        "incomplete_reasons": reasons,
        "body_truncated": bool(body_truncated),
        "body_spilled_complete": body_spilled_complete,
        "body_retrievable_complete": body_retrievable_complete,
        "visible_body_complete": visible_body_complete,
        "wire_body_complete": wire_body_complete,
        "response_bytes_read": max(0, int(response_bytes_read)),
        "response_byte_limit": max(1, int(response_byte_limit)),
        "response_chars_read": chars_read,
        "response_chars_returned": max(0, int(response_chars_returned)),
        "response_char_limit": max(1, int(response_char_limit)),
        "response_char_hard_limit": max(
            1,
            int(response_char_hard_limit or response_char_limit),
        ),
        "request_number": max(0, int(request_number)),
        "request_run_hop_limit": max(1, int(request_run_hop_limit)),
        "request_elapsed_ms": max(0, int(request_elapsed_ms)),
        "request_method": method,
        "request_url_sha256": request_url_sha256,
        "request_identity_sha256": request_identity_sha256,
        "request_cursor_sha256s": _request_cursor_hashes(
            method, request_url, request_body
        ),
        "family_sha256": family_sha256,
        "pagination": pagination_record,
        "collection_evidence": collection_evidence,
        "continuation_action": continuation_action,
        "receipt_sha256": _sha256_text(receipt_identity),
    }


@dataclass
class _EvidenceFamilyLedger:
    family_sha256: str
    http_responses_observed: int = 0
    page_observations: dict[str, dict[str, Any]] = field(default_factory=dict)
    collection_paths: dict[str, str] = field(default_factory=dict)
    collection_paths_truncated: int = 0
    uncounted_statuses: dict[str, int] = field(default_factory=dict)
    deduplicated_page_observations: int = 0
    replaced_request_observations: int = 0
    source_totals: dict[int, dict[str, Any]] = field(default_factory=dict)
    source_total_conflicts: int = 0


@dataclass
class _OpenChain:
    family_sha256: str
    incomplete_reasons: tuple[str, ...]
    expected_cursor_sha256s: tuple[str, ...]
    expected_url_sha256s: tuple[str, ...]
    last_request_identity_sha256: str
    response_char_limit: int
    continuation_action: dict[str, Any] | None = None
    carried_cursor_sha256s: tuple[str, ...] = ()
    carried_url_sha256s: tuple[str, ...] = ()
    pages_observed: int = 1
    unlinked_attempts: int = 0
    # Monotonic receipt sequence at which this frontier last became ready.
    # Selection is a pure read: advancing a linked chain moves it behind other
    # ready chains, while repeated action inspection cannot perturb ordering.
    ready_sequence: int = 0


@dataclass
class RetrievalCompletenessTracker:
    """Track only delegated required-evidence HTTP chains.

    All limits are independent and fail closed. A normal, non-paginated
    response creates no open chain. A locally truncated response is superseded
    only by the exact same HTTP request identity under a larger/full local
    character bound, or by the exact machine-generated smaller-page request
    that starts an explicit replacement pagination chain. An explicit cursor
    frontier closes only after every expected cursor/next URL branch is
    consumed and ends; it never erases an unresolved truncated page.
    """

    max_pages_per_chain: int = 12
    max_total_response_bytes: int = 2_400_000
    max_total_request_elapsed_ms: int = 360_000
    max_total_requests: int = 16
    max_unlinked_attempts: int = 2
    _open: dict[str, _OpenChain] = field(default_factory=dict)
    _evidence: dict[str, _EvidenceFamilyLedger] = field(default_factory=dict)
    _seen_receipts: set[str] = field(default_factory=set)
    total_requests: int = 0
    total_response_bytes: int = 0
    total_request_elapsed_ms: int = 0
    _global_terminal_failure: str | None = None
    _terminal_chain_failures: dict[str, str] = field(default_factory=dict)

    @property
    def terminal_failure(self) -> str | None:
        """Return a run-terminal reason only when no family can advance."""

        if self._global_terminal_failure is not None:
            return self._global_terminal_failure
        runnable = set(self._open) - set(self._terminal_chain_failures)
        if self._open and not runnable:
            first_family = min(self._open)
            return self._terminal_chain_failures.get(first_family)
        return None

    def _set_terminal(
        self,
        reason: str,
        *,
        family: str | None = None,
        global_scope: bool = False,
    ) -> None:
        clean = str(reason or "retrieval_incomplete")
        if global_scope or not family:
            if self._global_terminal_failure is None:
                self._global_terminal_failure = clean
            return
        self._terminal_chain_failures.setdefault(family, clean)

    def mark_terminal(self, reason: str) -> None:
        """Close further retrieval while preserving the unresolved receipt."""

        self._set_terminal(
            str(reason or "retrieval_incomplete"),
            global_scope=True,
        )

    def _observe_collection_evidence(
        self,
        family: str,
        receipt: Mapping[str, Any],
    ) -> None:
        ledger = self._evidence.setdefault(
            family, _EvidenceFamilyLedger(family_sha256=family)
        )
        ledger.http_responses_observed += 1
        raw = receipt.get("collection_evidence")
        if not (
            isinstance(raw, Mapping)
            and raw.get("version") == EVIDENCE_LEDGER_VERSION
        ):
            status = "missing"
            ledger.uncounted_statuses[status] = (
                ledger.uncounted_statuses.get(status, 0) + 1
            )
            return
        status = str(raw.get("status") or "invalid")[:64]
        primary = raw.get("primary_collection")
        if status != "observed" or not isinstance(primary, Mapping):
            safe_status = (
                status
                if status in {
                    "ambiguous",
                    "no_collection",
                    "not_json",
                    "scan_bounded",
                    "unavailable_partial_wire",
                }
                else "invalid"
            )
            ledger.uncounted_statuses[safe_status] = (
                ledger.uncounted_statuses.get(safe_status, 0) + 1
            )
            return

        observed_items = primary.get("observed_items")
        request_identity = str(
            receipt.get("request_identity_sha256") or ""
        )
        page_observation_sha = str(
            primary.get("page_observation_sha256") or ""
        )
        collection_sha = str(primary.get("collection_sha256") or "")
        path_sha = str(primary.get("path_sha256") or "")
        path = str(primary.get("path") or "")
        valid_count = bool(
            isinstance(observed_items, int)
            and not isinstance(observed_items, bool)
            and 0 <= observed_items <= 10_000_000
        )
        valid_hashes = all(
            len(value) == 64
            for value in (
                request_identity,
                page_observation_sha,
                collection_sha,
                path_sha,
            )
        )
        if not valid_count or not valid_hashes or not path:
            ledger.uncounted_statuses["invalid"] = (
                ledger.uncounted_statuses.get("invalid", 0) + 1
            )
            return

        # One canonical request identity contributes at most one page count.
        # Exact retries are common after a model-visible truncation. If the
        # server returns changed content for that same request, replace the
        # prior observation and mark the conflict; never sum both responses.
        page_key = _sha256_text(f"{family}\0{request_identity}")
        prior_page = ledger.page_observations.get(page_key)
        page_record = {
            "observed_items": int(observed_items),
            "path_sha256": path_sha,
            "collection_sha256": collection_sha,
            "page_observation_sha256": page_observation_sha,
        }
        if prior_page is None:
            ledger.page_observations[page_key] = page_record
        elif (
            prior_page.get("page_observation_sha256")
            == page_observation_sha
        ):
            ledger.deduplicated_page_observations += 1
        else:
            ledger.page_observations[page_key] = page_record
            ledger.replaced_request_observations += 1
        if path_sha not in ledger.collection_paths:
            if len(ledger.collection_paths) < MAX_LEDGER_COLLECTION_PATHS:
                ledger.collection_paths[path_sha] = path[
                    :MAX_COLLECTION_PATH_CHARS
                ]
            else:
                ledger.collection_paths_truncated += 1

        source_total = raw.get("source_declared_total")
        if isinstance(source_total, Mapping):
            total_status = str(source_total.get("status") or "")
            if total_status == "observed":
                value = source_total.get("value")
                total_path = str(source_total.get("path") or "")
                total_path_sha = str(
                    source_total.get("path_sha256") or ""
                )
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 <= value <= 9_007_199_254_740_991
                    and total_path
                    and len(total_path_sha) == 64
                ):
                    ledger.source_totals.setdefault(int(value), {
                        "value": int(value),
                        "path": total_path[:MAX_COLLECTION_PATH_CHARS],
                        "path_sha256": total_path_sha,
                    })
                else:
                    ledger.source_total_conflicts += 1
            elif total_status in {"conflict", "ambiguous_scope"}:
                ledger.source_total_conflicts += 1

    def evidence_ledger(self) -> dict[str, Any]:
        """Return a bounded, raw-data-free quantitative evidence ledger."""

        families: list[dict[str, Any]] = []
        for family_sha, ledger in sorted(self._evidence.items()):
            items_observed = sum(
                int(item.get("observed_items") or 0)
                for item in ledger.page_observations.values()
            )
            total_values = sorted(ledger.source_totals)
            if (
                len(total_values) == 1
                and ledger.source_total_conflicts == 0
            ):
                source_total: dict[str, Any] = {
                    "status": "observed",
                    **ledger.source_totals[total_values[0]],
                }
            elif total_values or ledger.source_total_conflicts:
                source_total = {
                    "status": "conflict",
                    "distinct_explicit_value_count": len(total_values),
                    "conflicting_response_count": (
                        ledger.source_total_conflicts
                    ),
                }
            else:
                source_total = {"status": "absent"}
            observation_signatures = sorted(
                json.dumps(
                    {"page_key_sha256": page_key, **record},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for page_key, record in ledger.page_observations.items()
            )
            families.append({
                "family_sha256": family_sha,
                "http_responses_observed": ledger.http_responses_observed,
                "pages_observed": len(ledger.page_observations),
                "items_observed": items_observed,
                "uncounted_response_count": sum(
                    ledger.uncounted_statuses.values()
                ),
                "uncounted_statuses": dict(sorted(
                    ledger.uncounted_statuses.items()
                )),
                "deduplicated_page_observations": (
                    ledger.deduplicated_page_observations
                ),
                "replaced_request_observations": (
                    ledger.replaced_request_observations
                ),
                "collection_paths": [
                    {
                        "path": ledger.collection_paths[path_sha],
                        "path_sha256": path_sha,
                    }
                    for path_sha in sorted(ledger.collection_paths)
                ],
                "collection_paths_truncated": (
                    ledger.collection_paths_truncated
                ),
                "observations_sha256": _sha256_text(
                    "\n".join(observation_signatures)
                ),
                "source_declared_total": source_total,
            })
        return {
            "version": EVIDENCE_LEDGER_VERSION,
            "family_count": len(families),
            "families": families,
            "quantification_rules": {
                "items_observed_are_response_collection_slots": True,
                "items_observed_are_not_source_declared_total": True,
                "items_observed_are_not_proven_unique": True,
                "families_may_overlap_and_must_not_be_summed_as_unique": True,
                "same_request_retries_are_not_summed": True,
                "replaced_request_observations_require_qualification": True,
                "source_total_requires_explicit_numeric_field": True,
            },
        }

    def observe(self, receipt: Any) -> dict[str, Any] | None:
        if not isinstance(receipt, dict):
            return None
        if receipt.get("version") != RETRIEVAL_RECEIPT_VERSION:
            return None
        family = str(receipt.get("family_sha256") or "")
        receipt_sha = str(receipt.get("receipt_sha256") or "")
        state = str(receipt.get("state") or "")
        if (
            len(family) != 64
            or state not in {"complete", "incomplete"}
            or not receipt_sha
        ):
            return None
        if receipt_sha in self._seen_receipts:
            return self.snapshot()
        self._seen_receipts.add(receipt_sha)
        self.total_requests += 1
        self.total_response_bytes += max(
            0, int(receipt.get("response_bytes_read") or 0)
        )
        self.total_request_elapsed_ms += max(
            0, int(receipt.get("request_elapsed_ms") or 0)
        )
        self._observe_collection_evidence(family, receipt)

        pagination = receipt.get("pagination")
        if not isinstance(pagination, dict):
            pagination = {}
        hints = [
            item for item in (pagination.get("next_hints") or [])
            if isinstance(item, dict)
        ][:MAX_PAGINATION_HINTS]
        next_cursor_hashes = tuple(sorted({
            str(item.get("sha256") or "")
            for item in hints
            if item.get("kind") == "cursor"
            and len(str(item.get("sha256") or "")) == 64
        }))
        next_url_hashes = tuple(sorted({
            str(item.get("sha256") or "")
            for item in hints
            if item.get("kind") == "url"
            and len(str(item.get("sha256") or "")) == 64
        }))
        request_cursor_hashes = {
            str(item) for item in (
                receipt.get("request_cursor_sha256s") or []
            )
            if len(str(item)) == 64
        }
        request_url_hash = str(receipt.get("request_url_sha256") or "")
        request_identity_hash = str(
            receipt.get("request_identity_sha256") or ""
        )
        response_char_limit = max(
            1, int(receipt.get("response_char_limit") or 1)
        )
        # A losslessly spilled complete wire body is visibly truncated but no
        # longer an unresolved evidence obligation.  Drive chain state from
        # the producer's incomplete reasons, not the presentation flag.
        current_body_truncated = "body_truncated" in {
            str(item)
            for item in (receipt.get("incomplete_reasons") or [])
        }
        continuation_action = receipt.get("continuation_action")
        if not (
            isinstance(continuation_action, dict)
            and continuation_action.get("version")
            == CONTINUATION_ACTION_VERSION
            and isinstance(continuation_action.get("kind"), str)
        ):
            continuation_action = None
        else:
            continuation_action = copy.deepcopy(continuation_action)
        reasons = tuple(sorted({
            str(item) for item in (
                receipt.get("incomplete_reasons") or []
            )
            if str(item)
        }))

        prior = self._open.get(family)
        linked = prior is None
        consumed_cursor_hashes: set[str] = set()
        consumed_url_hashes: set[str] = set()
        truncation_retry = False
        replacement_restart = False
        exact_truncation_no_progress = False
        opaque_pagination_advance = False
        if prior is not None:
            expected_cursor = set(prior.expected_cursor_sha256s)
            expected_url = set(prior.expected_url_sha256s)
            consumed_cursor_hashes = expected_cursor & request_cursor_hashes
            consumed_url_hashes = (
                {request_url_hash}
                if request_url_hash and request_url_hash in expected_url
                else set()
            )
            prior_body_truncated = (
                "body_truncated" in prior.incomplete_reasons
            )
            same_request_identity = bool(
                request_identity_hash
                and request_identity_hash
                == prior.last_request_identity_sha256
            )
            truncation_retry = bool(
                prior_body_truncated
                and same_request_identity
                and (
                    response_char_limit > prior.response_char_limit
                    or not current_body_truncated
                )
            )
            exact_truncation_no_progress = bool(
                prior_body_truncated
                and same_request_identity
                and current_body_truncated
                and response_char_limit <= prior.response_char_limit
            )
            prior_action = prior.continuation_action or {}
            replacement_restart = bool(
                prior_body_truncated
                and prior_action.get("kind")
                == "restart_with_smaller_page"
                and request_identity_hash
                and request_identity_hash
                == str(
                    prior_action.get("expected_request_identity_sha256")
                    or ""
                )
            )
            opaque_pagination_advance = bool(
                not prior_body_truncated
                and not expected_cursor
                and not expected_url
                and "pagination_more_available"
                in prior.incomplete_reasons
                and request_cursor_hashes
            )
            if prior_body_truncated:
                # A cursor from a page whose visible evidence is incomplete
                # may guide a later request, but consuming it cannot erase the
                # missing records on that page. Only an exact larger/full retry
                # or the exact machine-generated smaller-page replacement may
                # supersede that obligation.
                linked = bool(truncation_retry or replacement_restart)
            else:
                linked = bool(
                    consumed_cursor_hashes
                    or consumed_url_hashes
                    or opaque_pagination_advance
                )

        if exact_truncation_no_progress:
            # The response has already reached the same local presentation
            # bound for the exact same HTTP request. Another identical call
            # cannot reveal new visible evidence, so fail closed immediately
            # instead of spending a second grace attempt and growing context.
            self._set_terminal(
                "body_truncation_no_progress_at_limit",
                family=family,
            )
        elif state == "incomplete":
            if prior is not None and not linked:
                prior.unlinked_attempts += 1
                if prior.unlinked_attempts >= self.max_unlinked_attempts:
                    self._set_terminal(
                        "body_truncation_continuation_not_followed"
                        if "body_truncated" in prior.incomplete_reasons
                        else "pagination_cursor_not_consumed",
                        family=family,
                    )
            else:
                self._terminal_chain_failures.pop(family, None)
                pages = (
                    1
                    if prior is None or replacement_restart
                    else (
                        prior.pages_observed
                        if truncation_retry
                        else prior.pages_observed + 1
                    )
                )
                if (
                    not truncation_retry
                    and not replacement_restart
                    and (
                        set(next_cursor_hashes) & consumed_cursor_hashes
                        or set(next_url_hashes) & consumed_url_hashes
                    )
                ):
                    self._set_terminal(
                        "pagination_cursor_repeated",
                        family=family,
                    )
                remaining_cursor_hashes = set()
                remaining_url_hashes = set()
                if prior is not None and truncation_retry:
                    # Preserve only independent frontier branches. The hints
                    # emitted by the retried page are refreshed from this
                    # receipt and must not be duplicated from the prior one.
                    remaining_cursor_hashes = set(
                        prior.carried_cursor_sha256s
                    )
                    remaining_url_hashes = set(prior.carried_url_sha256s)
                elif prior is not None and not replacement_restart:
                    remaining_cursor_hashes = (
                        set(prior.expected_cursor_sha256s)
                        - consumed_cursor_hashes
                    )
                    remaining_url_hashes = (
                        set(prior.expected_url_sha256s)
                        - consumed_url_hashes
                    )
                combined_cursor_hashes = tuple(sorted(
                    remaining_cursor_hashes | set(next_cursor_hashes)
                ))
                combined_url_hashes = tuple(sorted(
                    remaining_url_hashes | set(next_url_hashes)
                ))
                if combined_cursor_hashes or combined_url_hashes:
                    reasons = tuple(sorted({
                        *reasons,
                        "pagination_more_available",
                    }))
                self._open[family] = _OpenChain(
                    family_sha256=family,
                    incomplete_reasons=reasons,
                    expected_cursor_sha256s=combined_cursor_hashes,
                    expected_url_sha256s=combined_url_hashes,
                    last_request_identity_sha256=request_identity_hash,
                    response_char_limit=response_char_limit,
                    continuation_action=continuation_action,
                    carried_cursor_sha256s=(
                        tuple(sorted(remaining_cursor_hashes))
                        if current_body_truncated else ()
                    ),
                    carried_url_sha256s=(
                        tuple(sorted(remaining_url_hashes))
                        if current_body_truncated else ()
                    ),
                    pages_observed=pages,
                    ready_sequence=self.total_requests,
                )
                if "pagination_frontier_truncated" in reasons:
                    self._set_terminal(
                        "pagination_frontier_limit",
                        family=family,
                    )
                elif "pagination_scan_bounded" in reasons:
                    self._set_terminal(
                        "pagination_scan_limit",
                        family=family,
                    )
                if (
                    continuation_action is not None
                    and continuation_action.get("kind") == "degrade"
                ):
                    self._set_terminal(
                        str(
                            continuation_action.get("reason")
                            or "retrieval_incomplete"
                        ),
                        family=family,
                    )
                if pages >= self.max_pages_per_chain:
                    self._set_terminal(
                        "pagination_page_limit",
                        family=family,
                    )
        elif prior is not None and not linked:
            # A syntactically valid response from the same broad family is not
            # evidence that the exact outstanding cursor/URL was consumed.
            # Count it just like an unlinked incomplete response so a model
            # cannot keep the chain alive by rotating unrelated first pages.
            prior.unlinked_attempts += 1
            if prior.unlinked_attempts >= self.max_unlinked_attempts:
                self._set_terminal(
                    "body_truncation_continuation_not_followed"
                    if "body_truncated" in prior.incomplete_reasons
                    else "pagination_cursor_not_consumed",
                    family=family,
                )
        elif prior is not None and linked:
            self._terminal_chain_failures.pop(family, None)
            if replacement_restart:
                # A smaller first page replaces the oversized request only
                # when the server explicitly says that this replacement chain
                # ended. A generic complete response without a pagination
                # signal cannot prove equivalent collection coverage.
                if pagination.get("explicit_end") is True:
                    remaining_cursor_hashes: set[str] = set()
                    remaining_url_hashes: set[str] = set()
                else:
                    self._set_terminal(
                        "pagination_replacement_unproven",
                        family=family,
                    )
                    remaining_cursor_hashes = set(
                        prior.expected_cursor_sha256s
                    )
                    remaining_url_hashes = set(prior.expected_url_sha256s)
            elif truncation_retry:
                remaining_cursor_hashes = set(
                    prior.carried_cursor_sha256s
                )
                remaining_url_hashes = set(prior.carried_url_sha256s)
            else:
                remaining_cursor_hashes = (
                    set(prior.expected_cursor_sha256s)
                    - consumed_cursor_hashes
                )
                remaining_url_hashes = (
                    set(prior.expected_url_sha256s)
                    - consumed_url_hashes
                )
            if remaining_cursor_hashes or remaining_url_hashes:
                self._open[family] = _OpenChain(
                    family_sha256=family,
                    incomplete_reasons=("pagination_more_available",),
                    expected_cursor_sha256s=tuple(sorted(
                        remaining_cursor_hashes
                    )),
                    expected_url_sha256s=tuple(sorted(
                        remaining_url_hashes
                    )),
                    last_request_identity_sha256=request_identity_hash,
                    response_char_limit=response_char_limit,
                    continuation_action=continuation_action,
                    pages_observed=(
                        prior.pages_observed
                        if truncation_retry
                        else prior.pages_observed + 1
                    ),
                    ready_sequence=self.total_requests,
                )
            else:
                self._open.pop(family, None)
                self._terminal_chain_failures.pop(family, None)

        if self.total_requests >= self.max_total_requests and self._open:
            self._set_terminal(
                "retrieval_request_limit",
                global_scope=True,
            )
        if (
            self.total_response_bytes >= self.max_total_response_bytes
            and self._open
        ):
            self._set_terminal(
                "retrieval_cumulative_byte_limit",
                global_scope=True,
            )
        if (
            self.total_request_elapsed_ms
            >= self.max_total_request_elapsed_ms
            and self._open
        ):
            self._set_terminal(
                "retrieval_total_time_limit",
                global_scope=True,
            )
        return self.snapshot()

    @property
    def has_open_chains(self) -> bool:
        return bool(self._open)

    def requires_mandatory_continuation(self, policy: str) -> bool:
        """Whether the next turn must perform the machine continuation.

        A truncated response is not usable as complete evidence and therefore
        always requires its exact repair.  A clean page that merely advertises
        another cursor is optional under the default bounded-acquisition
        policy.  Only an explicit exhaustive policy promotes that cursor into
        a mandatory frontier.
        """

        normalized = normalize_retrieval_completeness_policy(policy)
        if self.terminal_failure is not None or not self._open:
            return False
        return any(
            self._chain_requires_mandatory_continuation(
                chain, normalized
            )
            for family, chain in self._open.items()
            if family not in self._terminal_chain_failures
        )

    def has_optional_pagination_frontier(self, policy: str) -> bool:
        normalized = normalize_retrieval_completeness_policy(policy)
        return bool(
            normalized == RETRIEVAL_COMPLETENESS_POLICY_BOUNDED
            and bool(set(self._open) - set(self._terminal_chain_failures))
            and self.terminal_failure is None
            and not self.requires_mandatory_continuation(normalized)
        )

    def closure_quality_impact(self, policy: str) -> str:
        """Classify the current open frontier at the tracker authority."""

        if (
            not self._terminal_chain_failures
            and self._global_terminal_failure is None
            and self.has_optional_pagination_frontier(policy)
        ):
            return RETRIEVAL_QUALITY_IMPACT_ADVISORY
        return RETRIEVAL_QUALITY_IMPACT_DEGRADED

    @staticmethod
    def _chain_requires_mandatory_continuation(
        chain: _OpenChain,
        normalized_policy: str,
    ) -> bool:
        if (
            normalized_policy
            == RETRIEVAL_COMPLETENESS_POLICY_EXHAUSTIVE
        ):
            return True
        return not set(chain.incomplete_reasons).issubset({
            "pagination_more_available",
        })

    def next_continuation_action(
        self,
        policy: str = RETRIEVAL_COMPLETENESS_POLICY_BOUNDED,
        *,
        mandatory_only: bool = False,
    ) -> dict[str, Any] | None:
        """Return the exact harness-generated action for the next open chain.

        The action is kept outside ``snapshot`` so ordinary lifecycle/debug
        records do not persist raw query values or POST bodies. Agent-loop may
        inject this bounded copy into the immediate correction turn; the
        normal bridge grant and preflight remain authoritative.

        Selection is policy-aware and stable. Under bounded acquisition, any
        non-pagination-only obligation is selected before a clean optional
        cursor. Under exhaustive acquisition every open chain is mandatory.
        Mandatory chains use a deterministic least-recently-advanced order, so
        one productive multi-page family cannot starve another unresolved
        family. Merely reading this method never advances the order.

        ``mandatory_only`` lets a forced-correction caller refuse an advisory
        cursor when no mandatory action remains.
        """

        normalized = normalize_retrieval_completeness_policy(policy)
        if self.terminal_failure is not None:
            return None

        ordered = sorted(
            (
                chain
                for family, chain in self._open.items()
                if family not in self._terminal_chain_failures
            ),
            key=lambda chain: (
                max(0, int(chain.ready_sequence)),
                chain.family_sha256,
            ),
        )
        mandatory = [
            chain
            for chain in ordered
            if self._chain_requires_mandatory_continuation(
                chain, normalized
            )
        ]
        if mandatory:
            candidates = mandatory
        elif mandatory_only:
            return None
        else:
            candidates = ordered

        for chain in candidates:
            action = chain.continuation_action
            if isinstance(action, dict) and action.get("kind") != "degrade":
                return copy.deepcopy(action)
        return None

    def _continuation_action_summary(self) -> dict[str, Any] | None:
        action = self.next_continuation_action()
        if action is None:
            return None
        args = action.get("args")
        page_window = action.get("page_window")
        return {
            "version": action.get("version"),
            "kind": str(action.get("kind") or ""),
            "tool_name": str(action.get("tool_name") or ""),
            "reason": str(action.get("reason") or ""),
            "argument_keys": sorted(
                str(key) for key in args
            ) if isinstance(args, dict) else [],
            "page_window": (
                {
                    key: page_window.get(key)
                    for key in (
                        "location", "key", "path", "from", "to"
                    )
                    if page_window.get(key) not in (None, "")
                }
                if isinstance(page_window, dict) else None
            ),
            "expected_request_identity_sha256": str(
                action.get("expected_request_identity_sha256") or ""
            ),
        }

    def frontier_receipt(self) -> dict[str, Any]:
        """Return an exact, secret-free receipt for the unresolved frontier.

        Cursor values and next URLs can contain sensitive query material, so
        the durable receipt retains their canonical SHA-256 identities rather
        than raw values.  Page/item observations remain exact per request
        family through the existing evidence ledger and are never represented
        as a source-wide or de-duplicated total.
        """

        cursor_hashes = sorted({
            value
            for chain in self._open.values()
            for value in chain.expected_cursor_sha256s
            if len(value) == 64
        })
        url_hashes = sorted({
            value
            for chain in self._open.values()
            for value in chain.expected_url_sha256s
            if len(value) == 64
        })
        ledger = self.evidence_ledger()
        families = [
            {
                key: family.get(key)
                for key in (
                    "family_sha256",
                    "http_responses_observed",
                    "pages_observed",
                    "items_observed",
                    "uncounted_response_count",
                    "source_declared_total",
                    "observations_sha256",
                )
            }
            for family in (ledger.get("families") or [])
            if isinstance(family, Mapping)
        ]
        return {
            "version": 1,
            "identity_representation": "sha256",
            "raw_cursor_or_url_persisted": False,
            "next_cursor_count": len(cursor_hashes),
            "next_cursor_sha256s": cursor_hashes[:MAX_PAGINATION_HINTS],
            "next_cursor_hashes_omitted": max(
                0, len(cursor_hashes) - MAX_PAGINATION_HINTS
            ),
            "next_url_count": len(url_hashes),
            "next_url_sha256s": url_hashes[:MAX_PAGINATION_HINTS],
            "next_url_hashes_omitted": max(
                0, len(url_hashes) - MAX_PAGINATION_HINTS
            ),
            "families": families,
            "family_count": len(families),
            "count_semantics": {
                "pages_are_distinct_canonical_requests": True,
                "items_are_collection_slots_not_unique_records": True,
                "items_are_not_source_total": True,
                "unresolved_frontier_means_coverage_is_partial": bool(
                    self._open
                ),
            },
            "limits": {
                "max_pages_per_chain": self.max_pages_per_chain,
                "max_total_requests": self.max_total_requests,
                "max_total_response_bytes": self.max_total_response_bytes,
                "max_total_request_elapsed_ms": (
                    self.max_total_request_elapsed_ms
                ),
            },
        }

    def snapshot(self) -> dict[str, Any]:
        reason_counts: dict[str, int] = {}
        for chain in self._open.values():
            for reason in chain.incomplete_reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        return {
            "open_chain_count": len(self._open),
            "open_frontier_count": sum(
                len(chain.expected_cursor_sha256s)
                + len(chain.expected_url_sha256s)
                + int(
                    not chain.expected_cursor_sha256s
                    and not chain.expected_url_sha256s
                )
                for chain in self._open.values()
            ),
            "open_reasons": dict(sorted(reason_counts.items())),
            "pages_observed": sum(
                chain.pages_observed for chain in self._open.values()
            ),
            "total_requests": self.total_requests,
            "total_response_bytes": self.total_response_bytes,
            "total_request_elapsed_ms": self.total_request_elapsed_ms,
            "max_pages_per_chain": self.max_pages_per_chain,
            "max_total_response_bytes": self.max_total_response_bytes,
            "max_total_request_elapsed_ms": (
                self.max_total_request_elapsed_ms
            ),
            "max_total_requests": self.max_total_requests,
            "terminal_failure": self.terminal_failure,
            "global_terminal_failure": self._global_terminal_failure,
            "terminal_chain_count": len(
                set(self._open) & set(self._terminal_chain_failures)
            ),
            "runnable_chain_count": len(
                set(self._open) - set(self._terminal_chain_failures)
            ),
            "terminal_chain_failures": [
                {
                    "family_sha256": family,
                    "reason": self._terminal_chain_failures[family],
                }
                for family in sorted(
                    set(self._open) & set(self._terminal_chain_failures)
                )
            ],
            "continuation_action": self._continuation_action_summary(),
            "frontier_receipt": self.frontier_receipt(),
            "evidence_ledger": self.evidence_ledger(),
        }
