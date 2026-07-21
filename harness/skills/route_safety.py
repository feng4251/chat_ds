"""Bounded, fail-closed helpers for Skill-declared route regular expressions.

Route expressions are package data, not trusted application code.  Python's
standard ``re`` engine has no match timeout and can exhibit catastrophic
backtracking, so merely compiling an expression is not a sufficient safety
check.  This module deliberately accepts a conservative, auditable subset and
also bounds the request text searched at runtime.

The structural checks are intentionally shared by the package compiler and
runtime.  Runtime validation is still required because execution contracts can
come from caches, tests, or older persisted sessions that did not pass through
the current compiler.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Any

try:  # ``re._parser`` is the stdlib parser on supported Python versions.
    from re import _parser as _re_parser
except ImportError:  # pragma: no cover - compatibility with older Python.
    import sre_parse as _re_parser  # type: ignore[no-redef]


MAX_ROUTE_PATTERNS_PER_ROUTE = 64
MAX_ROUTE_PATTERNS_TOTAL = 512
MAX_ROUTE_PATTERN_CHARS = 512
MAX_ROUTE_RUNTIME_TEXT_CHARS = 8_192
MAX_SKILL_ROUTES = 256

_MAX_PARSED_NODES = 1_024
_MAX_BRANCH_ALTERNATIVES = 128
_MAX_REPEAT_BOUND = 4_096
_MAX_ENUMERATED_CHARACTERS = 4_096
_REPEAT_OPS = {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}
_REJECTED_OPS = {
    # Backreferences and conditionals make runtime depend on captured text and
    # can re-introduce exponential paths even when the surrounding syntax is
    # superficially simple.
    "GROUPREF",
    "GROUPREF_EXISTS",
    "GROUPREF_IGNORE",
    "GROUPREF_LOC_IGNORE",
    "GROUPREF_UNI_IGNORE",
    # Lookarounds can repeatedly rescan the same suffix.  Route triggers do not
    # need them; fail closed instead of trying to prove each assertion safe.
    "ASSERT",
    "ASSERT_NOT",
}
_LEAF_OPS = {
    "ANY",
    "ANY_ALL",
    "AT",
    "CATEGORY",
    "FAILURE",
    "LITERAL",
    "LITERAL_IGNORE",
    "LITERAL_LOC_IGNORE",
    "LITERAL_UNI_IGNORE",
    "NOT_LITERAL",
    "NOT_LITERAL_IGNORE",
    "NOT_LITERAL_LOC_IGNORE",
    "NOT_LITERAL_UNI_IGNORE",
    "SUCCESS",
}
_CHARSET_OPS = {
    "BIGCHARSET",
    "CATEGORY",
    "CHARSET",
    "LITERAL",
    "NEGATE",
    "RANGE",
    "RANGE_UNI_IGNORE",
}


def _op_name(op: Any) -> str:
    return str(op)


def _contains_branch(nodes: Any) -> bool:
    """Return whether a parsed subtree contains an alternation."""
    for op, argument in nodes:
        name = _op_name(op)
        if name == "BRANCH":
            return True
        if name == "SUBPATTERN" and _contains_branch(argument[-1]):
            return True
        if name in _REPEAT_OPS and _contains_branch(argument[2]):
            return True
        if name == "ATOMIC_GROUP" and _contains_branch(argument):
            return True
    return False


_CharacterDomain = frozenset[str] | None


@dataclass(frozen=True)
class _SequenceSummary:
    """Repeat frontiers that can touch a surrounding concatenation.

    ``None`` is a deliberately conservative character domain: it represents
    ANY, categories, negated sets, or a set too large to enumerate safely.
    Such a domain is assumed to overlap every other domain.
    """

    nullable: bool
    leading_repeats: tuple[_CharacterDomain, ...] = ()
    trailing_repeats: tuple[_CharacterDomain, ...] = ()


def _case_insensitive_keys(codepoint: int) -> frozenset[str]:
    """Return conservative one-character IGNORECASE equivalence keys."""
    character = chr(codepoint)
    variants = {
        character,
        character.lower(),
        character.upper(),
        character.title(),
        character.casefold(),
        character.swapcase(),
    }
    # Applying casefold after upper/lower catches Python's special Unicode
    # IGNORECASE pairs such as Kelvin-sign/K and long-s/S without attempting
    # to reproduce the regular-expression engine's complete Unicode tables.
    variants.update(value.casefold() for value in tuple(variants))
    variants.update(value.lower() for value in tuple(variants))
    return frozenset(variants)


def _merge_domains(
    left: _CharacterDomain,
    right: _CharacterDomain,
) -> _CharacterDomain:
    if left is None or right is None:
        return None
    return left | right


def _literal_domain(codepoint: int) -> _CharacterDomain:
    try:
        return _case_insensitive_keys(int(codepoint))
    except (OverflowError, TypeError, ValueError):  # pragma: no cover - parser contract.
        return None


def _charset_domain(charset: Any) -> _CharacterDomain:
    values: set[str] = set()
    enumerated = 0
    for op, argument in charset:
        name = _op_name(op)
        if name in {
            "BIGCHARSET",
            "CATEGORY",
            "CHARSET",
            "NEGATE",
        }:
            return None
        if name in {
            "LITERAL",
            "LITERAL_IGNORE",
            "LITERAL_LOC_IGNORE",
            "LITERAL_UNI_IGNORE",
        }:
            domain = _literal_domain(argument)
            if domain is None:
                return None
            values.update(domain)
            enumerated += 1
            continue
        if name in {"RANGE", "RANGE_UNI_IGNORE"}:
            try:
                start, end = (int(item) for item in argument)
            except (TypeError, ValueError):  # pragma: no cover - parser contract.
                return None
            width = end - start + 1
            if width < 0 or enumerated + width > _MAX_ENUMERATED_CHARACTERS:
                return None
            for codepoint in range(start, end + 1):
                domain = _literal_domain(codepoint)
                if domain is None:
                    return None
                values.update(domain)
            enumerated += width
            continue
        return None
    return frozenset(values)


def _node_first_domain(op: Any, argument: Any) -> tuple[bool, _CharacterDomain]:
    """Return ``(nullable, possible-first-character-domain)`` for one node."""
    name = _op_name(op)
    if name in {
        "LITERAL",
        "LITERAL_IGNORE",
        "LITERAL_LOC_IGNORE",
        "LITERAL_UNI_IGNORE",
    }:
        return False, _literal_domain(argument)
    if name == "IN":
        return False, _charset_domain(argument)
    if name in {
        "ANY",
        "ANY_ALL",
        "CATEGORY",
        "NOT_LITERAL",
        "NOT_LITERAL_IGNORE",
        "NOT_LITERAL_LOC_IGNORE",
        "NOT_LITERAL_UNI_IGNORE",
    }:
        return False, None
    if name in {"AT", "SUCCESS"}:
        return True, frozenset()
    if name == "FAILURE":
        return False, frozenset()
    if name == "SUBPATTERN":
        return _sequence_first_domain(argument[-1])
    if name == "ATOMIC_GROUP":
        return _sequence_first_domain(argument)
    if name == "BRANCH":
        nullable = False
        domain: _CharacterDomain = frozenset()
        for alternative in argument[1]:
            alternative_nullable, alternative_domain = _sequence_first_domain(
                alternative
            )
            nullable = nullable or alternative_nullable
            domain = _merge_domains(domain, alternative_domain)
        return nullable, domain
    if name in _REPEAT_OPS:
        minimum, _, child = argument
        child_nullable, child_domain = _sequence_first_domain(child)
        return int(minimum) == 0 or child_nullable, child_domain
    # Structural validation rejects unknown operations before this analysis.
    return False, None  # pragma: no cover - defensive fallback.


def _sequence_first_domain(nodes: Any) -> tuple[bool, _CharacterDomain]:
    nullable = True
    domain: _CharacterDomain = frozenset()
    for op, argument in nodes:
        node_nullable, node_domain = _node_first_domain(op, argument)
        if nullable:
            domain = _merge_domains(domain, node_domain)
        nullable = nullable and node_nullable
        if not nullable:
            break
    return nullable, domain


def _merge_frontiers(
    *frontiers: tuple[_CharacterDomain, ...],
) -> tuple[_CharacterDomain, ...]:
    merged: list[_CharacterDomain] = []
    for frontier in frontiers:
        for domain in frontier:
            if domain not in merged:
                merged.append(domain)
    return tuple(merged)


def _domains_may_overlap(
    left: _CharacterDomain,
    right: _CharacterDomain,
) -> bool:
    return left is None or right is None or not left.isdisjoint(right)


def _repeat_adjacency_summary(
    nodes: Any,
) -> tuple[str | None, _SequenceSummary]:
    """Reject ambiguous variable repeats that can become adjacent.

    A fixed consuming atom cuts the repeat frontier.  Capturing groups and
    zero-width atoms do not.  Nullable repeats retain the previous frontier,
    which catches non-obvious chains such as ``a+\\s*a+`` as well as direct
    chains such as ``a*a*a*b``.  Alternations are summarized per path without
    expanding their Cartesian product.
    """

    summaries: list[_SequenceSummary] = []
    for op, argument in nodes:
        name = _op_name(op)
        if name in _REPEAT_OPS:
            minimum, maximum, child = argument
            error, _ = _repeat_adjacency_summary(child)
            if error:
                return error, _SequenceSummary(nullable=False)
            minimum_int = int(minimum)
            maximum_int = int(maximum)
            if minimum_int != maximum_int:
                _, domain = _sequence_first_domain(child)
                frontier = (domain,)
            else:
                frontier = ()
            summaries.append(
                _SequenceSummary(
                    nullable=minimum_int == 0,
                    leading_repeats=frontier,
                    trailing_repeats=frontier,
                )
            )
            continue
        if name == "SUBPATTERN":
            error, summary = _repeat_adjacency_summary(argument[-1])
            if error:
                return error, summary
            summaries.append(summary)
            continue
        if name == "ATOMIC_GROUP":
            # Treat atomic groups transparently.  This is conservative if a
            # package later moves to another compatible regex implementation.
            error, summary = _repeat_adjacency_summary(argument)
            if error:
                return error, summary
            summaries.append(summary)
            continue
        if name == "BRANCH":
            nullable = False
            leading: tuple[_CharacterDomain, ...] = ()
            trailing: tuple[_CharacterDomain, ...] = ()
            for alternative in argument[1]:
                error, alternative_summary = _repeat_adjacency_summary(
                    alternative
                )
                if error:
                    return error, alternative_summary
                nullable = nullable or alternative_summary.nullable
                leading = _merge_frontiers(
                    leading, alternative_summary.leading_repeats
                )
                trailing = _merge_frontiers(
                    trailing, alternative_summary.trailing_repeats
                )
            summaries.append(
                _SequenceSummary(
                    nullable=nullable,
                    leading_repeats=leading,
                    trailing_repeats=trailing,
                )
            )
            continue
        node_nullable, _ = _node_first_domain(op, argument)
        summaries.append(_SequenceSummary(nullable=node_nullable))

    pending: tuple[_CharacterDomain, ...] = ()
    leading: tuple[_CharacterDomain, ...] = ()
    prefix_nullable = True
    for summary in summaries:
        for previous_domain in pending:
            for next_domain in summary.leading_repeats:
                if _domains_may_overlap(previous_domain, next_domain):
                    return (
                        "adjacent variable quantifiers may consume "
                        "overlapping characters",
                        _SequenceSummary(nullable=False),
                    )
        if prefix_nullable:
            leading = _merge_frontiers(leading, summary.leading_repeats)
        prefix_nullable = prefix_nullable and summary.nullable
        if summary.nullable:
            pending = _merge_frontiers(pending, summary.trailing_repeats)
        else:
            pending = summary.trailing_repeats

    return (
        None,
        _SequenceSummary(
            nullable=prefix_nullable,
            leading_repeats=leading,
            trailing_repeats=pending,
        ),
    )


@lru_cache(maxsize=2_048)
def _validated_route_pattern(
    pattern: str,
) -> tuple[str | None, re.Pattern[str] | None]:
    """Validate and compile once so compiler and runtime share one verdict."""
    if not isinstance(pattern, str) or not pattern:
        return "pattern must be a non-empty string", None
    if len(pattern) > MAX_ROUTE_PATTERN_CHARS:
        return f"pattern exceeds {MAX_ROUTE_PATTERN_CHARS} characters", None
    if "\x00" in pattern:
        return "pattern contains a NUL character", None
    try:
        parsed = _re_parser.parse(pattern, re.IGNORECASE)
    except (re.error, OverflowError, ValueError) as exc:
        return f"invalid regular expression: {exc}", None

    node_count = 0
    branch_count = 0

    def walk(nodes: Any, *, inside_repeat: bool = False) -> str | None:
        nonlocal node_count, branch_count
        for op, argument in nodes:
            node_count += 1
            if node_count > _MAX_PARSED_NODES:
                return f"pattern exceeds {_MAX_PARSED_NODES} parsed nodes"
            name = _op_name(op)
            if name in _REJECTED_OPS:
                return f"unsupported potentially unsafe construct: {name.lower()}"
            if name in _LEAF_OPS:
                continue
            if name == "IN":
                for charset_op, _ in argument:
                    node_count += 1
                    if node_count > _MAX_PARSED_NODES:
                        return f"pattern exceeds {_MAX_PARSED_NODES} parsed nodes"
                    charset_name = _op_name(charset_op)
                    if charset_name not in _CHARSET_OPS:
                        return (
                            "unsupported character-set construct: "
                            f"{charset_name.lower()}"
                        )
                continue
            if name == "SUBPATTERN":
                error = walk(argument[-1], inside_repeat=inside_repeat)
                if error:
                    return error
                continue
            if name == "BRANCH":
                alternatives = argument[1]
                branch_count += len(alternatives)
                if branch_count > _MAX_BRANCH_ALTERNATIVES:
                    return (
                        "pattern exceeds "
                        f"{_MAX_BRANCH_ALTERNATIVES} branch alternatives"
                    )
                for alternative in alternatives:
                    error = walk(alternative, inside_repeat=inside_repeat)
                    if error:
                        return error
                continue
            if name in _REPEAT_OPS:
                minimum, maximum, child = argument
                if inside_repeat:
                    return "nested quantifiers are not allowed"
                # A repeated alternation such as ``(a|aa)+`` has overlapping
                # parse paths and is a classic catastrophic-backtracking form.
                if _contains_branch(child):
                    return "quantified alternation is not allowed"
                try:
                    minimum_width, _ = child.getwidth()
                except AttributeError:  # pragma: no cover - parser contract.
                    minimum_width = 0
                if minimum_width == 0:
                    return "a quantified expression must consume input"
                max_repeat = int(maximum)
                parser_unbounded = int(getattr(_re_parser, "MAXREPEAT", max_repeat))
                if max_repeat != parser_unbounded and max_repeat > _MAX_REPEAT_BOUND:
                    return (
                        "finite repeat bound exceeds "
                        f"{_MAX_REPEAT_BOUND}"
                    )
                error = walk(child, inside_repeat=True)
                if error:
                    return error
                continue
            if name == "ATOMIC_GROUP":
                error = walk(argument, inside_repeat=inside_repeat)
                if error:
                    return error
                continue
            return f"unsupported regular-expression construct: {name.lower()}"
        return None

    structural_error = walk(parsed)
    if structural_error:
        return structural_error, None
    adjacency_error, _ = _repeat_adjacency_summary(parsed)
    if adjacency_error:
        return adjacency_error, None
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except (re.error, OverflowError, ValueError) as exc:
        return f"invalid regular expression: {exc}", None
    return None, compiled


def route_pattern_validation_error(pattern: str) -> str | None:
    """Return a stable rejection reason, or ``None`` for an accepted pattern."""
    if not isinstance(pattern, str):
        return "pattern must be a non-empty string"
    return _validated_route_pattern(pattern)[0]


def safe_route_pattern_search(pattern: str, text: str) -> bool:
    """Search one validated expression against a bounded projection of text.

    Long requests retain both their beginning and end as independent segments;
    this keeps typical intent phrasing and explicit trailing selections without
    inventing a match across a removed middle section.
    """
    if not isinstance(pattern, str):
        return False
    validation_error, compiled = _validated_route_pattern(pattern)
    if validation_error is not None or compiled is None:
        return False
    value = str(text or "")
    if len(value) <= MAX_ROUTE_RUNTIME_TEXT_CHARS:
        segments = (value,)
    else:
        half = MAX_ROUTE_RUNTIME_TEXT_CHARS // 2
        segments = (value[:half], value[-half:])
    return any(compiled.search(segment) is not None for segment in segments)
