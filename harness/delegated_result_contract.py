"""Shared validation for delegated typed-result terminal contracts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Sequence


RESULT_FIELDS_JSON_PREFIX = "RESULT_FIELDS_JSON:"
MAX_RESULT_FIELDS_JSON_CHARS = 64 * 1024
MAX_REQUIRED_RESULT_FIELDS = 128
MAX_REQUIRED_RESULT_FIELD_CHARS = 256
MAX_REQUIRED_RESULT_SCHEMA_BYTES = 16 * 1024
DEFAULT_RESULT_CONTRACT_OUTPUT_TOKENS = 8_192
MEDIUM_RESULT_CONTRACT_OUTPUT_TOKENS = 16_384
LARGE_RESULT_CONTRACT_OUTPUT_TOKENS = 32_768

_RAW_TOOL_CALL_OPEN_PATTERN = re.compile(
    r"(?i)<tool_call\b(?P<attributes>[^>\r\n]*)>",
)
_RAW_TOOL_NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}"
_MAX_RAW_TOOL_CALL_SCAN_CHARS = 64 * 1024
_MAX_RAW_TOOL_CALL_FINDINGS = 16
_RAW_TOOL_PROTOCOL_ARGUMENT_PATTERN = re.compile(
    r"(?is)</?(?:arguments?|arg_key|arg_value|parameters?|parameter)\b|"
    r"[\"']arguments[\"']\s*:",
)
_RAW_TOOL_PROTOCOL_RESERVED_TAGS = {
    "tool_call",
    "name",
    "tool_name",
    "function",
    "function_name",
    "arguments",
    "argument",
    "arg_key",
    "arg_value",
    "parameters",
    "parameter",
}

_PROCESS_NARRATION_PATTERN = re.compile(
    r"(?:\b(?:let me|i will|i'll|i need to|i should|i am going to|i'm going to|"
    r"next\s*,?\s*i(?:\s+will|'ll|\s+need to)?|now\s+i(?:\s+will|'ll|\s+need to)?|"
    r"we need to|we will|continue (?:searching|researching|retrieving|checking)|"
    r"going to (?:search|retrieve|inspect|check|query))\b|"
    r"\u8ba9\u6211(?:\u5148|\u518d)?|\u6211(?:\u5c06|\u4f1a|\u8981|\u9700\u8981|\u51c6\u5907)(?:\u5148|\u518d)?|\u63a5\u4e0b\u6765(?:\u6211)?|"
    r"\u4e0b\u4e00\u6b65(?:\u6211)?|\u73b0\u5728(?:\u6211)?(?:\u8981|\u9700\u8981|\u5c06)|\u7ee7\u7eed(?:\u641c\u7d22|\u68c0\u7d22|\u67e5\u8be2|\u7814\u7a76|\u68c0\u67e5))",
    re.IGNORECASE,
)

_SUBSTANTIVE_RESULT_SIGNAL_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s+(?:findings?|results?|evidence|analysis|"
    r"provenance|gaps?|verification|conclusions?|recommendations?|"
    r"\u53d1\u73b0|\u7ed3\u679c|\u8bc1\u636e|\u5206\u6790|\u6765\u6e90|\u7f3a\u53e3|\u9a8c\u8bc1|\u7ed3\u8bba|\u5efa\u8bae)\b|"
    r"(?:sources?|provenance|verification|\u6765\u6e90|\u8bc1\u636e\u6765\u6e90|\u9a8c\u8bc1)\s*:|"
    r"\|[^\n]+\|\s*$)"
    r"|\b(?:pass(?:ed)?|warn(?:ing)?|fail(?:ed|ure)?|degraded|verified)\b"
    r"|\u901a\u8fc7|\u8b66\u544a|\u5931\u8d25|\u964d\u7ea7|\u9a8c\u8bc1",
    re.IGNORECASE | re.MULTILINE,
)


def is_process_narration_only(content: str) -> bool:
    """Return whether a terminal draft only promises future work.

    The inner output transaction and outer delegated-result commit boundary
    share this exact semantic predicate.  Several future-action markers are
    required, while ordinary report structure, provenance, citations, or an
    explicit completion/degraded status make the draft non-matching.
    """

    value = str(content or "").strip()
    if not value:
        return False
    process_markers = _PROCESS_NARRATION_PATTERN.findall(value)
    if len(process_markers) < 3:
        return False
    return _SUBSTANTIVE_RESULT_SIGNAL_PATTERN.search(value) is None


def delegated_result_substantive_body(content: str) -> str:
    """Return model-authored result prose without terminal machine ledgers.

    Result-field, quality, gap, and receipt records are authoritative audit
    metadata, but none can make future-action narration substantive.  Every
    delegated semantic gate uses this same projection so an inner transaction
    and its outer commit boundary cannot disagree merely because a valid
    machine ledger contains a status word.
    """

    retained, _removed = strip_result_fields_candidate_tail(content)
    machine_prefixes = (
        "[HARNESS_",
        "CAPABILITY_GAPS_JSON:",
        "COMPLETION_QUALITY_JSON:",
        "KNOWLEDGE_GATE_CHECKS_JSON:",
        "KNOWLEDGE_GATE_GAPS_JSON:",
    )
    return "\n".join(
        line
        for line in retained.splitlines()
        if not line.strip().startswith(machine_prefixes)
    ).strip()


def is_formal_object_result_schema(value: Any) -> bool:
    """Disambiguate formal JSON Schema from legacy field mappings.

    Legacy Skills may legitimately declare a result field named ``type`` with
    the example value ``object``.  Treat that as a formal root only when the
    remaining keys are JSON-Schema keywords; an explicit ``properties`` object
    remains an unambiguous formal declaration.
    """

    if not isinstance(value, dict):
        return False
    if isinstance(value.get("properties"), dict):
        return True
    if value.get("type") != "object":
        return False
    from tools.registry import JSON_SCHEMA_LOSSLESS_KEYWORDS

    return set(value).issubset(JSON_SCHEMA_LOSSLESS_KEYWORDS)


def validate_result_field_names(
    required_fields: Sequence[Any],
    *,
    field_path: str = "required_result_fields",
) -> list[str]:
    """Validate exact field identifiers without repairing their spelling."""

    if isinstance(required_fields, (str, bytes)) or not isinstance(
        required_fields, Sequence
    ):
        raise ValueError(f"{field_path} must be an array of strings")
    fields: list[str] = []
    for index, item in enumerate(required_fields):
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or "\n" in item
            or "\r" in item
            or "\x00" in item
        ):
            raise ValueError(
                f"{field_path}[{index}] must be a non-empty, single-line "
                "string without surrounding whitespace"
            )
        if len(item) > MAX_REQUIRED_RESULT_FIELD_CHARS:
            raise ValueError(
                f"{field_path}[{index}] exceeds the "
                f"{MAX_REQUIRED_RESULT_FIELD_CHARS}-character field-name limit"
            )
        fields.append(item)
    if len(fields) > MAX_REQUIRED_RESULT_FIELDS:
        raise ValueError(
            f"{field_path} may declare at most "
            f"{MAX_REQUIRED_RESULT_FIELDS} exact fields"
        )
    if len(set(fields)) != len(fields):
        raise ValueError(f"{field_path} must list each exact field once")
    return fields


def validate_projected_result_contract(
    required_fields: Sequence[Any],
    field_schema: Any,
    *,
    field_path: str = "required_result_fields",
    schema_path: str = "required_result_schema",
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Validate the exact metadata accepted by delegated child transport."""

    fields = validate_result_field_names(
        required_fields,
        field_path=field_path,
    )
    if not isinstance(field_schema, dict):
        raise ValueError(f"{schema_path} must be an object")
    if set(field_schema) != set(fields):
        raise ValueError(
            f"{schema_path} keys must exactly match {field_path}"
        )
    if any(not isinstance(value, dict) for value in field_schema.values()):
        raise ValueError(
            f"Every {schema_path} value must be a JSON-schema object"
        )

    from tools.registry import json_schema_shape_error

    for field_name in fields:
        schema_error = json_schema_shape_error(
            field_schema[field_name],
            schema_path=f"{schema_path}.{field_name}",
            reject_unsupported_keywords=True,
        )
        if schema_error is not None:
            raise ValueError(schema_error)
    try:
        encoded = json.dumps(
            field_schema,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(
            f"{schema_path} must contain only finite, acyclic JSON values"
        ) from exc
    if len(encoded) > MAX_REQUIRED_RESULT_SCHEMA_BYTES:
        raise ValueError(
            f"{schema_path} exceeds the "
            f"{MAX_REQUIRED_RESULT_SCHEMA_BYTES // 1024} KiB delegation "
            "metadata limit"
        )
    return fields, json.loads(encoded.decode("utf-8"))


def project_object_result_contract(
    schema: Any,
    *,
    schema_path: str = "declared_result_schema",
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Project one object JSON Schema to the exact delegated wire contract.

    Workflow compilation and child dispatch must share this boundary.  A plan
    is not executable when its root schema cannot be represented by the
    delegation protocol's exact top-level field list and per-field schemas, so
    reject it before any authority is installed or worker is scheduled.
    """

    if not isinstance(schema, dict):
        raise ValueError(f"{schema_path} must be a JSON-schema object")

    from tools.registry import (
        JSON_SCHEMA_LOSSLESS_KEYWORDS,
        json_schema_shape_error,
    )

    root_error = json_schema_shape_error(
        schema,
        schema_path=schema_path,
        reject_unsupported_keywords=True,
    )
    if root_error is not None:
        raise ValueError(root_error)
    raw_required = schema.get("required", [])
    required_fields = validate_result_field_names(
        raw_required,
        field_path=f"{schema_path}.required",
    )

    annotation_keywords = JSON_SCHEMA_LOSSLESS_KEYWORDS - {
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
    }
    projectable_root_keywords = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        *annotation_keywords,
    }
    unprojectable = sorted(set(schema) - projectable_root_keywords)
    if unprojectable:
        raise ValueError(
            f"{schema_path}.{unprojectable[0]} cannot be losslessly projected "
            "into per-field result metadata"
        )

    declared_root_type = schema.get("type")
    if declared_root_type is None and not required_fields:
        # A properties-only schema with no required names admits the empty
        # object, so it creates no exact terminal-ledger obligation. Preserve
        # this standards-compliant compatibility form without manufacturing
        # optional fields.
        return [], {}
    if declared_root_type != "object":
        raise ValueError(
            f"{schema_path}.type must be exactly 'object' for a delegated "
            "result contract"
        )

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError(f"{schema_path}.properties must be an object")
    additional = schema.get("additionalProperties", True)
    projected: dict[str, dict[str, Any]] = {}
    for field_name in required_fields:
        if field_name in properties:
            declaration = properties[field_name]
        elif isinstance(additional, dict):
            declaration = additional
        elif additional is False:
            raise ValueError(
                f"{schema_path}.required names property {field_name!r}, but "
                "additionalProperties is false and no property schema is declared"
            )
        else:
            declaration = {}

        if declaration is True:
            field_schema: dict[str, Any] = {}
        elif declaration is False:
            raise ValueError(
                f"{schema_path}.properties.{field_name} is boolean false and "
                "cannot yield a required result value"
            )
        elif isinstance(declaration, dict):
            field_error = json_schema_shape_error(
                declaration,
                schema_path=f"{schema_path}.properties.{field_name}",
                reject_unsupported_keywords=True,
            )
            if field_error is not None:
                raise ValueError(field_error)
            field_schema = declaration
        else:
            raise ValueError(
                f"{schema_path}.properties.{field_name} must be an object or "
                "boolean JSON Schema"
            )
        projected[field_name] = field_schema

    # Detach runtime metadata from loader/model-owned mutable structures while
    # applying the exact same field/schema transport boundary used by child
    # dispatch.
    return validate_projected_result_contract(
        required_fields,
        projected,
        field_path=f"{schema_path}.required",
        schema_path=f"{schema_path}.projection",
    )


def adaptive_result_contract_output_tokens(
    required_fields: Sequence[str],
    field_schema: dict[str, Any],
) -> int:
    """Choose one shared structural output tier for child and finalizer.

    The score is independent of domain, filename, model, and provider.  Both
    the original delegated response and its one bounded typed-footer repair
    must receive the same schema-complexity tier; otherwise a contract that
    legitimately needed 16K/32K can be truncated only during finalization.
    """

    node_count = 0
    stack: list[Any] = [field_schema]
    while stack and node_count < 2_000:
        value = stack.pop()
        node_count += 1
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
    score = (len(tuple(required_fields)) * 8) + node_count
    if score <= 48:
        return DEFAULT_RESULT_CONTRACT_OUTPUT_TOKENS
    if score <= 120:
        return MEDIUM_RESULT_CONTRACT_OUTPUT_TOKENS
    return LARGE_RESULT_CONTRACT_OUTPUT_TOKENS


def normalize_result_field_schema(
    required_fields: Sequence[str],
    field_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the run-scoped per-field schema used by every footer gate.

    Field names are themselves a typed-output contract.  When an older/direct
    delegate supplies those names without a value schema, preserve the native
    JSON value instead of inventing a research-specific envelope: the empty
    JSON Schema accepts any JSON scalar, container, or ``null``.  Explicit
    schema mappings are returned unchanged so their exact semantics remain
    authoritative.

    Boundary validation still owns malformed/non-object schema rejection.  The
    defensive non-dict fallback below retains the parser's historical
    fail-closed behavior for callers that bypass that boundary.
    """
    if field_schema is None:
        return {str(field): {} for field in required_fields}
    if isinstance(field_schema, dict):
        return field_schema
    return {}


def strip_result_fields_candidate_tail(content: str) -> tuple[str, int]:
    """Strip the first typed-ledger candidate line and everything after it.

    A ``RESULT_FIELDS_JSON`` ledger is required to be one terminal non-empty
    line.  Once the shared parser rejects an accumulated delegated result, any
    candidate marker (including a truncated or pretty-printed/multiline
    payload) and its following bytes belong to that rejected ledger turn, not
    to the substantive report retained for a footer-only correction. The first
    protocol-visible marker is authoritative here: a terminal ledger may not
    be followed by report prose or another ledger. Markdown code/examples are
    masked and are not candidates.
    """
    value = str(content or "")
    lines = value.splitlines(keepends=True)
    masked_lines = _mask_markdown_code_for_protocol_audit(value).splitlines(
        keepends=True
    )
    candidate_index = next(
        (
            index
            for index in range(len(masked_lines))
            if masked_lines[index].strip().startswith(
                RESULT_FIELDS_JSON_PREFIX
            )
        ),
        None,
    )
    if candidate_index is None:
        return value, 0
    retained = "".join(lines[:candidate_index])
    return retained, len(value) - len(retained)


def _result_fields_candidate_offsets(content: str) -> list[int]:
    """Return protocol-visible footer marker offsets without exposing text."""

    value = str(content or "")
    masked = _mask_markdown_code_for_protocol_audit(value)
    offsets: list[int] = []
    offset = 0
    for line in masked.splitlines(keepends=True):
        leading = len(line) - len(line.lstrip())
        if line[leading:].startswith(RESULT_FIELDS_JSON_PREFIX):
            offsets.append(offset + leading)
        offset += len(line)
    return offsets


def _reject_duplicate_result_field_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, item in pairs:
        if key in parsed:
            raise ValueError(f"duplicate field key: {key}")
        parsed[key] = item
    return parsed


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def canonical_result_fields_footer(
    ledger: Any,
    required_fields: Sequence[str],
    field_schema: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Serialize one already-parsed ledger iff the exact contract validates.

    This is intentionally a canonicalizer, not a JSON repairer. It never adds,
    removes, guesses, or coerces a field value. The shared footer audit remains
    authoritative after serialization, including exact-key and per-field
    JSON-Schema validation.
    """

    try:
        encoded = json.dumps(
            ledger,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        return None, {
            "footer_valid": False,
            "footer_error": (
                "RESULT_FIELDS_JSON value is not finite canonical JSON: "
                + type(exc).__name__
            ),
        }
    if len(encoded) > MAX_RESULT_FIELDS_JSON_CHARS:
        return None, {
            "footer_valid": False,
            "footer_error": "RESULT_FIELDS_JSON exceeds 64 KiB",
        }
    footer = RESULT_FIELDS_JSON_PREFIX + " " + encoded
    audit = audit_result_fields(footer, required_fields, field_schema)
    return (footer if audit.get("footer_valid") else None), audit


def canonical_result_fields_footer_from_json(
    raw_json: str,
    required_fields: Sequence[str],
    field_schema: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Strictly parse one complete JSON object and canonicalize it.

    Unlike the content extractor, internal structured-call arguments must be
    the entire JSON payload. Trailing non-whitespace, duplicate keys,
    non-finite constants, and malformed JSON all fail closed.
    """

    raw = str(raw_json or "")
    if not raw or len(raw) > MAX_RESULT_FIELDS_JSON_CHARS:
        return None, {
            "footer_valid": False,
            "footer_error": "structured result JSON is empty or exceeds 64 KiB",
        }
    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_result_field_pairs,
        parse_constant=_reject_nonfinite_json_constant,
    )
    try:
        ledger, consumed = decoder.raw_decode(raw.lstrip())
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        return None, {
            "footer_valid": False,
            "footer_error": (
                "structured result JSON is invalid: " + type(exc).__name__
            ),
        }
    leading = len(raw) - len(raw.lstrip())
    if raw[leading + consumed:].strip():
        return None, {
            "footer_valid": False,
            "footer_error": "structured result JSON has trailing data",
        }
    return canonical_result_fields_footer(
        ledger,
        required_fields,
        field_schema,
    )


def canonical_result_fields_footer_from_internal_submitter_json(
    raw_json: str,
    required_fields: Sequence[str],
    field_schema: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    """Validate an internal submitter payload with one safe wire adaptation.

    Some OpenAI-compatible providers double-serialize an object- or
    array-valued *field* while producing otherwise valid native tool
    arguments.  At this exact, non-dispatchable, closed-schema boundary the
    harness may unwrap one such JSON string envelope, but only when all of the
    following are true:

    * the outer arguments are one complete strict JSON object;
    * the original string does not satisfy the declared field schema;
    * the string is one complete strict JSON object or array; and
    * the decoded container satisfies the same declared schema exactly.

    This is transport normalization, not semantic repair.  It never changes a
    schema-valid string, scalar, key set, or malformed/ambiguous value, and the
    ordinary authoritative footer audit still runs after normalization.
    Diagnostics intentionally expose only counts and hashes, never values.
    """

    diagnostics: dict[str, Any] = {
        "transport_envelope_normalized": False,
        "normalized_field_count": 0,
        "normalized_field_name_sha256": [],
        "candidate_string_count": 0,
        "rejected_candidate_count": 0,
    }
    raw = str(raw_json or "")
    if not raw or len(raw) > MAX_RESULT_FIELDS_JSON_CHARS:
        return None, {
            "footer_valid": False,
            "footer_error": "structured result JSON is empty or exceeds 64 KiB",
        }, diagnostics

    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_result_field_pairs,
        parse_constant=_reject_nonfinite_json_constant,
    )
    try:
        ledger, consumed = decoder.raw_decode(raw.lstrip())
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        return None, {
            "footer_valid": False,
            "footer_error": (
                "structured result JSON is invalid: " + type(exc).__name__
            ),
        }, diagnostics
    leading = len(raw) - len(raw.lstrip())
    if raw[leading + consumed:].strip():
        return None, {
            "footer_valid": False,
            "footer_error": "structured result JSON has trailing data",
        }, diagnostics

    fields = tuple(str(field) for field in required_fields)
    schemas = normalize_result_field_schema(fields, field_schema)
    if not isinstance(ledger, dict) or set(ledger) != set(fields):
        footer, audit = canonical_result_fields_footer(
            ledger,
            fields,
            field_schema,
        )
        return footer, audit, diagnostics

    normalized = dict(ledger)
    normalized_fields: list[str] = []
    try:
        # Use the same bounded schema validator as both native tool arguments
        # and the final delegated-result audit.  A permissive or string-valued
        # schema therefore leaves the provider value untouched.
        from tools.registry import json_schema_value_error
    except Exception:  # pragma: no cover - downstream audit remains fail closed
        json_schema_value_error = None

    for field in fields:
        value = ledger.get(field)
        schema = schemas.get(field)
        if (
            json_schema_value_error is None
            or not isinstance(value, str)
            or not isinstance(schema, dict)
        ):
            continue
        stripped = value.strip()
        if not stripped.startswith(("{", "[")):
            continue
        diagnostics["candidate_string_count"] += 1

        original_error = json_schema_value_error(
            value,
            schema,
            value_path=f"result_fields.{field}",
            schema_path=f"result_schema.{field}",
        )
        if original_error is None:
            # The declared contract legitimately accepts the string.  Parsing
            # it would change semantics, so it is not a transport envelope.
            diagnostics["rejected_candidate_count"] += 1
            continue
        if len(value) > MAX_RESULT_FIELDS_JSON_CHARS:
            diagnostics["rejected_candidate_count"] += 1
            continue
        try:
            candidate, inner_consumed = decoder.raw_decode(value.lstrip())
        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
            RecursionError,
        ):
            diagnostics["rejected_candidate_count"] += 1
            continue
        inner_leading = len(value) - len(value.lstrip())
        if (
            value[inner_leading + inner_consumed:].strip()
            or not isinstance(candidate, (dict, list))
        ):
            diagnostics["rejected_candidate_count"] += 1
            continue
        candidate_error = json_schema_value_error(
            candidate,
            schema,
            value_path=f"result_fields.{field}",
            schema_path=f"result_schema.{field}",
        )
        if candidate_error is not None:
            diagnostics["rejected_candidate_count"] += 1
            continue
        normalized[field] = candidate
        normalized_fields.append(field)

    if normalized_fields:
        diagnostics.update({
            "transport_envelope_normalized": True,
            "normalized_field_count": len(normalized_fields),
            "normalized_field_name_sha256": sorted(
                hashlib.sha256(field.encode("utf-8")).hexdigest()
                for field in normalized_fields
            ),
        })
    footer, audit = canonical_result_fields_footer(
        normalized,
        fields,
        field_schema,
    )
    return footer, audit, diagnostics


def extract_canonical_result_fields_footer(
    content: str,
    required_fields: Sequence[str],
    field_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract the last complete standards-compliant JSON footer candidate.

    Providers sometimes pretty-print an otherwise valid JSON object. When
    exactly one candidate contains that object plus whitespace only, the
    harness may replace its framing and re-emit the *same parsed value*
    canonically. Multiple candidates or trailing non-whitespace are ambiguous
    and remain invalid. Parsing is strict RFC JSON:
    no JSON5, quote repair, delimiter completion, value coercion, or guessed
    defaults are permitted. A malformed candidate therefore remains invalid
    and must use the one closed-schema repair turn or fail closed.

    Returned diagnostics contain only counts, positions, and parser state;
    source text and parsed values are deliberately omitted.
    """

    value = str(content or "")
    offsets = _result_fields_candidate_offsets(value)
    result: dict[str, Any] = {
        "recovered": False,
        "method": None,
        "candidate_count": len(offsets),
        "selected_candidate_ordinal": len(offsets) if offsets else 0,
        "candidate_chars": 0,
        "json_chars_consumed": 0,
        "trailing_chars_discarded": 0,
        "json_error_position": None,
        "json_error_line": None,
        "json_error_column": None,
        "object_balance": 0,
        "array_balance": 0,
        "minimum_object_balance": 0,
        "minimum_array_balance": 0,
        "in_string": False,
        "trailing_escape": False,
        "footer": None,
        "audit": None,
    }
    if len(offsets) != 1:
        return result

    marker_offset = offsets[0]
    raw = value[
        marker_offset + len(RESULT_FIELDS_JSON_PREFIX):
    ].lstrip()
    result["candidate_chars"] = len(raw)
    if not raw or len(raw) > MAX_RESULT_FIELDS_JSON_CHARS:
        return result

    object_balance = 0
    array_balance = 0
    minimum_object_balance = 0
    minimum_array_balance = 0
    in_string = False
    escaping = False
    for char in raw:
        if in_string:
            if escaping:
                escaping = False
            elif char == "\\":
                escaping = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            object_balance += 1
        elif char == "}":
            object_balance -= 1
            minimum_object_balance = min(
                minimum_object_balance, object_balance
            )
        elif char == "[":
            array_balance += 1
        elif char == "]":
            array_balance -= 1
            minimum_array_balance = min(array_balance, minimum_array_balance)
    result.update({
        "object_balance": object_balance,
        "array_balance": array_balance,
        "minimum_object_balance": minimum_object_balance,
        "minimum_array_balance": minimum_array_balance,
        "in_string": in_string,
        "trailing_escape": bool(in_string and escaping),
    })

    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_result_field_pairs,
        parse_constant=_reject_nonfinite_json_constant,
    )
    try:
        ledger, consumed = decoder.raw_decode(raw)
    except json.JSONDecodeError as exc:
        result.update({
            "json_error_position": int(exc.pos),
            "json_error_line": int(exc.lineno),
            "json_error_column": int(exc.colno),
        })
        return result
    except (TypeError, ValueError, RecursionError):
        return result

    footer, audit = canonical_result_fields_footer(
        ledger,
        required_fields,
        field_schema,
    )
    trailing = raw[consumed:]
    result.update({
        "json_chars_consumed": consumed,
        "trailing_chars_discarded": len(trailing),
        "audit": audit,
    })
    if trailing.strip():
        # A deterministic canonicalization may change framing only. Arbitrary
        # bytes after the JSON object could be substantive or adversarial and
        # therefore require the isolated structured submitter; never silently
        # discard them here.
        return result
    if footer is None:
        return result
    result.update({
        "recovered": True,
        "method": "strict_json_canonicalization",
        "footer": footer,
    })
    return result


def _blank_text_span(buffer: list[str], start: int, end: int) -> None:
    for index in range(max(0, start), min(len(buffer), end)):
        if buffer[index] not in {"\r", "\n"}:
            buffer[index] = " "


def _mask_markdown_code_for_protocol_audit(content: str) -> str:
    """Mask code/examples so literal protocol documentation stays valid."""
    value = str(content or "")
    masked = list(value)
    offset = 0
    open_fence: tuple[str, int] | None = None
    for line in value.splitlines(keepends=True):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if open_fence is not None:
            _blank_text_span(masked, offset, offset + len(line))
            if (
                fence is not None
                and fence.group(1)[0] == open_fence[0]
                and len(fence.group(1)) >= open_fence[1]
            ):
                open_fence = None
        elif fence is not None:
            open_fence = (fence.group(1)[0], len(fence.group(1)))
            _blank_text_span(masked, offset, offset + len(line))
        elif re.match(r"^(?: {4}|\t)", line):
            _blank_text_span(masked, offset, offset + len(line))
        offset += len(line)

    intermediate = "".join(masked)
    excluded_patterns = (
        re.compile(r"(?s)<!--.*?-->"),
        re.compile(r"(?is)<(?:pre|code)\b[^>]*>.*?</(?:pre|code)\s*>"),
        re.compile(r"(?P<ticks>`+)[^\r\n]*?(?P=ticks)"),
    )
    for pattern in excluded_patterns:
        for match in pattern.finditer(intermediate):
            _blank_text_span(masked, match.start(), match.end())
        intermediate = "".join(masked)
    return intermediate


def audit_raw_tool_protocol(
    content: str,
    completed_tools: Sequence[str] = (),
) -> dict[str, Any]:
    """Audit executable-looking raw XML tool calls outside code examples.

    A raw call has no trustworthy lifecycle identity or argument binding, so a
    matching completed capability is diagnostic context only and never makes
    serialized protocol acceptable as a delegated result.
    """
    masked = _mask_markdown_code_for_protocol_audit(content)
    completed_folded = {
        str(name).casefold() for name in completed_tools if str(name).strip()
    }
    unsupported: set[str] = set()
    completed_name_overlap: set[str] = set()
    unknown_unsupported = 0
    detected_count = 0
    truncated = False
    first_protocol_offset: int | None = None
    last_protocol_end = 0

    for opening_index, opening in enumerate(
        _RAW_TOOL_CALL_OPEN_PATTERN.finditer(masked)
    ):
        if opening_index >= _MAX_RAW_TOOL_CALL_FINDINGS:
            truncated = True
            break
        body = masked[
            opening.end(): opening.end() + _MAX_RAW_TOOL_CALL_SCAN_CHARS
        ]
        attributes = str(opening.group("attributes") or "")
        tool_name = ""
        explicit_named_shape = False
        attribute_match = re.search(
            rf"(?i)\b(?:name|tool|function)\s*=\s*[\"']?"
            rf"(?P<name>{_RAW_TOOL_NAME_PATTERN})",
            attributes,
        )
        if attribute_match is not None:
            tool_name = attribute_match.group("name")
            explicit_named_shape = True
        if not tool_name:
            nested_match = re.match(
                rf"(?is)^\s*<(?:name|tool_name|function|function_name)\s*>"
                rf"\s*(?P<name>{_RAW_TOOL_NAME_PATTERN})\s*</",
                body,
            )
            if nested_match is not None:
                tool_name = nested_match.group("name")
                explicit_named_shape = True
        if not tool_name:
            json_match = re.search(
                rf"(?is)^[\s{{]*[\"'](?:name|tool|function)[\"']\s*:\s*"
                rf"[\"'](?P<name>{_RAW_TOOL_NAME_PATTERN})[\"']",
                body[:2048],
            )
            if json_match is not None:
                tool_name = json_match.group("name")
                explicit_named_shape = True
        if not tool_name:
            immediate_match = re.match(
                rf"(?is)^\s*(?P<name>{_RAW_TOOL_NAME_PATTERN})"
                # Common provider dialects serialize a call as
                # ``<tool_call>write_file(...)`` or the escaped JSON-key form
                # ``<tool_call>write_file\":{...}`` without a nested name tag
                # or closing ``</tool_call>``. These delimiters identify an
                # executable call, unlike prose that merely discusses the
                # literal ``<tool_call>`` marker.
                r"(?=\s*(?:\(|>|\r?\n|\{|$|\\?[\"']\s*:))",
                body,
            )
            if immediate_match is not None:
                tool_name = immediate_match.group("name")
                explicit_named_shape = True
        closing_tool_call = re.search(r"(?is)</tool_call\s*>", body)
        if not tool_name:
            for closing_match in re.finditer(
                rf"(?is)</(?P<name>{_RAW_TOOL_NAME_PATTERN})\s*>",
                body,
            ):
                candidate = closing_match.group("name")
                if candidate.casefold() not in _RAW_TOOL_PROTOCOL_RESERVED_TAGS:
                    tool_name = candidate
                    break
        closing_named_tool = bool(
            tool_name
            and re.search(
                rf"(?is)</{re.escape(tool_name)}\s*>",
                body,
            )
        )
        protocol_shape = bool(
            explicit_named_shape
            or closing_tool_call
            or closing_named_tool
            or _RAW_TOOL_PROTOCOL_ARGUMENT_PATTERN.search(body)
        )
        if not protocol_shape:
            continue
        detected_count += 1
        if first_protocol_offset is None:
            first_protocol_offset = opening.start()
        closing_end = None
        if closing_tool_call is not None:
            closing_end = opening.end() + closing_tool_call.end()
        elif closing_named_tool and tool_name:
            closing_named_match = re.search(
                rf"(?is)</{re.escape(tool_name)}\s*>",
                body,
            )
            if closing_named_match is not None:
                closing_end = opening.end() + closing_named_match.end()
        if closing_end is None:
            # An unterminated serialized call contaminates every remaining
            # byte in the bounded scan; report only offsets, never raw content.
            closing_end = min(len(masked), opening.end() + len(body))
        last_protocol_end = max(last_protocol_end, closing_end)
        if tool_name:
            unsupported.add(tool_name)
            if tool_name.casefold() in completed_folded:
                completed_name_overlap.add(tool_name)
        else:
            unknown_unsupported += 1

    return {
        "detected_count": detected_count,
        "supported_tool_names": [],
        "unsupported_tool_names": sorted(unsupported, key=str.casefold),
        "completed_name_overlap": sorted(
            completed_name_overlap,
            key=str.casefold,
        ),
        "unknown_unsupported_count": unknown_unsupported,
        "scan_truncated": truncated,
        "raw_protocol_first_offset": first_protocol_offset,
        "raw_protocol_span_chars": (
            max(0, last_protocol_end - first_protocol_offset)
            if first_protocol_offset is not None else 0
        ),
        "clean_prefix_chars": first_protocol_offset or 0,
    }


def audit_result_fields(
    content: str,
    required_fields: Sequence[str],
    field_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the terminal machine-readable ledger for typed child fields.

    This parser is shared by the in-run stop gate and the outer delegated
    result audit.  The in-run gate may request one footer-only correction, but
    only this exact contract decides whether that correction is structurally
    valid; the outer audit remains authoritative for every other requirement.
    """
    fields = list(required_fields)
    schemas = normalize_result_field_schema(fields, field_schema)
    audit: dict[str, Any] = {
        "present": [],
        "degraded": [],
        "missing": fields,
        "footer_valid": not fields,
        "footer_error": None,
        "footer_required": bool(fields),
        "footer_present": False,
        "footer_status": "invalid" if fields else "not_required",
    }
    if not fields:
        return audit
    lines = [
        line.strip()
        for line in str(content or "").rstrip().splitlines()
        if line.strip()
    ]
    protocol_visible_candidates = [
        line.strip()
        for line in _mask_markdown_code_for_protocol_audit(
            str(content or "")
        ).splitlines()
        if line.strip().startswith(RESULT_FIELDS_JSON_PREFIX)
    ]
    audit["footer_present"] = bool(protocol_visible_candidates)
    if len(protocol_visible_candidates) != 1:
        audit["footer_error"] = (
            "multiple non-code RESULT_FIELDS_JSON candidates are present"
            if len(protocol_visible_candidates) > 1
            else "no protocol-visible RESULT_FIELDS_JSON candidate is present"
        )
        return audit
    if not lines or not lines[-1].startswith(RESULT_FIELDS_JSON_PREFIX):
        audit["footer_error"] = (
            "the final non-empty line is not RESULT_FIELDS_JSON"
        )
        return audit
    raw = lines[-1][len(RESULT_FIELDS_JSON_PREFIX):].strip()
    if not raw or len(raw) > MAX_RESULT_FIELDS_JSON_CHARS:
        audit["footer_error"] = (
            "RESULT_FIELDS_JSON is empty or exceeds 64 KiB"
        )
        return audit

    def reject_duplicate_pairs(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, item in pairs:
            if key in parsed:
                raise ValueError(f"duplicate field key: {key}")
            parsed[key] = item
        return parsed

    try:
        ledger = json.loads(
            raw,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        audit["footer_error"] = (
            f"RESULT_FIELDS_JSON is invalid: {type(exc).__name__}"
        )
        return audit
    expected = set(fields)
    if not isinstance(ledger, dict) or set(ledger) != expected:
        audit["footer_error"] = (
            "RESULT_FIELDS_JSON keys must exactly match required_result_fields"
        )
        return audit

    present: list[str] = []
    degraded: list[str] = []
    missing: list[str] = []

    def substantive(value: Any) -> bool:
        if value is None or isinstance(value, (dict, list)):
            return False
        if isinstance(value, str):
            return bool(value.strip())
        return isinstance(value, (bool, int, float))

    def substantive_text(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def schema_is_status_envelope(schema: Any) -> bool:
        if not isinstance(schema, dict):
            return False
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return False
        keys = {str(key).casefold() for key in properties}
        return (
            "status" in keys
            and "provenance" in keys
            and bool(keys & {"value", "value_summary", "reason"})
        )

    schema_errors: dict[str, str] = {}
    for field in fields:
        record = ledger.get(field)
        declared_schema = schemas.get(field)
        if isinstance(declared_schema, dict):
            try:
                # Share the exact bounded subset used for native tool schemas;
                # do not maintain a second, drifting validator here.
                from tools.registry import json_schema_value_error

                schema_error = json_schema_value_error(
                    record,
                    declared_schema,
                    value_path=f"result_fields.{field}",
                    schema_path=f"result_schema.{field}",
                )
            except Exception as exc:  # pragma: no cover - fail-closed fallback
                schema_error = (
                    "shared schema validator unavailable: "
                    + type(exc).__name__
                )
            if schema_error is not None:
                missing.append(field)
                schema_errors[field] = schema_error
                continue
            if not schema_is_status_envelope(declared_schema):
                present.append(field)
                continue
        if not isinstance(record, dict):
            missing.append(field)
            continue
        status = str(record.get("status") or "").strip().casefold()
        provenance = record.get("provenance")
        if status == "present" and substantive(
            record.get("value_summary", record.get("value"))
        ) and substantive_text(provenance):
            present.append(field)
        elif status in {"degraded", "warn", "gap"} and substantive_text(
            record.get("reason")
        ) and substantive_text(provenance):
            degraded.append(field)
        else:
            missing.append(field)
    audit.update({
        "present": present,
        "degraded": degraded,
        "missing": missing,
        "footer_valid": not missing,
        "footer_status": "valid" if not missing else "invalid",
        "footer_error": (
            None
            if not missing
            else (
                "one or more field values does not satisfy its declared schema"
                + (
                    ": " + next(iter(schema_errors.values()))[:500]
                    if schema_errors else ""
                )
                if schemas else
                "one or more field records lacks a valid status/value or provenance"
            )
        ),
    })
    return audit
