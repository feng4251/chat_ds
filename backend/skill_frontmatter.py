"""Bounded YAML frontmatter parsing for uploaded Skill manifests.

Skill packages are untrusted input.  This module intentionally stays on the
backend side of the service boundary instead of importing the harness loader,
while matching its SafeLoader and fail-closed parsing semantics.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


MAX_FRONTMATTER_SOURCE_CHARS = 1_000_000
MAX_FRONTMATTER_STRUCTURE_DEPTH = 64
MAX_FRONTMATTER_STRUCTURE_NODES = 20_000
MAX_FRONTMATTER_SCALAR_CHARS = 500_000

_CLOSING_DELIMITER_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


class SkillFrontmatterError(ValueError):
    """Stable fail-closed error for an invalid Skill frontmatter document."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _frontmatter_source(text: str) -> str | None:
    """Return the bounded YAML source, or ``None`` when no header is present."""
    if not text.startswith("---"):
        return None

    first_newline = text.find("\n", 0, 64)
    if first_newline < 0 or text[:first_newline].rstrip("\r").strip() != "---":
        return None

    source_start = first_newline + 1
    # Include enough trailing characters for the closing delimiter itself,
    # without scanning an arbitrarily large SKILL.md looking for one.
    search_end = min(
        len(text),
        source_start + MAX_FRONTMATTER_SOURCE_CHARS + len("---\r\n"),
    )
    closing = _CLOSING_DELIMITER_RE.search(text, source_start, search_end)
    if closing is None:
        if len(text) - source_start > MAX_FRONTMATTER_SOURCE_CHARS:
            raise SkillFrontmatterError(
                "frontmatter_source_limit_exceeded",
                "YAML frontmatter exceeds the source-size limit or has no bounded closing delimiter.",
            )
        raise SkillFrontmatterError(
            "unclosed_frontmatter",
            "YAML frontmatter has no closing delimiter.",
        )

    source = text[source_start:closing.start()]
    if len(source) > MAX_FRONTMATTER_SOURCE_CHARS:
        raise SkillFrontmatterError(
            "frontmatter_source_limit_exceeded",
            "YAML frontmatter exceeds the source-size limit.",
        )
    return source


def _audit_yaml_nodes(root: Any) -> None:
    """Reject duplicate keys and bound the composed YAML node graph."""
    stack: list[tuple[Any, int]] = [(root, 0)]
    visited: set[int] = set()
    nodes = 0
    scalar_chars = 0

    while stack:
        node, depth = stack.pop()
        identity = id(node)
        if identity in visited:
            continue
        visited.add(identity)
        nodes += 1
        if nodes > MAX_FRONTMATTER_STRUCTURE_NODES:
            raise SkillFrontmatterError(
                "frontmatter_node_limit_exceeded",
                "YAML frontmatter exceeds the structure-node limit.",
            )
        if depth > MAX_FRONTMATTER_STRUCTURE_DEPTH:
            raise SkillFrontmatterError(
                "frontmatter_depth_limit_exceeded",
                "YAML frontmatter exceeds the nesting-depth limit.",
            )

        if isinstance(node, ScalarNode):
            scalar_chars += len(str(node.value or ""))
            if scalar_chars > MAX_FRONTMATTER_SCALAR_CHARS:
                raise SkillFrontmatterError(
                    "frontmatter_scalar_limit_exceeded",
                    "YAML frontmatter exceeds the aggregate scalar-text limit.",
                )
            continue

        if isinstance(node, MappingNode):
            seen_keys: set[str] = set()
            for key_node, value_node in node.value:
                key = (
                    str(key_node.value)
                    if isinstance(key_node, ScalarNode)
                    else "<non-scalar-key>"
                )
                if key in seen_keys:
                    raise SkillFrontmatterError(
                        "duplicate_frontmatter_key",
                        f"YAML frontmatter contains duplicate key {key!r}.",
                    )
                seen_keys.add(key)
                stack.append((value_node, depth + 1))
                stack.append((key_node, depth + 1))
        elif isinstance(node, SequenceNode):
            stack.extend((child, depth + 1) for child in reversed(node.value))


def _audit_loaded_graph(value: Any) -> None:
    """Bound aliases after construction and reject recursive alias cycles."""
    stack: list[tuple[str, Any, int]] = [("enter", value, 0)]
    active: set[int] = set()
    visited_depth: dict[int, int] = {}
    counted_containers: set[int] = set()
    nodes = 0
    scalar_chars = 0

    while stack:
        action, node, depth = stack.pop()
        is_container = isinstance(node, (dict, list, tuple, set))
        if action == "exit":
            if is_container:
                active.discard(id(node))
            continue

        identity = id(node) if is_container else None
        if identity is None or identity not in counted_containers:
            nodes += 1
            if identity is not None:
                counted_containers.add(identity)
        if nodes > MAX_FRONTMATTER_STRUCTURE_NODES:
            raise SkillFrontmatterError(
                "frontmatter_node_limit_exceeded",
                "YAML frontmatter exceeds the structure-node limit.",
            )
        if depth > MAX_FRONTMATTER_STRUCTURE_DEPTH:
            raise SkillFrontmatterError(
                "frontmatter_depth_limit_exceeded",
                "YAML frontmatter exceeds the nesting-depth limit.",
            )

        if not is_container:
            scalar_chars += len(str(node)) if node is not None else 0
            if scalar_chars > MAX_FRONTMATTER_SCALAR_CHARS:
                raise SkillFrontmatterError(
                    "frontmatter_scalar_limit_exceeded",
                    "YAML frontmatter exceeds the aggregate scalar-text limit.",
                )
            continue

        assert identity is not None
        if identity in active:
            raise SkillFrontmatterError(
                "frontmatter_alias_cycle",
                "YAML frontmatter contains a recursive alias cycle.",
            )
        previous_depth = visited_depth.get(identity)
        if previous_depth is not None and depth <= previous_depth:
            continue
        visited_depth[identity] = depth
        active.add(identity)
        stack.append(("exit", node, depth))

        children: list[Any] = []
        if isinstance(node, dict):
            for key, child in node.items():
                scalar_chars += len(str(key))
                if scalar_chars > MAX_FRONTMATTER_SCALAR_CHARS:
                    raise SkillFrontmatterError(
                        "frontmatter_scalar_limit_exceeded",
                        "YAML frontmatter exceeds the aggregate scalar-text limit.",
                    )
                children.append(child)
        else:
            children.extend(node)
        for child in reversed(children):
            stack.append(("enter", child, depth + 1))


def parse_skill_frontmatter(text: str) -> dict[str, Any]:
    """Parse a Skill header with bounded ``yaml.safe_load`` semantics.

    A document without a frontmatter opener remains an empty mapping so the
    caller can retain its existing required-name validation.  Once an opener
    is present, malformed, oversized, cyclic, duplicate-key, empty, or
    non-mapping YAML is rejected rather than reinterpreted by a fallback.
    """
    source = _frontmatter_source(text)
    if source is None:
        return {}

    try:
        composed = yaml.compose(source, Loader=yaml.SafeLoader)
        if composed is not None:
            _audit_yaml_nodes(composed)
        parsed = yaml.safe_load(source)
    except SkillFrontmatterError:
        raise
    except Exception as exc:
        raise SkillFrontmatterError(
            "invalid_frontmatter_yaml",
            f"Could not parse YAML frontmatter: {exc}",
        ) from exc

    if not isinstance(parsed, dict):
        raise SkillFrontmatterError(
            "invalid_frontmatter_document",
            "YAML frontmatter must contain a mapping at its root.",
        )
    _audit_loaded_graph(parsed)
    return parsed
