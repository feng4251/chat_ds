"""Shared guards for compacted conversation-history placeholders."""

from __future__ import annotations

import json
import re
from typing import Any

_OMITTED_HISTORY_RE = re.compile(
    r"(?:"
    r"\[omitted\s+\d+\s+chars\s+from conversation history"
    r"(?:\s+for [^\]]+)?;\s+use the workspace file or tool result if needed\]"
    r"|__CHATDS_OMITTED_TOOL_CONTENT_[A-Z_]+__"
    r"|__CHATDS_OMITTED_TOOL_ARGUMENT_\d+_CHARS__"
    r"|\[large argument omitted:\s*\d+\s*chars(?:[^\]]*)\]"
    r")",
    re.IGNORECASE,
)

_RESERVED_OMISSION_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"_chatds_arguments?_omitted"
    r"(?![A-Za-z0-9_])"
    # The marker may be plain JSON, Python-ish text, or escaped JSON embedded
    # in Markdown/a JSON string.  Match only the reserved key assigned the
    # exact boolean/string value true; ordinary uses of the word 'omitted'
    # must not be treated as executable-history placeholders.
    r"(?:\\*[\"'])?\s*[:=]\s*"
    r"(?:true(?![A-Za-z0-9_])|(?:\\*[\"'])true(?:\\*[\"']))",
    re.IGNORECASE,
)

_RESERVED_OMISSION_KEYS = {
    "_chatds_argument_omitted",
    "_chatds_arguments_omitted",
}

# Tool arguments normally have much shallower schemas, but these deliberately
# generous limits also protect callers which invoke the shared guard before
# JSON-schema validation.  The scan must never recurse on an attacker-shaped
# Python value: deeply nested/cyclic structures and very wide containers are
# rejected deterministically instead of leaking ``RecursionError`` or doing
# unbounded work.
MAX_OMISSION_SCAN_DEPTH = 64
MAX_OMISSION_SCAN_NODES = 20_000
_SCAN_LIMIT_SUFFIX = {
    "depth": ".__chatds_omission_guard_depth_limit__",
    "nodes": ".__chatds_omission_guard_node_limit__",
}


def _marker_value_is_true(value: Any) -> bool:
    if value is True:
        return True
    return isinstance(value, str) and value.strip().casefold() == "true"


def find_compacted_history_omission_path(
    value: Any,
    path: str = "args",
) -> str | None:
    """Return an omitted-marker path, failing closed on scan limits.

    Container iterators are represented as explicit stack frames so traversal
    remains depth-first and preserves the historical first-match behavior
    without using the Python call stack.  A synthetic, stable path suffix is
    returned when either safety budget is exhausted; callers therefore reject
    the value just as they reject a real compacted-history marker.
    """

    # Frames are either ("visit", value, path, depth) or
    # ("children", iterator, parent_path, child_depth, container_kind).
    stack: list[tuple[Any, ...]] = [("visit", value, path, 0)]
    nodes_seen = 0

    while stack:
        frame = stack.pop()
        if frame[0] == "children":
            _, iterator, parent_path, child_depth, container_kind = frame
            try:
                key, item = next(iterator)
            except StopIteration:
                continue
            # Put the iterator back first; its next child is visited only after
            # this child has been completely traversed.
            stack.append(frame)
            if container_kind == "mapping":
                child_path = f"{parent_path}.{key}"
            else:
                child_path = f"{parent_path}[{key}]"
            stack.append(("visit", item, child_path, child_depth))
            continue

        _, current, current_path, depth = frame
        if depth > MAX_OMISSION_SCAN_DEPTH:
            return path + _SCAN_LIMIT_SUFFIX["depth"]
        nodes_seen += 1
        if nodes_seen > MAX_OMISSION_SCAN_NODES:
            return path + _SCAN_LIMIT_SUFFIX["nodes"]

        if isinstance(current, dict):
            for key in _RESERVED_OMISSION_KEYS:
                if key in current and _marker_value_is_true(current.get(key)):
                    return current_path
            stack.append((
                "children",
                iter(current.items()),
                current_path,
                depth + 1,
                "mapping",
            ))
            continue

        if isinstance(current, (list, tuple)):
            stack.append((
                "children",
                iter(enumerate(current)),
                current_path,
                depth + 1,
                "sequence",
            ))
            continue

        if not isinstance(current, str) or not current:
            continue
        if _OMITTED_HISTORY_RE.search(current) or _RESERVED_OMISSION_MARKER_RE.search(current):
            return current_path
        stripped = current.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except RecursionError:
                # CPython's JSON decoder has its own recursion limit.  Treat
                # hitting it exactly like our explicit depth budget.
                return path + _SCAN_LIMIT_SUFFIX["depth"]
            except (json.JSONDecodeError, TypeError):
                continue
            stack.append(("visit", parsed, current_path, depth + 1))
    return None


def contains_compacted_history_omission(value: Any) -> bool:
    return find_compacted_history_omission_path(value) is not None


def compacted_history_omission_error(field: str) -> dict:
    for limit_kind, suffix in _SCAN_LIMIT_SUFFIX.items():
        if field.endswith(suffix):
            root_field = field[: -len(suffix)] or "args"
            return {
                "error": (
                    f"{root_field} exceeds the compacted-history safety scan "
                    f"{limit_kind} limit; request rejected without execution."
                ),
                "reason": "omission_guard_limit_exceeded",
                "field": root_field,
                "limit_kind": limit_kind,
            }
    return {
        "error": (
            f"{field} contains a compacted conversation-history placeholder, not real file content. "
            "Regenerate the actual content or read the workspace/source file before retrying."
        ),
        "reason": "invalid_placeholder_content",
        "field": field,
    }
