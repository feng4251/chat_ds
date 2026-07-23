"""Tool registry — ToolEntry-based registration, schema retrieval, and dispatch.

Enhanced from the hermes-agent pattern. Thread-safe for async FastAPI usage.
"""

from __future__ import annotations

import difflib
import hashlib
import inspect
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, get_args, get_origin, get_type_hints

from skills.route_safety import route_pattern_validation_error
from tools.context import ToolContext
from tools.omission_guard import (
    compacted_history_omission_error,
    find_compacted_history_omission_path,
)
from workspace_patterns import (
    WorkspacePatternError,
    normalize_workspace_pattern,
    workspace_pattern_matches,
)

logger = logging.getLogger(__name__)

_MAX_SCHEMA_VALIDATION_DEPTH = 32
_MAX_SCHEMA_VALIDATION_NODES = 20_000
_MAX_SCHEMA_PATTERN_INPUT_CHARS = 1_000_000
_JSON_SCHEMA_TYPES = frozenset({
    "array",
    "boolean",
    "integer",
    "null",
    "number",
    "object",
    "string",
})
# Validation keywords whose semantics are implemented by this registry.  A
# caller that must preserve a contract exactly (rather than merely consume a
# native-tool schema) can ask ``json_schema_shape_error`` to reject every
# assertion outside this set.  Common annotation-only keywords remain safe to
# carry verbatim because they do not weaken value validation.
_JSON_SCHEMA_VALIDATION_KEYWORDS = frozenset({
    "type",
    "enum",
    "const",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minProperties",
    "maxProperties",
    "pattern",
    "minimum",
    "maximum",
    "anyOf",
    "oneOf",
})
_JSON_SCHEMA_ANNOTATION_KEYWORDS = frozenset({
    "$comment",
    "$id",
    "$schema",
    "default",
    "deprecated",
    "description",
    "examples",
    "readOnly",
    "title",
    "writeOnly",
})
JSON_SCHEMA_LOSSLESS_KEYWORDS = frozenset(
    _JSON_SCHEMA_VALIDATION_KEYWORDS | _JSON_SCHEMA_ANNOTATION_KEYWORDS
)
_PATH_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _SchemaValidationLimit(RuntimeError):
    def __init__(self, kind: str, path: str):
        super().__init__(f"{kind} limit exceeded at {path}")
        self.kind = kind
        self.path = path


@dataclass
class _SchemaValidationBudget:
    nodes: int = 0

    def consume(self, path: str, *, depth: int, count: int = 1) -> None:
        if depth > _MAX_SCHEMA_VALIDATION_DEPTH:
            raise _SchemaValidationLimit("depth", path)
        self.nodes += max(1, int(count))
        if self.nodes > _MAX_SCHEMA_VALIDATION_NODES:
            raise _SchemaValidationLimit("node", path)


@dataclass(frozen=True)
class _SchemaValidationIssue:
    path: str
    message: str
    malformed_schema: bool = False


def _child_path(base: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{base}[{key}]"
    text = str(key)
    if _PATH_IDENTIFIER_RE.fullmatch(text):
        return f"{base}.{text}"
    return f"{base}[{json.dumps(text, ensure_ascii=False)}]"


def _is_json_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _is_json_number(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return False


def _value_summary(value: Any) -> str:
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, str):
        rendered = value if len(value) <= 80 else value[:77] + "..."
        return repr(rendered)
    if isinstance(value, list):
        return f"array(length={len(value)})"
    if isinstance(value, dict):
        return f"object(properties={len(value)})"
    return type(value).__name__


def _validate_schema_literal(
    value: Any,
    path: str,
    budget: _SchemaValidationBudget,
    *,
    depth: int,
    active: set[int] | None = None,
) -> _SchemaValidationIssue | None:
    """Validate and bound a JSON literal embedded in enum/const."""

    budget.consume(path, depth=depth)
    if value is None or isinstance(value, (str, bool, int)):
        return None
    if isinstance(value, float):
        if math.isfinite(value):
            return None
        return _SchemaValidationIssue(
            path,
            "must contain only finite JSON numbers",
            malformed_schema=True,
        )
    if not isinstance(value, (list, dict)):
        return _SchemaValidationIssue(
            path,
            f"contains non-JSON value of type {type(value).__name__}",
            malformed_schema=True,
        )

    active = active if active is not None else set()
    identity = id(value)
    if identity in active:
        return _SchemaValidationIssue(
            path,
            "contains a cyclic JSON literal",
            malformed_schema=True,
        )
    active.add(identity)
    try:
        if isinstance(value, list):
            for index, item in enumerate(value):
                issue = _validate_schema_literal(
                    item,
                    _child_path(path, index),
                    budget,
                    depth=depth + 1,
                    active=active,
                )
                if issue is not None:
                    return issue
            return None
        for key in sorted(value, key=lambda item: str(item)):
            if not isinstance(key, str):
                return _SchemaValidationIssue(
                    path,
                    "contains a non-string object key",
                    malformed_schema=True,
                )
            issue = _validate_schema_literal(
                value[key],
                _child_path(path, key),
                budget,
                depth=depth + 1,
                active=active,
            )
            if issue is not None:
                return issue
        return None
    finally:
        active.remove(identity)


def _nonnegative_schema_integer(
    schema: dict[str, Any],
    keyword: str,
    schema_path: str,
) -> _SchemaValidationIssue | None:
    if keyword not in schema:
        return None
    value = schema[keyword]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return _SchemaValidationIssue(
            _child_path(schema_path, keyword),
            "must be a non-negative integer",
            malformed_schema=True,
        )
    return None


def _validate_schema_shape(
    schema: Any,
    schema_path: str,
    budget: _SchemaValidationBudget,
    *,
    depth: int = 0,
    reject_unsupported_keywords: bool = False,
) -> _SchemaValidationIssue | None:
    """Validate the supported schema subset before inspecting arguments."""

    budget.consume(schema_path, depth=depth)
    if isinstance(schema, bool):
        return None
    if not isinstance(schema, dict):
        return _SchemaValidationIssue(
            schema_path,
            "must be an object or boolean schema",
            malformed_schema=True,
        )

    if reject_unsupported_keywords:
        unsupported = sorted(
            set(schema) - JSON_SCHEMA_LOSSLESS_KEYWORDS,
            key=lambda item: str(item),
        )
        if unsupported:
            keyword = unsupported[0]
            return _SchemaValidationIssue(
                _child_path(schema_path, keyword),
                (
                    "is not implemented by the bounded schema registry; "
                    "accepting it would silently weaken the declared contract"
                ),
                malformed_schema=True,
            )

    expected = schema.get("type")
    if expected is not None:
        expected_types = expected if isinstance(expected, list) else [expected]
        budget.consume(
            _child_path(schema_path, "type"),
            depth=depth + 1,
            count=len(expected_types),
        )
        if (
            not expected_types
            or any(
                not isinstance(item, str) or item not in _JSON_SCHEMA_TYPES
                for item in expected_types
            )
            or len(set(expected_types)) != len(expected_types)
        ):
            return _SchemaValidationIssue(
                _child_path(schema_path, "type"),
                "must name one or more unique supported JSON types",
                malformed_schema=True,
            )

    enum_values = schema.get("enum")
    if "enum" in schema:
        if not isinstance(enum_values, list) or not enum_values:
            return _SchemaValidationIssue(
                _child_path(schema_path, "enum"),
                "must be a non-empty array of JSON values",
                malformed_schema=True,
            )
        for index, option in enumerate(enum_values):
            issue = _validate_schema_literal(
                option,
                _child_path(_child_path(schema_path, "enum"), index),
                budget,
                depth=depth + 2,
            )
            if issue is not None:
                return issue
    if "const" in schema:
        issue = _validate_schema_literal(
            schema["const"],
            _child_path(schema_path, "const"),
            budget,
            depth=depth + 1,
        )
        if issue is not None:
            return issue

    required = schema.get("required")
    if required is not None:
        if (
            not isinstance(required, list)
            or any(not isinstance(item, str) for item in required)
            or len(set(required)) != len(required)
        ):
            return _SchemaValidationIssue(
                _child_path(schema_path, "required"),
                "must be an array of unique string property names",
                malformed_schema=True,
            )
        budget.consume(
            _child_path(schema_path, "required"),
            depth=depth + 1,
            count=len(required),
        )

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict) or any(
            not isinstance(key, str) for key in properties
        ):
            return _SchemaValidationIssue(
                _child_path(schema_path, "properties"),
                "must be an object whose keys are property names",
                malformed_schema=True,
            )
        budget.consume(
            _child_path(schema_path, "properties"),
            depth=depth + 1,
            count=len(properties),
        )
        for key in sorted(properties):
            issue = _validate_schema_shape(
                properties[key],
                _child_path(_child_path(schema_path, "properties"), key),
                budget,
                depth=depth + 2,
                reject_unsupported_keywords=reject_unsupported_keywords,
            )
            if issue is not None:
                return issue

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, (bool, dict)):
        return _SchemaValidationIssue(
            _child_path(schema_path, "additionalProperties"),
            "must be a boolean or object schema",
            malformed_schema=True,
        )
    if isinstance(additional, dict):
        issue = _validate_schema_shape(
            additional,
            _child_path(schema_path, "additionalProperties"),
            budget,
            depth=depth + 1,
            reject_unsupported_keywords=reject_unsupported_keywords,
        )
        if issue is not None:
            return issue

    items = schema.get("items")
    if items is not None and not isinstance(items, (bool, dict)):
        return _SchemaValidationIssue(
            _child_path(schema_path, "items"),
            "must be a boolean or object schema",
            malformed_schema=True,
        )
    if isinstance(items, dict):
        issue = _validate_schema_shape(
            items,
            _child_path(schema_path, "items"),
            budget,
            depth=depth + 1,
            reject_unsupported_keywords=reject_unsupported_keywords,
        )
        if issue is not None:
            return issue

    for keyword in (
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minProperties",
        "maxProperties",
    ):
        issue = _nonnegative_schema_integer(schema, keyword, schema_path)
        if issue is not None:
            return issue
    for minimum_key, maximum_key in (
        ("minItems", "maxItems"),
        ("minLength", "maxLength"),
        ("minProperties", "maxProperties"),
    ):
        if (
            minimum_key in schema
            and maximum_key in schema
            and schema[minimum_key] > schema[maximum_key]
        ):
            return _SchemaValidationIssue(
                schema_path,
                f"has {minimum_key} greater than {maximum_key}",
                malformed_schema=True,
            )

    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            return _SchemaValidationIssue(
                _child_path(schema_path, "pattern"),
                "must be a string regular expression",
                malformed_schema=True,
            )
        if pattern:
            pattern_error = route_pattern_validation_error(pattern)
            if pattern_error is not None:
                return _SchemaValidationIssue(
                    _child_path(schema_path, "pattern"),
                    f"is not a safe bounded regular expression: {pattern_error}",
                    malformed_schema=True,
                )

    for keyword in ("minimum", "maximum"):
        if keyword in schema and not _is_json_number(schema[keyword]):
            return _SchemaValidationIssue(
                _child_path(schema_path, keyword),
                "must be a finite JSON number",
                malformed_schema=True,
            )
    if (
        "minimum" in schema
        and "maximum" in schema
        and schema["minimum"] > schema["maximum"]
    ):
        return _SchemaValidationIssue(
            schema_path,
            "has minimum greater than maximum",
            malformed_schema=True,
        )

    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, list) or not branches:
            return _SchemaValidationIssue(
                _child_path(schema_path, keyword),
                "must be a non-empty array of schemas",
                malformed_schema=True,
            )
        budget.consume(
            _child_path(schema_path, keyword),
            depth=depth + 1,
            count=len(branches),
        )
        for index, branch in enumerate(branches):
            issue = _validate_schema_shape(
                branch,
                _child_path(_child_path(schema_path, keyword), index),
                budget,
                depth=depth + 2,
                reject_unsupported_keywords=reject_unsupported_keywords,
            )
            if issue is not None:
                return issue
    return None


