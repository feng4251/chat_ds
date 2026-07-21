"""Safe, segment-aware matching for workspace-relative artifact paths.

Artifact declarations are rooted at the session workspace.  This module keeps
their semantics independent from :mod:`fnmatch`'s platform/path quirks:

* ``{NAME}`` and ``<NAME>`` placeholders match characters in one path segment;
* ordinary glob tokens (``*``, ``?`` and ``[]``) never cross ``/``;
* only a complete ``**`` segment can match zero or more path segments; and
* absolute, empty, current-directory and parent-directory segments are invalid.

Matching is deliberately case-insensitive to preserve the harness's historical
artifact behavior while removing basename fallback and cross-directory globbing.
"""

from __future__ import annotations

import fnmatch
import re
from functools import lru_cache
from typing import Iterable


_PLACEHOLDER_RE = re.compile(r"\{[^{}\/]+\}|<[^<>\/]+>")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_MAX_PATH_CHARS = 4096
_MAX_PATH_SEGMENTS = 256


class WorkspacePatternError(ValueError):
    """Raised when a workspace path or declaration is unsafe or malformed."""


def normalize_workspace_path(value: object) -> str:
    """Return a safe normalized workspace-relative concrete path.

    Backslashes are accepted as separators for compatibility with Skill
    packages authored on Windows.  No lexical cleanup is performed: unsafe or
    ambiguous components are rejected rather than silently collapsed.
    """

    return _normalize(value, pattern=False)


def normalize_workspace_pattern(value: object) -> str:
    """Return a safe normalized workspace-relative artifact declaration."""

    return _normalize(value, pattern=True)


def _normalize(value: object, *, pattern: bool) -> str:
    if not isinstance(value, str):
        raise WorkspacePatternError("workspace path must be a string")
    text = value.strip().replace("\\", "/")
    if not text:
        raise WorkspacePatternError("workspace path is empty")
    if len(text) > _MAX_PATH_CHARS:
        raise WorkspacePatternError("workspace path is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise WorkspacePatternError("workspace path contains control characters")
    if text.startswith(("/", "~")) or _SCHEME_RE.match(text):
        raise WorkspacePatternError("workspace path must be relative")
    parts = text.split("/")
    if len(parts) > _MAX_PATH_SEGMENTS:
        raise WorkspacePatternError("workspace path has too many segments")
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkspacePatternError(
            "workspace path contains an empty, current, or parent segment"
        )
    for part in parts:
        if pattern and "**" in part and part != "**":
            raise WorkspacePatternError("** is valid only as a complete segment")
    if pattern and parts[-1] == "**":
        raise WorkspacePatternError(
            "an artifact pattern must end in a file segment, not **"
        )
    return "/".join(parts)


@lru_cache(maxsize=2048)
def _compiled_segments(pattern: str) -> tuple[str, ...]:
    normalized = normalize_workspace_pattern(pattern)
    return tuple(normalized.casefold().split("/"))


@lru_cache(maxsize=8192)
def _segment_matches(candidate: str, declaration: str) -> bool:
    # A placeholder is intentionally non-empty.  Replacing it with ``?*``
    # keeps it inside the current segment and prevents a bare placeholder from
    # accepting an empty filename component.
    glob = _PLACEHOLDER_RE.sub("?*", declaration)
    return fnmatch.fnmatchcase(candidate.casefold(), glob.casefold())


def workspace_pattern_matches(path: object, pattern: object) -> bool:
    """Return whether ``path`` satisfies the rooted artifact ``pattern``.

    Invalid input fails closed.  Call :func:`normalize_workspace_pattern` when
    a caller needs the precise validation error for diagnostics.
    """

    try:
        candidate = tuple(normalize_workspace_path(path).casefold().split("/"))
        declarations = _compiled_segments(normalize_workspace_pattern(pattern))
    except WorkspacePatternError:
        return False

    # Small dynamic program for explicit ** segments.  Workspace paths and
    # declarations are bounded elsewhere, and the memoized state space is
    # O(path_segments * pattern_segments).
    memo: dict[tuple[int, int], bool] = {}

    def visit(candidate_index: int, declaration_index: int) -> bool:
        state = (candidate_index, declaration_index)
        if state in memo:
            return memo[state]
        if declaration_index == len(declarations):
            result = candidate_index == len(candidate)
        elif declarations[declaration_index] == "**":
            result = visit(candidate_index, declaration_index + 1) or (
                candidate_index < len(candidate)
                and visit(candidate_index + 1, declaration_index)
            )
        else:
            result = (
                candidate_index < len(candidate)
                and _segment_matches(
                    candidate[candidate_index],
                    declarations[declaration_index],
                )
                and visit(candidate_index + 1, declaration_index + 1)
            )
        memo[state] = result
        return result

    return visit(0, 0)


def matching_workspace_paths(
    paths: Iterable[str],
    pattern: object,
) -> list[str]:
    """Return every concrete path matching one declaration, in input order."""

    try:
        normalized_pattern = normalize_workspace_pattern(pattern)
    except WorkspacePatternError:
        return []
    return [
        path for path in paths
        if workspace_pattern_matches(path, normalized_pattern)
    ]


__all__ = [
    "WorkspacePatternError",
    "matching_workspace_paths",
    "normalize_workspace_path",
    "normalize_workspace_pattern",
    "workspace_pattern_matches",
]
