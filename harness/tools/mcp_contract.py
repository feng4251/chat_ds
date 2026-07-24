"""Immutable, fail-closed contracts for session-scoped MCP tools.

MCP servers own their ``tools/list`` payloads and may refresh that payload at
runtime.  The model-facing schema, argument validator, delegation policy, and
eventual dispatcher must therefore agree on one bounded snapshot rather than
independently interpreting mutable server state.

This module is deliberately transport-agnostic.  ``mcp_client`` adapts live
server state into these descriptors; callers may then retain a
``FrozenMCPCatalog`` for one run and require a schema-drift check immediately
before dispatch.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from tools.registry import json_schema_shape_error, json_schema_value_error


MCP_CONTRACT_SCHEMA_VERSION = 1
MCP_MAX_SCHEMA_BYTES = 256 * 1024
MCP_MAX_ARGUMENT_BYTES = 2 * 1024 * 1024
MCP_MAX_JSON_DEPTH = 48
MCP_MAX_JSON_NODES = 50_000
MCP_MAX_CATALOG_TOOLS = 512
MCP_MAX_NAME_CHARS = 512
MCP_MAX_DESCRIPTION_CHARS = 16_384

_DEFAULT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {},
}
_PARSE_SENTINELS = frozenset({"_raw_args", "__tool_arg_parse_error"})


class MCPContractError(ValueError):
    """A deterministic MCP contract error that is safe to show to the model."""

    def __init__(self, message: str, *, reason: str = "invalid_mcp_contract"):
        super().__init__(message)
        self.reason = reason


def _bounded_json_clone(
    value: Any,
    *,
    label: str,
    max_bytes: int,
) -> tuple[Any, str]:
    """Return a detached JSON value and canonical JSON under strict bounds."""

    nodes = 0
    text_chars = 0
    active: set[int] = set()

    def clone(item: Any, path: str, depth: int) -> Any:
        nonlocal nodes, text_chars
        if depth > MCP_MAX_JSON_DEPTH:
            raise MCPContractError(
                f"{label} exceeds the bounded depth limit at {path}",
                reason="mcp_contract_limit_exceeded",
            )
        nodes += 1
        if nodes > MCP_MAX_JSON_NODES:
            raise MCPContractError(
                f"{label} exceeds the bounded node limit at {path}",
                reason="mcp_contract_limit_exceeded",
            )

        if isinstance(item, str):
            text_chars += len(item)
            if text_chars > max_bytes:
                raise MCPContractError(
                    f"{label} exceeds the bounded {max_bytes}-byte limit",
                    reason="mcp_contract_limit_exceeded",
                )
            return item
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if math.isfinite(item):
                return item
            raise MCPContractError(
                f"{label} contains a non-finite number at {path}",
            )
        if not isinstance(item, (list, dict)):
            raise MCPContractError(
                f"{label} contains non-JSON value {type(item).__name__} at {path}",
            )

        identity = id(item)
        if identity in active:
            raise MCPContractError(f"{label} contains a cycle at {path}")
        active.add(identity)
        try:
            if isinstance(item, list):
                return [
                    clone(child, f"{path}[{index}]", depth + 1)
                    for index, child in enumerate(item)
                ]
            result: dict[str, Any] = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise MCPContractError(
                        f"{label} contains a non-string object key at {path}",
                    )
                text_chars += len(key)
                if text_chars > max_bytes:
                    raise MCPContractError(
                        f"{label} exceeds the bounded {max_bytes}-byte limit",
                        reason="mcp_contract_limit_exceeded",
                    )
                bounded_key = key if len(key) <= 128 else key[:125] + "..."
                child_path = (
                    f"{path}.{bounded_key}" if bounded_key else f"{path}['']"
                )
                result[key] = clone(child, child_path, depth + 1)
            return result
        finally:
            active.remove(identity)

    detached = clone(value, label, 0)
    try:
        canonical = json.dumps(
            detached,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise MCPContractError(f"{label} is not bounded JSON: {exc}") from exc
    if len(canonical.encode("utf-8")) > max_bytes:
        raise MCPContractError(
            f"{label} exceeds the bounded {max_bytes}-byte limit",
            reason="mcp_contract_limit_exceeded",
        )
    return detached, canonical


def _schema_mapping(value: Any) -> Any:
    """Convert SDK/Pydantic schema containers without accepting arbitrary code."""

    if value is None:
        return dict(_DEFAULT_INPUT_SCHEMA)
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return value.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            )
        except (TypeError, ValueError) as exc:
            raise MCPContractError(
                f"MCP inputSchema could not be converted to JSON: {exc}",
            ) from exc
    if isinstance(value, Mapping) and not isinstance(value, dict):
        return dict(value)
    return value


def normalize_mcp_input_schema(input_schema: Any) -> dict[str, Any]:
    """Normalize and losslessly detach one supported MCP ``inputSchema``.

    The complete bounded schema is retained; top-level keywords are never
    flattened to ``type/properties/required``.  The Harness intentionally
    rejects validation keywords it cannot enforce.  Keeping such a keyword in
    the prompt while ignoring it at dispatch would weaken the server's
    declared contract.
    """

    candidate = _schema_mapping(input_schema)
    if candidate == {}:
        candidate = dict(_DEFAULT_INPUT_SCHEMA)
    detached, _ = _bounded_json_clone(
        candidate,
        label="MCP inputSchema",
        max_bytes=MCP_MAX_SCHEMA_BYTES,
    )
    if not isinstance(detached, dict):
        raise MCPContractError("MCP inputSchema must be a JSON object")

    # Function-call arguments are JSON objects even when an underspecified
    # server omits the top-level type.  Make that transport invariant explicit.
    if "type" not in detached:
        detached = {"type": "object", **detached}
    declared_type = detached.get("type")
    declared_types = (
        declared_type if isinstance(declared_type, list) else [declared_type]
    )
    if "object" not in declared_types:
        raise MCPContractError(
            "MCP inputSchema must permit an object at its top level",
        )

    shape_error = json_schema_shape_error(
        detached,
        schema_path="inputSchema",
        reject_unsupported_keywords=True,
    )
    if shape_error:
        raise MCPContractError(
            f"Invalid MCP inputSchema: {shape_error}",
            reason="invalid_mcp_schema",
        )
    # Re-run the byte bound after the object-type normalization.
    normalized, _ = _bounded_json_clone(
        detached,
        label="MCP inputSchema",
        max_bytes=MCP_MAX_SCHEMA_BYTES,
    )
    return normalized


def canonical_mcp_schema_json(input_schema: Any) -> str:
    """Return the stable canonical JSON for one valid MCP input schema."""

    normalized = normalize_mcp_input_schema(input_schema)
    _, canonical = _bounded_json_clone(
        normalized,
        label="MCP inputSchema",
        max_bytes=MCP_MAX_SCHEMA_BYTES,
    )
    return canonical


def mcp_schema_sha256(input_schema: Any) -> str:
    """Return a stable digest for one normalized MCP input schema."""

    canonical = canonical_mcp_schema_json(input_schema)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MCPToolPolicy:
    """Conservative execution metadata for one MCP capability."""

    mutating: bool = True
    idempotent: bool = False
    external: bool = True
    parallel_child_safe: bool = False
    authority: str = "conservative_default"

    @property
    def read_only(self) -> bool:
        return not self.mutating

    def as_dict(self) -> dict[str, Any]:
        return {
            "mutating": self.mutating,
            "read_only": self.read_only,
            "idempotent": self.idempotent,
            "external": self.external,
            "parallel_child_safe": self.parallel_child_safe,
            "authority": self.authority,
        }


def _annotation_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            dumped = value.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
            return dumped if isinstance(dumped, dict) else {}
        except (TypeError, ValueError):
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def resolve_mcp_tool_policy(
    *,
    tool_annotations: Any = None,
    trust_server_annotations: bool = False,
    trusted_control_plane_override: Mapping[str, Any] | None = None,
) -> MCPToolPolicy:
    """Resolve policy without trusting ordinary server self-annotations.

    Risk can only be narrowed by a caller-supplied trust decision.  Skill
    package configuration and untrusted MCP ``annotations`` therefore cannot
    make a capability read-only, replay-safe, or parallel-child-safe.
    """

    mutating = True
    idempotent = False
    external = True
    parallel_requested = False
    authorities: list[str] = []

    if trust_server_annotations:
        annotations = _annotation_mapping(tool_annotations)
        if annotations.get("readOnlyHint") is True:
            mutating = False
        if annotations.get("idempotentHint") is True:
            idempotent = True
        if annotations:
            authorities.append("trusted_server_annotations")

    override = (
        dict(trusted_control_plane_override)
        if isinstance(trusted_control_plane_override, Mapping)
        else {}
    )
    if override:
        if (
            override.get("read_only") is True
            or override.get("mutating") is False
        ):
            mutating = False
        if override.get("idempotent") is True:
            idempotent = True
        if override.get("external") is False:
            external = False
        if override.get("parallel_child_safe") is True:
            parallel_requested = True
        authorities.append("trusted_control_plane")

    # Without a scoped/disjoint-mutation contract, concurrent child use is
    # safe only for a capability explicitly proven read-only and replay-safe.
    parallel_child_safe = parallel_requested and not mutating and idempotent
    return MCPToolPolicy(
        mutating=mutating,
        idempotent=idempotent,
        external=external,
        parallel_child_safe=parallel_child_safe,
        authority="+".join(authorities) if authorities else "conservative_default",
    )


def _bounded_name(value: Any, *, label: str) -> str:
    text = str(value or "")
    if (
        not text
        or len(text) > MCP_MAX_NAME_CHARS
        or "\x00" in text
        or any(ord(char) < 32 for char in text)
    ):
        raise MCPContractError(f"Invalid {label}")
    return text


def _bounded_description(value: Any, *, fallback: str) -> str:
    text = str(value or fallback)
    if len(text) <= MCP_MAX_DESCRIPTION_CHARS:
        return text
    return text[: MCP_MAX_DESCRIPTION_CHARS - 1] + "…"


@dataclass(frozen=True)
class FrozenMCPToolDescriptor:
    """One immutable MCP capability descriptor bound to a catalog revision."""

    server_name: str
    tool_name: str
    public_name: str
    description: str
    input_schema_json: str
    schema_sha256: str
    descriptor_sha256: str
    catalog_revision: str
    policy: MCPToolPolicy
    schema_version: int = MCP_CONTRACT_SCHEMA_VERSION

    @property
    def input_schema(self) -> dict[str, Any]:
        # Never expose the mutable object used to create the snapshot.
        return json.loads(self.input_schema_json)

    def model_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.public_name,
                "description": f"[MCP:{self.server_name}] {self.description}",
                "parameters": self.input_schema,
            },
        }

    def as_dict(self, *, include_schema: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "public_name": self.public_name,
            "description": self.description,
            "schema_sha256": self.schema_sha256,
            "descriptor_sha256": self.descriptor_sha256,
            "catalog_revision": self.catalog_revision,
            "policy": self.policy.as_dict(),
        }
        if include_schema:
            payload["input_schema"] = self.input_schema
        return payload


def build_mcp_tool_descriptor(
    *,
    server_name: str,
    tool_name: str,
    public_name: str,
    description: Any,
    input_schema: Any,
    tool_annotations: Any = None,
    trust_server_annotations: bool = False,
    trusted_control_plane_override: Mapping[str, Any] | None = None,
) -> FrozenMCPToolDescriptor:
    """Create a validated provisional descriptor.

    ``freeze_mcp_catalog`` assigns its content-addressed catalog revision.
    """

    server = _bounded_name(server_name, label="MCP server name")
    tool = _bounded_name(tool_name, label="MCP tool name")
    public = _bounded_name(public_name, label="public MCP tool name")
    normalized = normalize_mcp_input_schema(input_schema)
    _, schema_json = _bounded_json_clone(
        normalized,
        label="MCP inputSchema",
        max_bytes=MCP_MAX_SCHEMA_BYTES,
    )
    schema_digest = hashlib.sha256(schema_json.encode("utf-8")).hexdigest()
    policy = resolve_mcp_tool_policy(
        tool_annotations=tool_annotations,
        trust_server_annotations=trust_server_annotations,
        trusted_control_plane_override=trusted_control_plane_override,
    )
    normalized_description = _bounded_description(
        description,
        fallback=f"MCP tool: {tool}",
    )
    identity_payload = {
        "schema_version": MCP_CONTRACT_SCHEMA_VERSION,
        "server_name": server,
        "tool_name": tool,
        "public_name": public,
        "description": normalized_description,
        "schema_sha256": schema_digest,
        "policy": policy.as_dict(),
    }
    identity_json = json.dumps(
        identity_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    descriptor_digest = hashlib.sha256(
        identity_json.encode("utf-8"),
    ).hexdigest()
    return FrozenMCPToolDescriptor(
        server_name=server,
        tool_name=tool,
        public_name=public,
        description=normalized_description,
        input_schema_json=schema_json,
        schema_sha256=schema_digest,
        descriptor_sha256=descriptor_digest,
        catalog_revision="",
        policy=policy,
    )


@dataclass(frozen=True)
class MCPRejectedTool:
    server_name: str
    tool_name: str
    public_name: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "public_name": self.public_name,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FrozenMCPCatalog:
    """A deterministic MCP surface that can be retained for one run."""

    descriptors: tuple[FrozenMCPToolDescriptor, ...]
    catalog_revision: str
    rejected_tools: tuple[MCPRejectedTool, ...] = ()
    parent_catalog_revision: str | None = None
    live_catalog_revision: str | None = None
    # ``resolved`` means live discovery completed, even when it found no tools.
    # The other states are sealed-empty run boundaries: they deliberately
    # distinguish an unrequested capability from a requested catalog whose
    # freeze failed, and must never be reinterpreted as permission to rediscover
    # a later live catalog.
    resolution_status: str = "resolved"
    schema_version: int = MCP_CONTRACT_SCHEMA_VERSION

    def get(self, public_name: str) -> FrozenMCPToolDescriptor | None:
        for descriptor in self.descriptors:
            if descriptor.public_name == public_name:
                return descriptor
        return None

    @property
    def sealed_closed(self) -> bool:
        return self.resolution_status != "resolved"

    def model_definitions(self) -> list[dict[str, Any]]:
        return [descriptor.model_definition() for descriptor in self.descriptors]

    def as_dict(self, *, include_schemas: bool = True) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "catalog_revision": self.catalog_revision,
            "parent_catalog_revision": self.parent_catalog_revision,
            "live_catalog_revision": self.live_catalog_revision,
            "resolution_status": self.resolution_status,
            "descriptors": [
                descriptor.as_dict(include_schema=include_schemas)
                for descriptor in self.descriptors
            ],
            "rejected_tools": [
                rejected.as_dict() for rejected in self.rejected_tools
            ],
        }


def freeze_mcp_catalog(
    descriptors: Iterable[FrozenMCPToolDescriptor],
    *,
    rejected_tools: Iterable[MCPRejectedTool] = (),
    parent_catalog_revision: str | None = None,
    live_catalog_revision: str | None = None,
    resolution_status: str = "resolved",
) -> FrozenMCPCatalog:
    """Bind validated descriptors to one content-addressed catalog revision."""

    ordered = sorted(tuple(descriptors), key=lambda item: item.public_name)
    if resolution_status not in {
        "resolved",
        "not_enabled",
        "freeze_failed",
    }:
        raise MCPContractError(
            f"Unsupported MCP catalog resolution status: {resolution_status}",
        )
    if resolution_status != "resolved" and ordered:
        raise MCPContractError(
            "A sealed-closed MCP catalog cannot contain tool descriptors",
        )
    if len(ordered) > MCP_MAX_CATALOG_TOOLS:
        raise MCPContractError(
            f"MCP catalog exceeds the bounded {MCP_MAX_CATALOG_TOOLS}-tool limit",
            reason="mcp_contract_limit_exceeded",
        )
    seen: set[str] = set()
    for descriptor in ordered:
        if descriptor.public_name in seen:
            raise MCPContractError(
                f"Duplicate public MCP tool name: {descriptor.public_name}",
            )
        seen.add(descriptor.public_name)
    revision_payload = {
        "schema_version": MCP_CONTRACT_SCHEMA_VERSION,
        "resolution_status": resolution_status,
        "descriptors": [
            {
                "public_name": descriptor.public_name,
                "descriptor_sha256": descriptor.descriptor_sha256,
            }
            for descriptor in ordered
        ],
    }
    revision_json = json.dumps(
        revision_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    revision = hashlib.sha256(revision_json.encode("utf-8")).hexdigest()
    bound = tuple(
        replace(descriptor, catalog_revision=revision)
        for descriptor in ordered
    )
    return FrozenMCPCatalog(
        descriptors=bound,
        catalog_revision=revision,
        rejected_tools=tuple(rejected_tools),
        parent_catalog_revision=parent_catalog_revision,
        live_catalog_revision=live_catalog_revision,
        resolution_status=resolution_status,
    )


def sealed_empty_mcp_catalog(
    resolution_status: str,
    *,
    parent_catalog_revision: str | None = None,
) -> FrozenMCPCatalog:
    """Create an immutable no-MCP boundary for one run or descendant.

    Only explicit closed states are accepted.  In particular, callers cannot
    use this helper to manufacture an ordinary resolved catalog and later
    mistake it for a failed discovery attempt.
    """

    if resolution_status not in {"not_enabled", "freeze_failed"}:
        raise MCPContractError(
            "A sealed empty MCP catalog requires not_enabled or freeze_failed",
        )
    return freeze_mcp_catalog(
        (),
        parent_catalog_revision=parent_catalog_revision,
        resolution_status=resolution_status,
    )


@dataclass(frozen=True)
class MCPToolCallPreflightResult:
    """Pure result of validating one call against a frozen MCP descriptor."""

    descriptor: FrozenMCPToolDescriptor | None
    args: Any
    error_payload: dict[str, Any] | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error_payload is None

    def error_json(self) -> str:
        return json.dumps(
            self.error_payload or {"error": "MCP preflight failed"},
            ensure_ascii=False,
        )


def _preflight_error(
    *,
    descriptor: FrozenMCPToolDescriptor | None,
    args: Any,
    error: str,
    reason: str,
) -> MCPToolCallPreflightResult:
    payload: dict[str, Any] = {
        "error": error,
        "reason": reason,
    }
    if descriptor is not None:
        payload.update({
            "tool_name": descriptor.public_name,
            "schema_sha256": descriptor.schema_sha256,
            "catalog_revision": descriptor.catalog_revision,
        })
    return MCPToolCallPreflightResult(
        descriptor=descriptor,
        args=args,
        error_payload=payload,
        reason=reason,
    )


def preflight_mcp_tool_call(
    descriptor: FrozenMCPToolDescriptor,
    args: Any,
) -> MCPToolCallPreflightResult:
    """Validate one MCP call without transport access or mutation."""

    if not isinstance(args, dict):
        return _preflight_error(
            descriptor=descriptor,
            args=args,
            error=(
                f"MCP tool {descriptor.public_name} arguments must be a JSON "
                f"object; got {type(args).__name__}."
            ),
            reason="malformed_mcp_tool_arguments",
        )
    sentinel = next((key for key in _PARSE_SENTINELS if key in args), None)
    if sentinel is not None:
        return _preflight_error(
            descriptor=descriptor,
            args=args,
            error=(
                f"MCP tool {descriptor.public_name} received reserved provider "
                f"parse field {sentinel}; the call was not dispatched."
            ),
            reason="malformed_mcp_tool_arguments",
        )
    try:
        normalized_args, _ = _bounded_json_clone(
            args,
            label="MCP tool arguments",
            max_bytes=MCP_MAX_ARGUMENT_BYTES,
        )
    except MCPContractError as exc:
        return _preflight_error(
            descriptor=descriptor,
            args=args,
            error=str(exc),
            reason=exc.reason,
        )

    validation_error = json_schema_value_error(
        normalized_args,
        descriptor.input_schema,
        value_path="args",
        schema_path="inputSchema",
    )
    if validation_error:
        return _preflight_error(
            descriptor=descriptor,
            args=normalized_args,
            error=(
                f"MCP tool {descriptor.public_name} arguments failed schema "
                f"validation: {validation_error}."
            ),
            reason="mcp_tool_schema_validation_failed",
        )
    return MCPToolCallPreflightResult(
        descriptor=descriptor,
        args=normalized_args,
    )


@dataclass(frozen=True)
class MCPDescriptorDriftResult:
    """Result of comparing a run-frozen descriptor with live state."""

    ok: bool
    reason: str = ""
    changed_fields: tuple[str, ...] = ()
    error_payload: dict[str, Any] | None = None

    def error_json(self) -> str:
        return json.dumps(
            self.error_payload or {"error": "MCP capability changed"},
            ensure_ascii=False,
        )


def check_mcp_descriptor_drift(
    expected: FrozenMCPToolDescriptor,
    current: FrozenMCPToolDescriptor | None,
    *,
    require_catalog_revision: bool = False,
) -> MCPDescriptorDriftResult:
    """Compare two descriptors without disclosing either schema payload."""

    if current is None:
        return MCPDescriptorDriftResult(
            ok=False,
            reason="mcp_capability_unavailable",
            changed_fields=("availability",),
            error_payload={
                "error": (
                    f"MCP tool '{expected.public_name}' is unavailable in the "
                    "current session; the call was not dispatched."
                ),
                "reason": "mcp_capability_unavailable",
                "tool_name": expected.public_name,
                "expected_schema_sha256": expected.schema_sha256,
                "expected_catalog_revision": expected.catalog_revision,
            },
        )

    changed: list[str] = []
    for field in ("server_name", "tool_name", "public_name"):
        if getattr(expected, field) != getattr(current, field):
            changed.append(field)
    if expected.schema_sha256 != current.schema_sha256:
        changed.append("schema_sha256")
    if expected.policy != current.policy:
        changed.append("policy")
    if (
        require_catalog_revision
        and expected.catalog_revision != current.catalog_revision
    ):
        changed.append("catalog_revision")
    if not changed:
        return MCPDescriptorDriftResult(ok=True)
    return MCPDescriptorDriftResult(
        ok=False,
        reason="mcp_capability_changed",
        changed_fields=tuple(changed),
        error_payload={
            "error": (
                f"MCP capability '{expected.public_name}' changed after the "
                "run-scoped tool surface was frozen; the call was not dispatched."
            ),
            "reason": "mcp_capability_changed",
            "tool_name": expected.public_name,
            "changed_fields": changed,
            "expected_schema_sha256": expected.schema_sha256,
            "current_schema_sha256": current.schema_sha256,
            "expected_catalog_revision": expected.catalog_revision,
            "current_catalog_revision": current.catalog_revision,
        },
    )


def check_mcp_schema_drift(
    expected: FrozenMCPToolDescriptor,
    *,
    server_name: str,
    tool_name: str,
    public_name: str,
    input_schema: Any,
) -> MCPDescriptorDriftResult:
    """Compare a frozen descriptor with one live raw MCP tool definition.

    This narrower helper intentionally retains the frozen policy authority:
    server self-annotations observed during a refresh cannot alter policy, and
    a caller does not need to replay its trusted control-plane inputs merely to
    prove that the dispatch schema is unchanged.
    """

    try:
        normalized = normalize_mcp_input_schema(input_schema)
        _, schema_json = _bounded_json_clone(
            normalized,
            label="MCP inputSchema",
            max_bytes=MCP_MAX_SCHEMA_BYTES,
        )
        current = FrozenMCPToolDescriptor(
            server_name=_bounded_name(server_name, label="MCP server name"),
            tool_name=_bounded_name(tool_name, label="MCP tool name"),
            public_name=_bounded_name(
                public_name,
                label="public MCP tool name",
            ),
            description=expected.description,
            input_schema_json=schema_json,
            schema_sha256=hashlib.sha256(
                schema_json.encode("utf-8"),
            ).hexdigest(),
            descriptor_sha256=expected.descriptor_sha256,
            catalog_revision=expected.catalog_revision,
            policy=expected.policy,
        )
    except MCPContractError as exc:
        return MCPDescriptorDriftResult(
            ok=False,
            reason="mcp_capability_changed",
            changed_fields=("input_schema",),
            error_payload={
                "error": (
                    f"MCP capability '{expected.public_name}' now exposes an "
                    f"invalid schema: {exc}; the call was not dispatched."
                ),
                "reason": "mcp_capability_changed",
                "tool_name": expected.public_name,
                "changed_fields": ["input_schema"],
                "expected_schema_sha256": expected.schema_sha256,
                "current_schema_sha256": None,
                "expected_catalog_revision": expected.catalog_revision,
            },
        )
    return check_mcp_descriptor_drift(expected, current)


def intersect_mcp_catalogs(
    parent: FrozenMCPCatalog,
    live: FrozenMCPCatalog,
    *,
    allowed_tool_names: Iterable[str] | None = None,
) -> FrozenMCPCatalog:
    """Return the non-widening child intersection of parent and live catalogs.

    A child receives the parent's exact schema and policy, never a refreshed
    server descriptor.  Live state is consulted only to prove that the same
    server/tool/schema still exists. New tools, widened schemas, renamed
    routes, and invalid refreshed contracts are omitted fail-closed.
    """

    if parent.sealed_closed:
        return sealed_empty_mcp_catalog(
            parent.resolution_status,
            parent_catalog_revision=parent.catalog_revision,
        )

    parent_by_name = {
        descriptor.public_name: descriptor
        for descriptor in parent.descriptors
    }
    live_by_name = {
        descriptor.public_name: descriptor
        for descriptor in live.descriptors
    }
    if allowed_tool_names is None:
        requested_names = tuple(sorted(parent_by_name))
    else:
        requested_names = tuple(dict.fromkeys(
            str(name)
            for name in allowed_tool_names
            if isinstance(name, str) and name
        ))

    retained: list[FrozenMCPToolDescriptor] = []
    rejected: list[MCPRejectedTool] = []
    for public_name in requested_names:
        expected = parent_by_name.get(public_name)
        if expected is None:
            current = live_by_name.get(public_name)
            rejected.append(MCPRejectedTool(
                server_name=(
                    current.server_name if current is not None else "<unknown>"
                ),
                tool_name=(
                    current.tool_name if current is not None else "<unknown>"
                ),
                public_name=public_name,
                reason=(
                    "not authorized by the parent run's frozen MCP catalog"
                ),
            ))
            continue
        current = live_by_name.get(public_name)
        if current is None:
            rejected.append(MCPRejectedTool(
                server_name=expected.server_name,
                tool_name=expected.tool_name,
                public_name=public_name,
                reason=(
                    "parent-frozen MCP capability is unavailable in the live "
                    "session catalog"
                ),
            ))
            continue
        drift = check_mcp_schema_drift(
            expected,
            server_name=current.server_name,
            tool_name=current.tool_name,
            public_name=current.public_name,
            input_schema=current.input_schema,
        )
        if not drift.ok:
            changed = ", ".join(drift.changed_fields) or "contract"
            rejected.append(MCPRejectedTool(
                server_name=expected.server_name,
                tool_name=expected.tool_name,
                public_name=public_name,
                reason=(
                    "parent-frozen MCP capability drifted before child "
                    f"execution ({changed})"
                ),
            ))
            continue
        # ``freeze_mcp_catalog`` binds a child-intersection revision below.
        # Every other identity, schema, and policy field remains parent-owned.
        retained.append(replace(expected, catalog_revision=""))

    return freeze_mcp_catalog(
        retained,
        rejected_tools=rejected,
        parent_catalog_revision=parent.catalog_revision,
        live_catalog_revision=live.catalog_revision,
    )