def _bounded_json_equal(
    left: Any,
    right: Any,
    path: str,
    budget: _SchemaValidationBudget,
) -> bool:
    """Compare JSON values without Python's unbounded recursive equality."""

    stack: list[tuple[Any, Any, str, int]] = [(left, right, path, 0)]
    while stack:
        lhs, rhs, current_path, depth = stack.pop()
        budget.consume(current_path, depth=depth)
        if isinstance(lhs, bool) or isinstance(rhs, bool):
            if not (isinstance(lhs, bool) and isinstance(rhs, bool) and lhs == rhs):
                return False
            continue
        if _is_json_number(lhs) or _is_json_number(rhs):
            if not (_is_json_number(lhs) and _is_json_number(rhs) and lhs == rhs):
                return False
            continue
        if lhs is None or rhs is None:
            if lhs is not None or rhs is not None:
                return False
            continue
        if isinstance(lhs, str) or isinstance(rhs, str):
            if not (isinstance(lhs, str) and isinstance(rhs, str) and lhs == rhs):
                return False
            continue
        if isinstance(lhs, list) or isinstance(rhs, list):
            if not (
                isinstance(lhs, list)
                and isinstance(rhs, list)
                and len(lhs) == len(rhs)
            ):
                return False
            for index in range(len(lhs) - 1, -1, -1):
                stack.append((
                    lhs[index],
                    rhs[index],
                    _child_path(current_path, index),
                    depth + 1,
                ))
            continue
        if isinstance(lhs, dict) or isinstance(rhs, dict):
            if not (isinstance(lhs, dict) and isinstance(rhs, dict)):
                return False
            if any(not isinstance(key, str) for key in lhs) or any(
                not isinstance(key, str) for key in rhs
            ):
                return False
            if set(lhs) != set(rhs):
                return False
            for key in sorted(lhs, reverse=True):
                stack.append((
                    lhs[key],
                    rhs[key],
                    _child_path(current_path, key),
                    depth + 1,
                ))
            continue
        return False
    return True


def _validate_json_schema_value(
    value: Any,
    schema: Any,
    value_path: str,
    schema_path: str,
    budget: _SchemaValidationBudget,
    *,
    depth: int = 0,
) -> _SchemaValidationIssue | None:
    """Validate one argument value against the already-shaped schema subset."""

    budget.consume(value_path, depth=depth)
    if schema is True:
        return None
    if schema is False:
        return _SchemaValidationIssue(value_path, "is forbidden by the schema")
    # _validate_schema_shape guarantees this for every reachable branch.
    if not isinstance(schema, dict):  # pragma: no cover - defensive fallback.
        return _SchemaValidationIssue(
            schema_path,
            "must be an object or boolean schema",
            malformed_schema=True,
        )

    expected = schema.get("type")
    if expected is not None:
        expected_types = expected if isinstance(expected, list) else [expected]
        if not any(_json_type_matches(value, item) for item in expected_types):
            rendered = " or ".join(expected_types)
            return _SchemaValidationIssue(
                value_path,
                f"must be {rendered}; got {type(value).__name__}",
            )

    if "enum" in schema:
        matches = False
        for index, option in enumerate(schema["enum"]):
            if _bounded_json_equal(
                value,
                option,
                _child_path(_child_path(schema_path, "enum"), index),
                budget,
            ):
                matches = True
                break
        if not matches:
            return _SchemaValidationIssue(
                value_path,
                f"must be one of the schema enum values; got {_value_summary(value)}",
            )
    if "const" in schema and not _bounded_json_equal(
        value,
        schema["const"],
        _child_path(schema_path, "const"),
        budget,
    ):
        return _SchemaValidationIssue(
            value_path,
            f"must equal the schema const; got {_value_summary(value)}",
        )

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return _SchemaValidationIssue(
                value_path,
                f"must contain at least {schema['minLength']} characters; got {len(value)}",
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return _SchemaValidationIssue(
                value_path,
                f"must contain at most {schema['maxLength']} characters; got {len(value)}",
            )
        if "pattern" in schema:
            if len(value) > _MAX_SCHEMA_PATTERN_INPUT_CHARS:
                return _SchemaValidationIssue(
                    value_path,
                    "exceeds the bounded input size for pattern validation",
                )
            pattern = schema["pattern"]
            if re.search(pattern, value) is None:
                return _SchemaValidationIssue(
                    value_path,
                    f"must match pattern {pattern!r}",
                )

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return _SchemaValidationIssue(
                value_path,
                f"must contain at least {schema['minItems']} items; got {len(value)}",
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return _SchemaValidationIssue(
                value_path,
                f"must contain at most {schema['maxItems']} items; got {len(value)}",
            )
        if "items" in schema:
            for index, item in enumerate(value):
                issue = _validate_json_schema_value(
                    item,
                    schema["items"],
                    _child_path(value_path, index),
                    _child_path(schema_path, "items"),
                    budget,
                    depth=depth + 1,
                )
                if issue is not None:
                    return issue

    if isinstance(value, dict):
        non_string_keys = [key for key in value if not isinstance(key, str)]
        if non_string_keys:
            return _SchemaValidationIssue(
                value_path,
                "must use string keys for every JSON object property",
            )
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            return _SchemaValidationIssue(
                value_path,
                f"must contain at least {schema['minProperties']} properties; got {len(value)}",
            )
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            return _SchemaValidationIssue(
                value_path,
                f"must contain at most {schema['maxProperties']} properties; got {len(value)}",
            )
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                return _SchemaValidationIssue(
                    _child_path(value_path, key),
                    "is required by the schema",
                )
        additional = schema.get("additionalProperties", True)
        for key in sorted(value):
            if key in properties:
                child_schema = properties[key]
                child_schema_path = _child_path(
                    _child_path(schema_path, "properties"),
                    key,
                )
            elif additional is False:
                return _SchemaValidationIssue(
                    _child_path(value_path, key),
                    "is an unexpected property",
                )
            elif isinstance(additional, dict):
                child_schema = additional
                child_schema_path = _child_path(schema_path, "additionalProperties")
            else:
                continue
            issue = _validate_json_schema_value(
                value[key],
                child_schema,
                _child_path(value_path, key),
                child_schema_path,
                budget,
                depth=depth + 1,
            )
            if issue is not None:
                return issue

    if _is_json_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            return _SchemaValidationIssue(
                value_path,
                f"must be at least {schema['minimum']}; got {value}",
            )
        if "maximum" in schema and value > schema["maximum"]:
            return _SchemaValidationIssue(
                value_path,
                f"must be at most {schema['maximum']}; got {value}",
            )

    any_of = schema.get("anyOf")
    if any_of is not None:
        first_issue: _SchemaValidationIssue | None = None
        for index, branch in enumerate(any_of):
            issue = _validate_json_schema_value(
                value,
                branch,
                value_path,
                _child_path(_child_path(schema_path, "anyOf"), index),
                budget,
                depth=depth + 1,
            )
            if issue is None:
                break
            if first_issue is None:
                first_issue = issue
        else:
            detail = f"; first mismatch: {first_issue.message}" if first_issue else ""
            return _SchemaValidationIssue(
                value_path,
                f"must match at least one anyOf branch{detail}",
            )

    one_of = schema.get("oneOf")
    if one_of is not None:
        matched = 0
        first_issue: _SchemaValidationIssue | None = None
        for index, branch in enumerate(one_of):
            issue = _validate_json_schema_value(
                value,
                branch,
                value_path,
                _child_path(_child_path(schema_path, "oneOf"), index),
                budget,
                depth=depth + 1,
            )
            if issue is None:
                matched += 1
            elif first_issue is None:
                first_issue = issue
        if matched != 1:
            detail = (
                f"; first mismatch: {first_issue.message}"
                if matched == 0 and first_issue is not None
                else ""
            )
            return _SchemaValidationIssue(
                value_path,
                f"must match exactly one oneOf branch; matched {matched}{detail}",
            )
    return None


def json_schema_shape_error(
    schema: Any,
    *,
    schema_path: str = "schema",
    reject_unsupported_keywords: bool = False,
) -> str | None:
    """Validate the registry's bounded JSON-Schema subset for reuse.

    Skill result contracts and native tool arguments must not drift into two
    subtly different schema dialects. This public, side-effect-free wrapper is
    the single shape-validation boundary for both callers.
    """
    budget = _SchemaValidationBudget()
    try:
        issue = _validate_schema_shape(
            schema,
            schema_path,
            budget,
            reject_unsupported_keywords=reject_unsupported_keywords,
        )
    except _SchemaValidationLimit as exc:
        return (
            f"schema validation exceeded the bounded {exc.kind} limit at "
            f"{exc.path}"
        )
    if issue is None:
        return None
    return f"{issue.path}: {issue.message}"


def json_schema_value_error(
    value: Any,
    schema: Any,
    *,
    value_path: str = "value",
    schema_path: str = "schema",
) -> str | None:
    """Validate one JSON value with the registry's bounded schema subset."""
    budget = _SchemaValidationBudget()
    try:
        issue = _validate_schema_shape(schema, schema_path, budget)
        if issue is None:
            issue = _validate_json_schema_value(
                value,
                schema,
                value_path,
                schema_path,
                budget,
            )
    except _SchemaValidationLimit as exc:
        return (
            f"schema validation exceeded the bounded {exc.kind} limit at "
            f"{exc.path}"
        )
    if issue is None:
        return None
    return f"{issue.path}: {issue.message}"


@dataclass(frozen=True)
class ToolPreflightResult:
    """Pure validation result for one native tool call.

    ``args`` contains the normalized arguments at the point where validation
    stopped. ``semantic_args`` is an audit-only projection after aliases and
    runtime-owned fields are normalized and schema-unknown fields are removed;
    it is never used for boundary checks or dispatch. A successful result's
    ``args`` remains the exact runtime-safe handler payload.
    Preflight never invokes ``check_fn`` or the handler and therefore can be
    used to validate a complete declared-workflow batch before any call in the
    batch is dispatched.
    """

    name: str
    args: Any
    semantic_args: Any = None
    ignored_args: tuple[str, ...] = ()
    error_payload: dict[str, Any] | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_payload is None

    def error_json(self) -> str:
        return json.dumps(self.error_payload or {}, ensure_ascii=False)


def _canonical_boundary_path(value: Any, *, workspace_alias: bool = False) -> str | None:
    """Return the exact safe relative path used by a delegated read boundary."""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return None
    normalized = value
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if workspace_alias and normalized.startswith("workspace/"):
        normalized = normalized[len("workspace/"):]
    path = PurePosixPath(normalized)
    if path.is_absolute() or not normalized or normalized == "." or ".." in path.parts:
        return None
    return str(path)


def _primary_selected_skill_manifest_allows(
    context: ToolContext,
    skill_name: str,
    resource_path: str,
) -> bool:
    """Revalidate one primary read against the current canonical inventory.

    This is a read-only progressive-disclosure authority.  It is deliberately
    unavailable to delegates and is never consulted for copy or execution.
    Package selection alone is insufficient: the main Skill must already have
    produced a successful view receipt in this run.
    """
    if (
        context.agent_kind != "primary"
        or context.delegated_resource_boundary
        or not context.skill_execution_resource_boundary
        or skill_name not in set(context.selected_skill_browse_roots)
        or (skill_name, "SKILL.md") not in set(context.allowed_skill_resources)
    ):
        return False
    if resource_path == "__manifest__":
        return (skill_name, "__manifest__") in set(context.allowed_skill_resources)
    if resource_path in {"", "SKILL.md"}:
        return resource_path == "SKILL.md"
    try:
        from skills.loader import load_skill_content
        from skills.scanner import resolve_skill_path

        skill_md = resolve_skill_path(
            skill_name,
            context.user_id,
            context.session_id,
            enabled_user_skills=list(context.enabled_user_skills),
        )
        if skill_md is None:
            return False
        loaded = load_skill_content(
            skill_md,
            skill_dir=str(skill_md.parent),
            session_id=context.session_id,
        )
        if loaded.get("error") or loaded.get("name") != skill_name:
            return False
        linked_files = loaded.get("linked_files")
        if not isinstance(linked_files, dict):
            return False
        return any(
            resource_path == candidate
            for paths in linked_files.values()
            if isinstance(paths, list)
            for candidate in paths
            if isinstance(candidate, str)
        )
    except (OSError, RuntimeError, ValueError):
        return False


def delegated_resource_boundary_error(
    name: str,
    args: Any,
    context: ToolContext | None,
) -> str | None:
    """Fail closed when a compiled child tries to read outside its task closure.

    Ordinary chat turns and ad-hoc delegates do not opt into this boundary.  A
    compiled workflow child does, after the parent has deterministically
    preloaded every declared dependency.  The boundary is deliberately checked
    at registry dispatch as well as prompt level so a model cannot widen it by
    inventing arguments or by using the deferred-tool wrapper.
    """
    runner_tools = {
        "run_skill_process", "run_skill_python", "run_skill_script",
    }
    if context is None:
        if name in runner_tools:
            return (
                "Skill script execution requires a runtime-owned exact script "
                "grant; a public runner schema is not executable authority."
            )
        return None
    if name not in runner_tools and not (
        context.delegated_resource_boundary
        or context.skill_execution_resource_boundary
    ):
        return None
    bounded_resource_tools = {
        "skill_view", "skill_copy_resource", "read_file",
        "run_skill_process", "run_skill_python", "run_skill_script",
    }
    if name not in bounded_resource_tools:
        return None
    if not isinstance(args, dict):
        return (
            "Delegated resource boundary rejected malformed reader arguments; "
            "only exact resources declared by the compiled task are authorized."
        )

    if name in runner_tools:
        if (
            name == "run_skill_process"
            and str(args.get("operation") or "") != "start"
        ):
            # A process handle is runtime-issued, owner-scoped authority. The
            # process tool must revalidate that opaque lease and op_id; package
            # path authorization applies only to start.
            return None
        raw_script_path = _canonical_boundary_path(args.get("script_path"))
        if (
            raw_script_path is None
            or not raw_script_path.startswith("skills/")
        ):
            return (
                "Delegated resource boundary requires an auditable full "
                "Skill script path: skills/<declared-skill>/<relative-script>."
            )
        relative_script = raw_script_path[len("skills/"):]
        allowed = set(context.allowed_skill_scripts)
        allowed_skill_names = sorted(
            {
                str(skill_name)
                for skill_name, _resource_path, _digest in allowed
                if str(skill_name)
            },
            key=len,
            reverse=True,
        )
        for skill_name in allowed_skill_names:
            prefix = f"{skill_name}/"
            if not relative_script.startswith(prefix):
                continue
            script_resource = relative_script[len(prefix):]
            grants = [
                digest
                for granted_skill, granted_path, digest in allowed
                if granted_skill == skill_name and granted_path == script_resource
            ]
            if grants:
                try:
                    from skills.path_safety import validate_skill_resource
                    from skills.scanner import resolve_skill_path

                    skill_md = resolve_skill_path(
                        skill_name,
                        context.user_id,
                        context.session_id,
                        enabled_user_skills=list(context.enabled_user_skills),
                    )
                    if skill_md is None:
                        raise ValueError("canonical Skill is unavailable")
                    package_root = skill_md.parent.resolve(strict=True)
                    checked = validate_skill_resource(
                        package_root,
                        script_resource,
                        expected_kind="file",
                        require_relative=True,
                    )
                    if checked.valid and checked.path is not None:
                        actual = hashlib.sha256(checked.path.read_bytes()).hexdigest()
                        if actual in grants:
                            exact_grant = (
                                skill_name,
                                script_resource,
                                actual,
                            )
                            if (
                                name in {
                                    "run_skill_python",
                                    "run_skill_script",
                                }
                                and exact_grant
                                in set(
                                    context.process_only_skill_scripts
                                )
                            ):
                                return (
                                    "This exact Skill entrypoint is bound to "
                                    "the browser-automation runtime and may be "
                                    "executed only through run_skill_process; "
                                    "the one-shot/base executor is not an "
                                    "authorized fallback."
                                )
                            package_grants = {
                                package_digest
                                for granted_skill, package_digest
                                in context.allowed_skill_package_digests
                                if granted_skill == skill_name
                            }
                            if name == "run_skill_process" or package_grants:
                                if not package_grants:
                                    return (
                                        "Persistent Skill execution requires "
                                        "a runtime-owned complete package "
                                        "digest. Recompile the exact Skill "
                                        "before starting it."
                                    )
                                from tools.isolated_skill_executor import (
                                    compute_skill_package_digest,
                                )

                                package_digest = compute_skill_package_digest(
                                    package_root
                                )
                                if package_digest not in package_grants:
                                    return (
                                        f"Delegated resource boundary rejected "
                                        f"{name} because the authorized Skill "
                                        "package changed after compilation: "
                                        f"{raw_script_path}. Recompile the "
                                        "Skill before executing it."
                                    )
                            authorities = [
                                row
                                for row in context.allowed_skill_script_authorities
                                if (
                                    len(row) == 6
                                    and row[0] == skill_name
                                    and row[4] == script_resource
                                    and row[5] == actual
                                )
                            ]
                            if not authorities:
                                if name == "run_skill_process":
                                    return (
                                        "Persistent Skill execution requires "
                                        "the exact root/reference/script "
                                        "authority chain. Recompile the Skill "
                                        "before starting it."
                                    )
                                return None
                            current_root_digest = hashlib.sha256(
                                skill_md.read_bytes()
                            ).hexdigest()
                            for (
                                _granted_skill,
                                root_digest,
                                declaring_resource,
                                declaring_digest,
                                _script_resource,
                                _script_digest,
                            ) in authorities:
                                if current_root_digest != root_digest:
                                    continue
                                declaring = validate_skill_resource(
                                    package_root,
                                    declaring_resource,
                                    expected_kind="file",
                                    require_relative=True,
                                )
                                if (
                                    declaring.valid
                                    and declaring.path is not None
                                    and hashlib.sha256(
                                        declaring.path.read_bytes()
                                    ).hexdigest() == declaring_digest
                                ):
                                    return None
                except (OSError, RuntimeError, ValueError):
                    pass
                return (
                    f"Delegated resource boundary rejected {name} because the "
                    f"authorized Skill script changed after compilation: "
                    f"{raw_script_path}. Recompile the Skill before executing it."
                )
            break
        return (
            f"Delegated resource boundary rejected {name} outside the "
            f"compiled Skill/script capability closure: {raw_script_path}. "
            "Use only an exact parent-Skill script declared for this task or "
            "a script owned by an explicitly declared capability Skill."
        )

    if name in {"skill_view", "skill_copy_resource"}:
        skill_name = args.get("name")
        raw_path = (
            args.get("file_path")
            if name == "skill_view"
            else args.get("source_path")
        )
        # skill_view(name) and skill_view(name, file_path='SKILL.md') address
        # the same main resource for boundary purposes.
        if name == "skill_view" and (raw_path is None or raw_path == ""):
            raw_path = "SKILL.md"
        resource_path = _canonical_boundary_path(raw_path)
        requested = (
            str(skill_name) if isinstance(skill_name, str) else "",
            resource_path or "",
        )
        allowed = set(context.allowed_skill_resources)
        if resource_path is not None and requested in allowed:
            # A manifest is package-wide discovery authority, so even an exact
            # manifest tuple is dormant until the selected main was viewed.
            if name != "skill_view" or resource_path != "__manifest__":
                return None
            if (
                not context.delegated_resource_boundary
                and _primary_selected_skill_manifest_allows(
                    context, requested[0], resource_path,
                )
            ):
                return None
        if (
            name == "skill_view"
            and resource_path is not None
            and _primary_selected_skill_manifest_allows(
                context, requested[0], resource_path,
            )
        ):
            return None
        rendered = f"{requested[0]}/{requested[1] or '<invalid>'}"
        return (
            f"Delegated resource boundary rejected {name} outside the "
            f"compiled task closure: {rendered}. The harness already preloaded "
            "the exact worker/resources/capability Skill mains declared for "
            "this child; parent Skill manifests and undeclared Skill files are "
            "not authorized."
        )

    # The selected-Skill boundary closes only Skill package resources and
    # executables.  A primary Skill execution may still need user-provided
    # workspace inputs; the stricter read closure is specific to compiled
    # delegated children whose prerequisites were deterministically preloaded.
    if not context.delegated_resource_boundary:
        return None

    read_path = _canonical_boundary_path(args.get("filepath"), workspace_alias=True)
    allowed_paths = set(context.allowed_read_paths)
    if read_path is not None and read_path in allowed_paths:
        return None
    return (
        "Delegated resource boundary rejected read_file outside the compiled "
        f"task closure: {read_path or '<invalid>'}. Only exact declared "
        "persisted prerequisite paths are authorized; arbitrary workspace and "
        "historical tool-result reads are not authorized."
    )


def declared_command_boundary_error(
    name: str,
    args: Any,
    context: ToolContext | None,
) -> str | None:
    """Recompile and verify every declared command at dispatch time.

    Unlike reader boundaries this is unconditional: possession of the public
    tool schema is never command authority, for either root or delegated runs.
    """
    if name != "run_declared_command":
        return None
    if context is None or not isinstance(args, dict):
        return (
            "Declared command dispatch requires a runtime-owned compiled Skill "
            "capability context."
        )
    skill_name = args.get("skill_name")
    command_id = args.get("command_id")
    allowed = [
        item for item in context.allowed_skill_commands
        if item[0] == skill_name and item[1] == command_id
    ]
    if len(allowed) != 1:
        return (
            "Declared command dispatch rejected a grant outside this run's "
            "exact compiled Skill capability closure."
        )
    try:
        from skills.command_grants import (
            grant_tuple,
            load_current_skill_command_grants,
        )

        _root, _loaded, current = load_current_skill_command_grants(
            str(skill_name),
            context.user_id,
            context.session_id,
            list(context.enabled_user_skills),
        )
    except (OSError, RuntimeError, ValueError):
        return (
            "Declared command dispatch could not recompile the current session "
            "Skill and failed closed."
        )
    if not any(grant_tuple(str(skill_name), grant) == allowed[0] for grant in current):
        return (
            "Declared command dispatch rejected a grant that changed after "
            "compilation. Recompile the Skill before executing it."
        )
    return None


def delegated_artifact_write_boundary_error(
    name: str,
    args: Any,
    context: ToolContext | None,
) -> str | None:
    """Reject direct child writes outside its compiled artifact subset.

    Artifact receipts are still verified after execution, but post-hoc receipt
    rejection cannot undo a write to the shared session workspace.  The
    synthesis boundary therefore checks every direct path-bearing writer before
    handler dispatch.  Declared scripts/commands retain their separate exact
    executable grants and emitted-artifact audit because their output paths are
    not model arguments.
    """

    if context is None or not context.artifact_write_boundary:
        return None
    path_fields = {
        "write_file": "filepath",
        "patch_file": "filepath",
        "skill_copy_resource": "destination_path",
        "merge_files": "output_filepath",
    }
    field_name = path_fields.get(name)
    if field_name is None:
        return None
    if not isinstance(args, dict):
        return (
            "Delegated artifact write boundary rejected malformed writer "
            "arguments; only compiled output paths are authorized."
        )
    relative = _canonical_boundary_path(
        args.get(field_name),
        workspace_alias=True,
    )
    if relative is None:
        return (
            "Delegated artifact write boundary requires one safe exact "
            f"workspace-relative {field_name}."
        )
    allowed_patterns: list[str] = []
    for raw_pattern in context.allowed_artifact_write_patterns:
        try:
            allowed_patterns.append(normalize_workspace_pattern(raw_pattern))
        except WorkspacePatternError:
            return (
                "Delegated artifact write boundary contains an invalid "
                "runtime-owned output pattern and failed closed."
            )
    def path_is_allowed(candidate: str) -> bool:
        if candidate in {
            normalized
            for raw in context.allowed_read_paths
            if (
                normalized := _canonical_boundary_path(
                    raw,
                    workspace_alias=True,
                )
            ) is not None
        }:
            return True
        return any(
            workspace_pattern_matches(candidate, pattern)
            for pattern in allowed_patterns
        )

    if not allowed_patterns or not any(
        workspace_pattern_matches(relative, pattern)
        for pattern in allowed_patterns
    ):
        return (
            "Delegated artifact write boundary rejected an undeclared output "
            f"path: {relative}."
        )
    if name == "merge_files":
        raw_inputs = args.get("input_files")
        if raw_inputs is not None:
            if not isinstance(raw_inputs, list):
                return (
                    "Delegated artifact write boundary requires merge input_files "
                    "to be an explicit list."
                )
            for raw_input in raw_inputs:
                input_path = _canonical_boundary_path(
                    raw_input,
                    workspace_alias=True,
                )
                if input_path is None or not path_is_allowed(input_path):
                    return (
                        "Delegated artifact write boundary rejected an undeclared "
                        f"merge input path: {input_path or '<invalid>'}."
                    )
        raw_input_patterns = args.get("patterns")
        if raw_input_patterns is not None:
            if not isinstance(raw_input_patterns, list):
                return (
                    "Delegated artifact write boundary requires merge patterns "
                    "to be an explicit list."
                )
            for raw_input_pattern in raw_input_patterns:
                try:
                    input_pattern = normalize_workspace_pattern(
                        raw_input_pattern
                    )
                except WorkspacePatternError:
                    input_pattern = ""
                if input_pattern not in allowed_patterns:
                    return (
                        "Delegated artifact write boundary rejected a merge input "
                        f"pattern not owned by the runtime: "
                        f"{input_pattern or '<invalid>'}."
                    )
    return None


def _is_tool_context_annotation(annotation: Any) -> bool:
    if annotation is inspect.Signature.empty:
        return False
    if annotation is ToolContext:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    return any(arg is ToolContext for arg in get_args(annotation))


def _find_omission_path(value: Any, path: str = "args") -> str | None:
    """Backward-compatible alias for the shared omission-path detector."""
    return find_compacted_history_omission_path(value, path)


# ── ToolEntry ──────────────────────────────────────────────────────────────

class ToolEntry:
    """Metadata for a single registered tool."""

    __slots__ = (
        "name", "toolset", "schema", "handler", "check_fn",
        "args_preflight_fn",
        "is_async", "description", "emoji",
        "accepts_context", "accepts_user_id", "accepts_session_id",
        "accepts_enabled_user_skills", "is_read_only", "is_destructive",
        "parallel_safe", "path_scoped", "allow_in_child",
        "allow_in_parallel_child", "mutates_workspace", "mutates_global_state",
        "requires_user_visibility", "salvage_safe", "external_interaction",
    )

    def __init__(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable | None = None,
        args_preflight_fn: Callable | None = None,
        is_async: bool = True,
        description: str = "",
        emoji: str = "",
        is_read_only: bool = False,
        is_destructive: bool = False,
        parallel_safe: bool = False,
        path_scoped: bool = False,
        allow_in_child: bool = True,
        allow_in_parallel_child: bool | None = None,
        mutates_workspace: bool | None = None,
        mutates_global_state: bool = False,
        requires_user_visibility: bool = False,
        salvage_safe: bool = False,
        external_interaction: bool = False,
    ):
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.handler = handler
        self.check_fn = check_fn
        self.args_preflight_fn = args_preflight_fn
        self.is_async = is_async
        self.description = description or schema.get("description", "")
        self.emoji = emoji
        try:
            signature = inspect.signature(handler)
            params = signature.parameters
            type_hints = get_type_hints(handler)
        except (TypeError, ValueError):
            params = {}
            type_hints = {}
        self.accepts_context = any(
            name == "context" and _is_tool_context_annotation(type_hints.get(name, param.annotation))
            for name, param in params.items()
        )
        self.accepts_user_id = "user_id" in params
        self.accepts_session_id = "session_id" in params
        self.accepts_enabled_user_skills = "enabled_user_skills" in params
        self.is_read_only = is_read_only
        self.is_destructive = is_destructive
        self.parallel_safe = parallel_safe
        self.path_scoped = path_scoped
        self.allow_in_child = allow_in_child
        self.allow_in_parallel_child = parallel_safe if allow_in_parallel_child is None else allow_in_parallel_child
        self.mutates_workspace = (not is_read_only and path_scoped) if mutates_workspace is None else mutates_workspace
        self.mutates_global_state = mutates_global_state
        self.requires_user_visibility = requires_user_visibility
        # ``read_only`` describes the tool's data mutation contract.  It does
        # not by itself make a call safe to recover from a structurally corrupt
        # provider batch: network reads, browser navigation, and other
        # externally observable operations may still be costly or stateful.
        # Corrupt-batch salvage is therefore an explicit, narrower opt-in.
        self.salvage_safe = bool(salvage_safe)
        self.external_interaction = bool(external_interaction)


# ── ToolRegistry ───────────────────────────────────────────────────────────

class ToolRegistry:
    """Singleton registry that collects tool schemas + handlers from tool files."""

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}

    # ── Registration ──────────────────────────────────────────────────

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable | None = None,
        args_preflight_fn: Callable | None = None,
        is_async: bool = True,
        description: str = "",
        emoji: str = "",
        is_read_only: bool = False,
        is_destructive: bool = False,
        parallel_safe: bool = False,
        path_scoped: bool = False,
        allow_in_child: bool = True,
        allow_in_parallel_child: bool | None = None,
        mutates_workspace: bool | None = None,
        mutates_global_state: bool = False,
        requires_user_visibility: bool = False,
        salvage_safe: bool = False,
        external_interaction: bool = False,
    ):
        """Register a tool. Called at module-import time by each tool file."""
        existing = self._tools.get(name)
        if existing and existing.toolset != toolset:
            logger.warning(
                "Tool '%s' re-registered: toolset '%s' replacing '%s'",
                name, toolset, existing.toolset,
            )
        self._tools[name] = ToolEntry(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            args_preflight_fn=args_preflight_fn,
            is_async=is_async,
            description=description,
            emoji=emoji,
            is_read_only=is_read_only,
            is_destructive=is_destructive,
            parallel_safe=parallel_safe,
            path_scoped=path_scoped,
            allow_in_child=allow_in_child,
            allow_in_parallel_child=allow_in_parallel_child,
            mutates_workspace=mutates_workspace,
            mutates_global_state=mutates_global_state,
            requires_user_visibility=requires_user_visibility,
            salvage_safe=salvage_safe,
            external_interaction=external_interaction,
        )

    def deregister(self, name: str) -> None:
        """Remove a tool from the registry."""
        self._tools.pop(name, None)

    # ── Schema retrieval ──────────────────────────────────────────────

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        """Return a registered tool entry by name, or None."""
        return self._tools.get(name)

    def get_definitions(self, tool_names: list[str]) -> list[dict]:
        """Return OpenAI-format tool schemas for the requested tool names.

        Only tools whose ``check_fn()`` returns True (or have no check_fn)
        are included.
        """
        result = []
        for name in tool_names:
            entry = self._tools.get(name)
            if not entry:
                continue
            if entry.check_fn:
                try:
                    if not entry.check_fn():
                        continue
                except Exception:
                    continue
            schema_with_name = {**entry.schema, "name": entry.name}
            parameters = entry.schema.get("parameters")
            if isinstance(parameters, dict):
                parameters = dict(parameters)
                properties = parameters.get("properties")
                if (
                    isinstance(properties, dict)
                    and properties
                    and "additionalProperties" not in parameters
                ):
                    parameters["additionalProperties"] = False
                schema_with_name["parameters"] = parameters
            result.append({"type": "function", "function": schema_with_name})
        return result

    def get_all_names(self) -> list[str]:
        """Return sorted list of all registered tool names."""
        return sorted(self._tools.keys())

    def get_schema(self, name: str) -> Optional[dict]:
        """Return a tool's raw schema dict (no check_fn filtering)."""
        entry = self._tools.get(name)
        return entry.schema if entry else None

    def get_metadata(self, name: str) -> Optional[dict]:
        """Return harness-only execution metadata for a tool."""
        entry = self._tools.get(name)
        if not entry:
            return None
        return {
            "read_only": entry.is_read_only,
            "destructive": entry.is_destructive,
            "parallel_safe": entry.parallel_safe,
            "path_scoped": entry.path_scoped,
            "allow_in_child": entry.allow_in_child,
            "allow_in_parallel_child": entry.allow_in_parallel_child,
            "mutates_workspace": entry.mutates_workspace,
            "mutates_global_state": entry.mutates_global_state,
            "requires_user_visibility": entry.requires_user_visibility,
            "salvage_safe": entry.salvage_safe,
            "external_interaction": entry.external_interaction,
            "toolset": entry.toolset,
        }

    def get_emoji(self, name: str, default: str = "⚡") -> str:
        """Return the emoji for a tool, or *default* if unset."""
        entry = self._tools.get(name)
        return (entry.emoji if entry and entry.emoji else default)

    def _validate_args(self, entry: ToolEntry, args: Any) -> str | None:
        if not isinstance(args, dict):
            return f"Tool {entry.name} arguments must be a JSON object; got {type(args).__name__}."
        if "_raw_args" in args:
            return (
                f"Tool {entry.name} received reserved field _raw_args. "
                f"Retry with valid JSON matching this schema: {self._schema_hint(entry)}"
            )
        if "__tool_arg_parse_error" in args:
            return (
                f"Tool {entry.name} arguments were malformed JSON: "
                f"{str(args.get('__tool_arg_parse_error'))[:300]}. "
                f"Retry with valid JSON matching this schema: {self._schema_hint(entry)}"
            )

        raw_schema = entry.schema
        if not isinstance(raw_schema, dict):
            return (
                f"Tool {entry.name} has invalid argument schema at schema: "
                "tool schema must be an object."
            )
        params = raw_schema.get("parameters", {})
        if not isinstance(params, (dict, bool)):
            return (
                f"Tool {entry.name} has invalid argument schema at "
                "schema.parameters: must be an object or boolean schema."
            )
        # OpenAI function arguments are always objects.  Preserve the existing
        # top-level safety default advertised by get_definitions while nested
        # objects follow JSON Schema's standard additionalProperties=True
        # default unless their own schema explicitly says otherwise.
        validation_schema: Any = params
        if isinstance(params, dict):
            validation_schema = dict(params)
            properties = validation_schema.get("properties")
            if (
                isinstance(properties, dict)
                and properties
                and "additionalProperties" not in validation_schema
            ):
                validation_schema["additionalProperties"] = False

        budget = _SchemaValidationBudget()
        try:
            issue = _validate_schema_shape(
                validation_schema,
                "schema.parameters",
                budget,
            )
            if issue is None:
                issue = _validate_json_schema_value(
                    args,
                    validation_schema,
                    "args",
                    "schema.parameters",
                    budget,
                )
        except _SchemaValidationLimit as exc:
            return (
                f"Tool {entry.name} argument schema validation exceeded the "
                f"bounded {exc.kind} limit at {exc.path}; request rejected."
            )
        if issue is not None:
            if issue.malformed_schema:
                return (
                    f"Tool {entry.name} has invalid argument schema at "
                    f"{issue.path}: {issue.message}."
                )
            return f"Tool {entry.name} field {issue.path} {issue.message}."
        return None

    def _schema_hint(self, entry: ToolEntry) -> dict:
        raw_schema = entry.schema if isinstance(entry.schema, dict) else {}
        params = raw_schema.get("parameters")
        params = params if isinstance(params, dict) else {}
        properties = params.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = params.get("required")
        required = required if isinstance(required, list) else []
        return {
            "required": [
                item for item in required[:64] if isinstance(item, str)
            ],
            "properties": {
                key: value.get("type", "any") if isinstance(value, dict) else "any"
                for key, value in sorted(
                    properties.items(),
                    key=lambda item: str(item[0]),
                )[:64]
                if isinstance(key, str)
            },
        }

    @staticmethod
    def _matches_json_type(value: Any, expected: Any) -> bool:
        if isinstance(expected, list):
            return any(ToolRegistry._matches_json_type(value, item) for item in expected)
        return isinstance(expected, str) and _json_type_matches(value, expected)

    @staticmethod
    def _strip_context_owned_args(entry: ToolEntry, args: Any) -> Any:
        if not isinstance(args, dict):
            return args
        stripped = dict(args)
        if entry.accepts_user_id:
            stripped.pop("user_id", None)
        if entry.accepts_session_id:
            stripped.pop("session_id", None)
        if entry.accepts_enabled_user_skills:
            stripped.pop("enabled_user_skills", None)
        if entry.accepts_context:
            stripped.pop("context", None)
        return stripped

    @staticmethod
    def _normalize_alias_args(entry: ToolEntry, args: Any) -> Any:
        if not isinstance(args, dict):
            return args
        if entry.name == "search_files" and "context" in args and "context_lines" not in args:
            normalized = dict(args)
            normalized["context_lines"] = normalized.pop("context")
            return normalized
        if entry.name in {"read_file", "write_file", "patch_file"} and "filepath" not in args:
            for alias in ("file_path", "path", "filename"):
                if alias in args:
                    normalized = dict(args)
                    normalized["filepath"] = normalized.pop(alias)
                    return normalized
        return args

    @staticmethod
    def _strip_unexpected_args(entry: ToolEntry, args: Any) -> tuple[Any, list[str]]:
        if not isinstance(args, dict):
            return args, []
        raw_schema = entry.schema if isinstance(entry.schema, dict) else {}
        params = raw_schema.get("parameters")
        if not isinstance(params, dict):
            return args, []
        properties = params.get("properties")
        if not isinstance(properties, dict) or any(
            not isinstance(key, str) for key in properties
        ):
            return args, []
        if any(not isinstance(key, str) for key in args):
            return args, []
        additional = params.get("additionalProperties")
        allow_extra = (
            additional is True
            or isinstance(additional, dict)
            or not properties
        )
        if allow_extra:
            return args, []
        unexpected = sorted(k for k in args if k not in properties)
        if not unexpected:
            return args, []
        return {k: v for k, v in args.items() if k in properties}, unexpected

    @staticmethod
    def _append_ignored_args_notice(result: str, ignored_args: list[str]) -> str:
        notice = {
            "ignored_unexpected_fields": ignored_args,
            "hint": "Unexpected fields were ignored; use the tool schema fields only on future calls.",
        }
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            return result + "\n\n" + json.dumps(notice, ensure_ascii=False)
        if isinstance(data, dict):
            data.update(notice)
            return json.dumps(data, ensure_ascii=False)
        return result + "\n\n" + json.dumps(notice, ensure_ascii=False)

    # ── Dispatch ──────────────────────────────────────────────────────

    def _unknown_tool_suggestions(self, name: str) -> list[str]:
        registered = sorted(self._tools)
        suggestions = difflib.get_close_matches(name, registered, n=5, cutoff=0.45)
        aliases = {
            "target_skill": ["skill_view", "skill_copy_resource", "run_skill_python", "skills_list"],
            "read_skill": ["skill_view", "skills_list"],
            "search_web": ["web_search"],
            "python": ["execute_code", "run_skill_python"],
        }
        for candidate in aliases.get(name, []):
            if candidate in self._tools and candidate not in suggestions:
                suggestions.append(candidate)
        return suggestions[:8]

    def preflight(
        self,
        name: str,
        args: Any,
        context: ToolContext | None = None,
        *,
        allowed_tool_names: Iterable[str] | None = None,
    ) -> ToolPreflightResult:
        """Validate and normalize a call without invoking its handler.

        ``allowed_tool_names`` is intentionally opt-in. Ordinary single-call
        dispatch retains its historical registry behavior, while declared
        workflow batches pass their exact per-turn schema surface here as a
        capability boundary.
        """
        entry = self._tools.get(name)
        if not entry:
            return ToolPreflightResult(
                name=name,
                args=args,
                error_payload={
                    "error": f"Unknown tool: {name}",
                    "unknown_tool": name,
                    "suggestions": self._unknown_tool_suggestions(name),
                },
                reason="unknown_tool",
            )

        if allowed_tool_names is not None:
            allowed = frozenset(
                str(item) for item in allowed_tool_names
                if isinstance(item, str) and item
            )
            if name not in allowed:
                return ToolPreflightResult(
                    name=name,
                    args=args,
                    error_payload={
                        "error": (
                            "Tool was not exposed for this model turn and was "
                            "not dispatched."
                        ),
                        "reason": "tool_capability_boundary_violation",
                        "tool_name": name,
                    },
                    reason="tool_capability_boundary_violation",
                )

        normalized_args = dict(args) if isinstance(args, dict) else args
        if context is not None:
            normalized_args = self._strip_context_owned_args(
                entry, normalized_args,
            )
        normalized_args = self._normalize_alias_args(entry, normalized_args)

        # Parse-state sentinels and non-object values must be rejected before
        # unexpected-field stripping. Otherwise a reserved sentinel could be
        # silently removed and turn malformed provider output into a call.
        schema_error = self._validate_args(entry, normalized_args)
        if (
            not isinstance(normalized_args, dict)
            or (
                schema_error
                and (
                    "__tool_arg_parse_error" in normalized_args
                    or "_raw_args" in normalized_args
                )
            )
        ):
            return ToolPreflightResult(
                name=name,
                args=normalized_args,
                error_payload={"error": str(schema_error or "Invalid tool arguments.")},
                reason="malformed_tool_arguments",
            )

        # Compute a stable semantic projection for convergence/audit only.
        # Keep the authoritative ordering below unchanged: resource/command
        # boundaries inspect the unstripped normalized payload first, omission
        # detection sees every supplied field, and only then are unexpected
        # fields removed from the actual dispatch payload.
        semantic_args, _semantic_ignored_args = self._strip_unexpected_args(
            entry, normalized_args,
        )

        boundary_error = delegated_resource_boundary_error(
            name, normalized_args, context,
        )
        if boundary_error:
            return ToolPreflightResult(
                name=name,
                args=normalized_args,
                semantic_args=semantic_args,
                error_payload={
                    "error": boundary_error,
                    "reason": "delegated_resource_boundary_violation",
                    "tool_name": name,
                },
                reason="delegated_resource_boundary_violation",
            )

        command_boundary_error = declared_command_boundary_error(
            name, normalized_args, context,
        )
        if command_boundary_error:
            return ToolPreflightResult(
                name=name,
                args=normalized_args,
                semantic_args=semantic_args,
                error_payload={
                    "error": command_boundary_error,
                    "reason": "declared_command_boundary_violation",
                    "tool_name": name,
                },
                reason="declared_command_boundary_violation",
            )

        artifact_write_error = delegated_artifact_write_boundary_error(
            name, normalized_args, context,
        )
        if artifact_write_error:
            return ToolPreflightResult(
                name=name,
                args=normalized_args,
                semantic_args=semantic_args,
                error_payload={
                    "error": artifact_write_error,
                    "reason": "delegated_artifact_write_boundary_violation",
                    "tool_name": name,
                },
                reason="delegated_artifact_write_boundary_violation",
            )

        omission_path = _find_omission_path(normalized_args)
        if omission_path:
            omission_error = compacted_history_omission_error(omission_path)
            return ToolPreflightResult(
                name=name,
                args=normalized_args,
                semantic_args=semantic_args,
                error_payload=dict(omission_error),
                reason=str(
                    omission_error.get("reason")
                    or "invalid_placeholder_content"
                ),
            )

        normalized_args, ignored_args = self._strip_unexpected_args(
            entry, normalized_args,
        )
        schema_error = self._validate_args(entry, normalized_args)
        if schema_error:
            return ToolPreflightResult(
                name=name,
                args=normalized_args,
                semantic_args=normalized_args,
                ignored_args=tuple(ignored_args),
                error_payload={"error": schema_error},
                reason="tool_schema_validation_failed",
            )

        if entry.args_preflight_fn is not None:
            # Tool-specific validation here must be synchronous, read-only,
            # and side-effect free. It runs only after the shared capability,
            # resource, omission, and JSON-schema gates, allowing adapters to
            # reject deterministic argument/entrypoint errors before a
            # handler or sidecar is entered. This is an extensibility point,
            # not a second dispatcher: successful preflight cannot mutate or
            # widen the normalized handler payload.
            try:
                custom_error = entry.args_preflight_fn(
                    dict(normalized_args),
                    context,
                )
            except Exception:
                logger.exception(
                    "Pure argument preflight failed internally for tool %s",
                    name,
                )
                custom_error = {
                    "error": (
                        "Tool argument preflight failed internally; the call "
                        "was not dispatched."
                    ),
                    "reason": "tool_argument_preflight_internal_error",
                    "tool_name": name,
                }
            if custom_error:
                error_payload = (
                    dict(custom_error)
                    if isinstance(custom_error, dict)
                    else {
                        "error": str(custom_error),
                        "reason": "tool_argument_preflight_failed",
                    }
                )
                error_payload.setdefault(
                    "error",
                    "Tool arguments failed deterministic preflight.",
                )
                error_payload.setdefault(
                    "reason",
                    "tool_argument_preflight_failed",
                )
                error_payload.setdefault("tool_name", name)
                return ToolPreflightResult(
                    name=name,
                    args=normalized_args,
                    semantic_args=normalized_args,
                    ignored_args=tuple(ignored_args),
                    error_payload=error_payload,
                    reason=str(error_payload.get("reason") or ""),
                )

        return ToolPreflightResult(
            name=name,
            args=normalized_args,
            semantic_args=normalized_args,
            ignored_args=tuple(ignored_args),
        )

    async def dispatch(
        self,
        name: str,
        args: dict,
        context: ToolContext | None = None,
    ) -> str:
        """Execute a tool handler by name with runtime-owned context."""
        entry = self._tools.get(name)
        if not entry:
            return tool_error(
                f"Unknown tool: {name}",
                unknown_tool=name,
                suggestions=self._unknown_tool_suggestions(name),
            )
        try:
            preflight = self.preflight(name, args, context)
            if not preflight.ok:
                error = str((preflight.error_payload or {}).get("error") or "")
                if preflight.reason == "delegated_resource_boundary_violation":
                    logger.info(
                        "Tool %s rejected by delegated resource boundary run_id=%s: %s",
                        name,
                        context.run_id if context is not None else None,
                        error,
                    )
                elif preflight.reason in {
                    "invalid_placeholder_content",
                    "omission_guard_limit_exceeded",
                }:
                    logger.info("Tool %s rejected by omission guard: %s", name, error)
                else:
                    logger.info("Tool %s preflight failed: %s", name, error)
                return preflight.error_json()

            args = preflight.args
            ignored_args = list(preflight.ignored_args)

            call_args = dict(args)
            if context is not None:
                if entry.accepts_user_id:
                    call_args["user_id"] = context.user_id
                if entry.accepts_session_id:
                    call_args["session_id"] = context.session_id
                if entry.accepts_enabled_user_skills:
                    call_args["enabled_user_skills"] = list(context.enabled_user_skills)
                if entry.accepts_context:
                    call_args["context"] = context
            if entry.is_async:
                result = await entry.handler(**call_args)
            else:
                result = entry.handler(**call_args)
            if ignored_args:
                result = self._append_ignored_args_notice(str(result), ignored_args)
            return str(result)
        except Exception as e:
            logger.exception("Tool %s dispatch error: %s", name, e)
            return tool_error(
                f"Tool execution failed: {type(e).__name__}: {e}. "
                "Check this tool's schema and retry with all required arguments; "
                "do not repeat empty or malformed args."
            )


# ── Module-level singleton ─────────────────────────────────────────────────

registry = ToolRegistry()


# ── Backward-compatible module-level functions ─────────────────────────────
# These delegate to the singleton so existing callers (agent.py) work unchanged.

def register(
    name: str,
    schema: dict,
    handler: Callable,
    toolset: str = "general",
    check_fn: Callable | None = None,
    args_preflight_fn: Callable | None = None,
    is_async: bool = True,
    description: str = "",
    emoji: str = "",
    is_read_only: bool = False,
    is_destructive: bool = False,
    parallel_safe: bool = False,
    path_scoped: bool = False,
    allow_in_child: bool = True,
    allow_in_parallel_child: bool | None = None,
    mutates_workspace: bool | None = None,
    mutates_global_state: bool = False,
    requires_user_visibility: bool = False,
    salvage_safe: bool = False,
    external_interaction: bool = False,
):
    """Register a tool with the default registry."""
    registry.register(
        name=name,
        toolset=toolset,
        schema=schema,
        handler=handler,
        check_fn=check_fn,
        args_preflight_fn=args_preflight_fn,
        is_async=is_async,
        description=description,
        emoji=emoji,
        is_read_only=is_read_only,
        is_destructive=is_destructive,
        parallel_safe=parallel_safe,
        path_scoped=path_scoped,
        allow_in_child=allow_in_child,
        allow_in_parallel_child=allow_in_parallel_child,
        mutates_workspace=mutates_workspace,
        mutates_global_state=mutates_global_state,
        requires_user_visibility=requires_user_visibility,
        salvage_safe=salvage_safe,
        external_interaction=external_interaction,
    )


def get_schemas(names: list[str]) -> list[dict]:
    """Return OpenAI-formatted tool definitions for the requested tool names."""
    return registry.get_definitions(names)


def get_metadata(name: str) -> Optional[dict]:
    """Return harness-only execution metadata for a registered tool."""
    return registry.get_metadata(name)


def preflight(
    name: str,
    args: Any,
    context: ToolContext | None = None,
    *,
    allowed_tool_names: Iterable[str] | None = None,
) -> ToolPreflightResult:
    """Purely validate one registered tool call without dispatching it."""
    return registry.preflight(
        name,
        args,
        context=context,
        allowed_tool_names=allowed_tool_names,
    )


async def dispatch(
    name: str,
    args: dict,
    context: ToolContext | None = None,
) -> str:
    """Execute a registered tool with explicit runtime context."""
    return await registry.dispatch(name, args, context=context)


# ── Helpers for tool response serialization ────────────────────────────────

def tool_error(message: str, **extra) -> str:
    """Return a JSON error string for tool handlers."""
    result = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def tool_result(data=None, **kwargs) -> str:
    """Return a JSON result string for tool handlers.

    Accepts a dict positional arg *or* keyword arguments (not both).
    """
    if data is not None:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(kwargs, ensure_ascii=False)
