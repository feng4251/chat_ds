"""Deterministic compilation of Skill output contracts into artifact plans.

The compiler intentionally does not inspect the workspace and does not guess a
project name from prose.  It converts exact Skill declarations plus explicit
``key=value`` bindings into a closed set of paths and one merge operation.  A
plan with unresolved placeholders or ambiguous/unsafe declarations is returned
as non-dispatchable and can be rejected with :meth:`ArtifactPlan.require_valid`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


_NAMED_PLACEHOLDER_RE = re.compile(
    r"\{([A-Za-z][A-Za-z0-9_]*)\}|<([A-Za-z][A-Za-z0-9_]*)>"
)
_BINDING_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_LITERAL_PLACEHOLDER_RE = re.compile(
    r"(?:^|[_.-])(?:TBD|TODO|PLACEHOLDER|REPLACE_ME|YOUR_[A-Z0-9_]+)"
    r"(?:$|[_.-])",
    re.IGNORECASE,
)
_GLOB_META = frozenset("*?[]")
_MAX_ARTIFACTS = 256
_MAX_PATH_CHARS = 1024
_MAX_BINDING_CHARS = 128
_MAX_SEPARATOR_CHARS = 4096
_MAX_SECTION_METADATA_CHARS = 4096
_MAX_SECTION_SOURCE_FILE_CHARS = 1024
_MAX_SECTION_KEY_ELEMENTS_NODES = 512
_MAX_SECTION_KEY_ELEMENTS_DEPTH = 12
_MAX_SECTION_KEY_ELEMENTS_JSON_CHARS = 65536


@dataclass(frozen=True)
class ArtifactPlanDiagnostic:
    """Stable machine-readable compiler finding."""

    code: str
    message: str
    field: str = ""

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.field:
            result["field"] = self.field
        return result


@dataclass(frozen=True)
class ArtifactPathPlan:
    """One declared artifact template and its optional concrete path."""

    kind: str
    template: str
    path: str | None
    unresolved_placeholders: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.path is not None and not self.unresolved_placeholders

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "template": self.template,
            "path": self.path,
            "resolved": self.resolved,
            "unresolved_placeholders": list(self.unresolved_placeholders),
        }


@dataclass(frozen=True)
class ArtifactSectionPlan:
    """A report section's exact declared evidence-worker ownership."""

    section_id: str
    title: str
    order: int
    source_workers: tuple[str, ...]
    artifact_template: str | None = None
    artifact_path: str | None = None
    applicability: str | None = None
    source_file: str | None = None
    key_elements_json: str | None = None
    activation_status: str = "unknown"
    selected_source_workers: tuple[str, ...] | None = None
    unselected_source_workers: tuple[str, ...] | None = None

    @property
    def key_elements(self) -> Any:
        """Return a fresh JSON value so the frozen plan stays deeply immutable."""
        if self.key_elements_json is None:
            return None
        return json.loads(self.key_elements_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "order": self.order,
            "source_workers": list(self.source_workers),
            "artifact_template": self.artifact_template,
            "artifact_path": self.artifact_path,
            "applicability": self.applicability,
            "key_elements": self.key_elements,
            "source_file": self.source_file,
            "activation_status": self.activation_status,
            "selected_source_workers": (
                list(self.selected_source_workers)
                if self.selected_source_workers is not None else None
            ),
            "unselected_source_workers": (
                list(self.unselected_source_workers)
                if self.unselected_source_workers is not None else None
            ),
        }


@dataclass(frozen=True)
class ArtifactMergePlan:
    """The only merge operation authorized by the compiled contract."""

    required: bool
    dispatchable: bool
    input_templates: tuple[str, ...]
    input_paths: tuple[str, ...]
    output_template: str | None
    output_path: str | None
    separator: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "dispatchable": self.dispatchable,
            "input_templates": list(self.input_templates),
            "input_paths": list(self.input_paths),
            "output_template": self.output_template,
            "output_path": self.output_path,
            "separator": self.separator,
        }


class ArtifactPlanError(ValueError):
    """Raised when a caller tries to dispatch an invalid artifact plan."""

    def __init__(self, plan: "ArtifactPlan") -> None:
        self.plan = plan
        codes = ", ".join(item.code for item in plan.errors) or "invalid_plan"
        super().__init__(f"Artifact plan is not dispatchable: {codes}")


@dataclass(frozen=True)
class ArtifactPlan:
    """Immutable deterministic output of :func:`compile_artifact_plan`."""

    schema_version: str
    plan_id: str
    valid: bool
    bindings: tuple[tuple[str, str], ...]
    deliverable_artifacts: tuple[ArtifactPathPlan, ...]
    modular_artifacts: tuple[ArtifactPathPlan, ...]
    ancillary_artifacts: tuple[ArtifactPathPlan, ...]
    final_artifact: ArtifactPathPlan | None
    selected_workers: tuple[str, ...] | None
    sections: tuple[ArtifactSectionPlan, ...]
    required_markers: tuple[str, ...]
    merge: ArtifactMergePlan
    unresolved_placeholders: tuple[str, ...]
    errors: tuple[ArtifactPlanDiagnostic, ...]
    warnings: tuple[ArtifactPlanDiagnostic, ...]

    @property
    def modular_paths(self) -> tuple[str, ...]:
        return tuple(
            artifact.path
            for artifact in self.modular_artifacts
            if artifact.path is not None
        )

    @property
    def deliverable_paths(self) -> tuple[str, ...]:
        return tuple(
            artifact.path
            for artifact in self.deliverable_artifacts
            if artifact.path is not None
        )

    @property
    def ancillary_paths(self) -> tuple[str, ...]:
        return tuple(
            artifact.path
            for artifact in self.ancillary_artifacts
            if artifact.path is not None
        )

    @property
    def final_path(self) -> str | None:
        return self.final_artifact.path if self.final_artifact else None

    def require_valid(self) -> "ArtifactPlan":
        if not self.valid or (self.merge.required and not self.merge.dispatchable):
            raise ArtifactPlanError(self)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "valid": self.valid,
            "bindings": dict(self.bindings),
            "deliverable_artifacts": [
                item.to_dict() for item in self.deliverable_artifacts
            ],
            "modular_artifacts": [item.to_dict() for item in self.modular_artifacts],
            "ancillary_artifacts": [item.to_dict() for item in self.ancillary_artifacts],
            "final_artifact": (
                self.final_artifact.to_dict() if self.final_artifact else None
            ),
            "selection_context": (
                {"selected_workers": list(self.selected_workers)}
                if self.selected_workers is not None else None
            ),
            "sections": [item.to_dict() for item in self.sections],
            "section_to_source_workers": {
                item.section_id: list(item.source_workers) for item in self.sections
            },
            "section_activation": {
                item.section_id: item.activation_status for item in self.sections
            },
            "active_section_ids": [
                item.section_id
                for item in self.sections
                if item.activation_status == "active"
            ],
            "inactive_section_ids": [
                item.section_id
                for item in self.sections
                if item.activation_status == "inactive"
            ],
            "required_markers": list(self.required_markers),
            "merge": self.merge.to_dict(),
            "unresolved_placeholders": list(self.unresolved_placeholders),
            "errors": [item.to_dict() for item in self.errors],
            "warnings": [item.to_dict() for item in self.warnings],
        }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _merge_is_required(output: Mapping[str, Any]) -> bool:
    """Resolve explicit merge policy while preserving legacy tri-state input."""

    mandatory = output.get("merge_mandatory")
    if isinstance(mandatory, bool):
        return mandatory
    command = output.get("merge_command")
    return isinstance(command, str) and bool(command.strip())


def _string_list(value: Any) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def _normalize_contracts(
    execution_contract: Mapping[str, Any],
    output_contract: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    nested = execution_contract.get("execution_contract")
    execution = nested if isinstance(nested, Mapping) else execution_contract
    output = (
        output_contract
        if isinstance(output_contract, Mapping)
        else _as_mapping(execution.get("output_contract"))
        or _as_mapping(execution_contract.get("output_contract"))
    )
    quality = (
        _as_mapping(execution.get("quality_contract"))
        or _as_mapping(execution_contract.get("quality_contract"))
    )
    return execution, output, quality


def _normalize_bindings(
    bindings: Mapping[str, str] | None,
    errors: list[ArtifactPlanDiagnostic],
) -> dict[str, tuple[str, str]]:
    if bindings is None:
        return {}
    if not isinstance(bindings, Mapping):
        errors.append(ArtifactPlanDiagnostic(
            "bindings_not_mapping",
            "Artifact bindings must be an explicit key=value mapping.",
            "bindings",
        ))
        return {}
    normalized: dict[str, tuple[str, str]] = {}
    for raw_key, raw_value in bindings.items():
        if not isinstance(raw_key, str) or not _BINDING_KEY_RE.fullmatch(raw_key):
            errors.append(ArtifactPlanDiagnostic(
                "invalid_binding_key",
                "Binding keys must be single identifier names.",
                "bindings",
            ))
            continue
        folded = raw_key.casefold()
        if folded in normalized:
            errors.append(ArtifactPlanDiagnostic(
                "duplicate_binding_key",
                "Binding keys are unique case-insensitively.",
                f"bindings.{raw_key}",
            ))
            continue
        if not isinstance(raw_value, str) or not _binding_value_is_safe(raw_value):
            errors.append(ArtifactPlanDiagnostic(
                "unsafe_binding_value",
                "Binding values must be bounded single path-component values.",
                f"bindings.{raw_key}",
            ))
            continue
        normalized[folded] = (raw_key, raw_value)
    return normalized


def _binding_value_is_safe(value: str) -> bool:
    if not value or value != value.strip() or len(value) > _MAX_BINDING_CHARS:
        return False
    if value in {".", ".."} or "/" in value or "\\" in value:
        return False
    if any(ord(char) < 32 or char in "{}<>*?[]:" for char in value):
        return False
    if _LITERAL_PLACEHOLDER_RE.search(value):
        return False
    return True


def _safe_exact_relative_path(path: str) -> bool:
    if not path or path != path.strip() or len(path) > _MAX_PATH_CHARS:
        return False
    if path.startswith(("/", "~")) or "\\" in path or "//" in path:
        return False
    if re.match(r"^[A-Za-z]:", path):
        return False
    if any(ord(char) < 32 or char in _GLOB_META or char == ":" for char in path):
        return False
    raw_parts = path.split("/")
    if any(
        not part
        or part in {".", ".."}
        or part != part.strip()
        or len(part) > 255
        for part in raw_parts
    ):
        return False
    parsed = PurePosixPath(path)
    return not parsed.is_absolute() and str(parsed) == path


def _render_artifact_path(
    kind: str,
    template: Any,
    field: str,
    bindings: Mapping[str, tuple[str, str]],
    errors: list[ArtifactPlanDiagnostic],
) -> ArtifactPathPlan:
    if not isinstance(template, str) or not template or template != template.strip():
        errors.append(ArtifactPlanDiagnostic(
            "invalid_artifact_path",
            "Artifact declarations must be non-empty exact relative path strings.",
            field,
        ))
        return ArtifactPathPlan(kind, str(template or ""), None)

    declared_names: list[str] = []

    def replace_placeholder(match: re.Match[str]) -> str:
        name = str(match.group(1) or match.group(2))
        declared_names.append(name)
        bound = bindings.get(name.casefold())
        return bound[1] if bound is not None else match.group(0)

    rendered = _NAMED_PLACEHOLDER_RE.sub(replace_placeholder, template)
    syntax_probe = _NAMED_PLACEHOLDER_RE.sub("BOUND", template)
    if any(char in syntax_probe for char in "{}<>") or re.search(
        r"\$[A-Za-z_{]", syntax_probe
    ):
        errors.append(ArtifactPlanDiagnostic(
            "invalid_placeholder_syntax",
            "Artifact paths may use only named {KEY} or <KEY> placeholders.",
            field,
        ))
    if _LITERAL_PLACEHOLDER_RE.search(syntax_probe):
        errors.append(ArtifactPlanDiagnostic(
            "literal_placeholder_path",
            "Artifact paths may not use literal TODO/TBD/placeholder names.",
            field,
        ))

    unresolved = tuple(sorted({
        name for name in declared_names if name.casefold() not in bindings
    }, key=str.casefold))
    safety_probe = _NAMED_PLACEHOLDER_RE.sub("UNRESOLVED", rendered)
    safe = _safe_exact_relative_path(safety_probe)
    if not safe:
        errors.append(ArtifactPlanDiagnostic(
            "unsafe_artifact_path",
            "Artifact paths must remain inside the workspace and contain no glob/traversal syntax.",
            field,
        ))
    for name in unresolved:
        errors.append(ArtifactPlanDiagnostic(
            "unresolved_artifact_placeholder",
            f"Artifact placeholder {name!r} has no explicit binding.",
            field,
        ))
    return ArtifactPathPlan(
        kind=kind,
        template=template,
        path=rendered if safe and not unresolved else None,
        unresolved_placeholders=unresolved,
    )


def _path_list(
    kind: str,
    value: Any,
    field: str,
    bindings: Mapping[str, tuple[str, str]],
    errors: list[ArtifactPlanDiagnostic],
) -> tuple[ArtifactPathPlan, ...]:
    items = _string_list(value)
    if items is None:
        errors.append(ArtifactPlanDiagnostic(
            "artifact_list_not_strings",
            "Artifact path lists must contain only strings.",
            field,
        ))
        return ()
    if len(items) > _MAX_ARTIFACTS:
        errors.append(ArtifactPlanDiagnostic(
            "artifact_list_too_large",
            f"Artifact plans support at most {_MAX_ARTIFACTS} paths per category.",
            field,
        ))
        items = items[:_MAX_ARTIFACTS]
    return tuple(
        _render_artifact_path(kind, item, f"{field}[{index}]", bindings, errors)
        for index, item in enumerate(items)
    )


def _collect_final_template(
    output: Mapping[str, Any],
    errors: list[ArtifactPlanDiagnostic],
) -> str | None:
    declared = output.get("declared_final_artifact")
    candidates: list[str] = []
    if declared is not None:
        if isinstance(declared, str) and declared:
            candidates.append(declared)
        else:
            errors.append(ArtifactPlanDiagnostic(
                "invalid_final_artifact",
                "declared_final_artifact must be one exact path string.",
                "output_contract.declared_final_artifact",
            ))

    for index, declaration in enumerate(output.get("merge_declarations") or []):
        if not isinstance(declaration, Mapping):
            continue
        target = declaration.get("output_artifact")
        if target is None:
            continue
        if not isinstance(target, str) or not target:
            errors.append(ArtifactPlanDiagnostic(
                "invalid_final_artifact",
                "Merge output_artifact must be one exact path string.",
                f"output_contract.merge_declarations[{index}].output_artifact",
            ))
            continue
        candidates.append(target)

    if not candidates and declared is None:
        format_finals = _string_list(output.get("declared_format_final_artifacts"))
        if format_finals is None:
            errors.append(ArtifactPlanDiagnostic(
                "invalid_final_artifact_list",
                "declared_format_final_artifacts must contain only strings.",
                "output_contract.declared_format_final_artifacts",
            ))
        elif len(format_finals) == 1:
            candidates.extend(format_finals)
        elif len(format_finals) > 1:
            errors.append(ArtifactPlanDiagnostic(
                "ambiguous_final_artifact",
                "Multiple final artifacts are declared and no canonical target is selected.",
                "output_contract.declared_format_final_artifacts",
            ))
            candidates.extend(format_finals[:1])

    unique = list(dict.fromkeys(candidates))
    if len(unique) > 1:
        errors.append(ArtifactPlanDiagnostic(
            "ambiguous_merge_target",
            "The Skill declares more than one distinct merge target.",
            "output_contract",
        ))
    return unique[0] if unique else None


def _declared_worker_ids(execution: Mapping[str, Any]) -> set[str]:
    result = {
        str(item) for item in execution.get("worker_ids") or []
        if isinstance(item, str) and item
    }
    for worker in execution.get("workers") or []:
        if isinstance(worker, Mapping) and isinstance(worker.get("id"), str):
            result.add(str(worker["id"]))
    return result


def _normalize_selected_workers(
    selection_context: Mapping[str, Any] | None,
    declared_workers: set[str],
    errors: list[ArtifactPlanDiagnostic],
) -> tuple[str, ...] | None:
    """Validate the optional route projection without inventing a selection."""
    if selection_context is None:
        return None
    if not isinstance(selection_context, Mapping):
        errors.append(ArtifactPlanDiagnostic(
            "selection_context_not_mapping",
            "Artifact selection_context must be an object.",
            "selection_context",
        ))
        return None
    if "selected_workers" not in selection_context:
        errors.append(ArtifactPlanDiagnostic(
            "selected_workers_missing",
            "Artifact selection_context must declare selected_workers explicitly.",
            "selection_context.selected_workers",
        ))
        return None
    raw_workers = _string_list(selection_context.get("selected_workers"))
    if raw_workers is None:
        errors.append(ArtifactPlanDiagnostic(
            "selected_workers_not_list",
            "selection_context.selected_workers must be an exact string list.",
            "selection_context.selected_workers",
        ))
        return None
    if any(not worker or worker != worker.strip() for worker in raw_workers):
        errors.append(ArtifactPlanDiagnostic(
            "invalid_selected_worker",
            "Selected worker ids must be non-empty strings without surrounding whitespace.",
            "selection_context.selected_workers",
        ))
    if len(set(raw_workers)) != len(raw_workers):
        errors.append(ArtifactPlanDiagnostic(
            "duplicate_selected_worker",
            "A worker may appear only once in selection_context.selected_workers.",
            "selection_context.selected_workers",
        ))
    selected_workers = tuple(sorted(
        {worker for worker in raw_workers if worker and worker == worker.strip()},
        key=str.casefold,
    ))
    if declared_workers:
        for worker in selected_workers:
            if worker not in declared_workers:
                errors.append(ArtifactPlanDiagnostic(
                    "unknown_selected_worker",
                    f"Selected worker {worker!r} is not declared by the execution contract.",
                    "selection_context.selected_workers",
                ))
    return selected_workers


def _bounded_section_text(
    value: Any,
    *,
    field: str,
    limit: int,
    errors: list[ArtifactPlanDiagnostic],
) -> str | None:
    """Preserve one optional normalized string exactly or fail without truncation."""
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(ArtifactPlanDiagnostic(
            "invalid_section_metadata",
            "Section metadata must be a string when declared.",
            field,
        ))
        return None
    if len(value) > limit:
        errors.append(ArtifactPlanDiagnostic(
            "section_metadata_limit_exceeded",
            f"Section metadata exceeds the {limit}-character compiler limit.",
            field,
        ))
        return None
    return value


def _canonical_section_key_elements(
    value: Any,
    *,
    field: str,
    errors: list[ArtifactPlanDiagnostic],
) -> str | None:
    """Return bounded canonical JSON, preserving safe declarative content exactly."""
    if value is None:
        return None

    nodes = 0
    scalar_chars = 0
    active: set[int] = set()

    def validate(node: Any, depth: int) -> None:
        nonlocal nodes, scalar_chars
        nodes += 1
        if nodes > _MAX_SECTION_KEY_ELEMENTS_NODES:
            raise ValueError("node_limit")
        if depth > _MAX_SECTION_KEY_ELEMENTS_DEPTH:
            raise ValueError("depth_limit")
        if isinstance(node, str):
            scalar_chars += len(node)
            if scalar_chars > _MAX_SECTION_KEY_ELEMENTS_JSON_CHARS:
                raise ValueError("scalar_limit")
            return
        if node is None or isinstance(node, (int, float, bool)):
            return
        if isinstance(node, Mapping):
            identity = id(node)
            if identity in active:
                raise ValueError("cycle")
            active.add(identity)
            try:
                for key, child in node.items():
                    if not isinstance(key, str):
                        raise TypeError("mapping_key")
                    validate(key, depth + 1)
                    validate(child, depth + 1)
            finally:
                active.discard(identity)
            return
        if isinstance(node, (list, tuple)):
            identity = id(node)
            if identity in active:
                raise ValueError("cycle")
            active.add(identity)
            try:
                for child in node:
                    validate(child, depth + 1)
            finally:
                active.discard(identity)
            return
        raise TypeError("value_type")

    try:
        validate(value, 0)
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        reason = str(exc)
        limit_exceeded = reason in {
            "node_limit", "depth_limit", "scalar_limit",
        }
        errors.append(ArtifactPlanDiagnostic(
            (
                "section_key_elements_limit_exceeded"
                if limit_exceeded else "invalid_section_key_elements"
            ),
            (
                "Section key_elements exceeds the compiler's bounded JSON limits."
                if limit_exceeded
                else "Section key_elements must be bounded, acyclic JSON data "
                f"({reason or type(exc).__name__})."
            ),
            field,
        ))
        return None
    if len(encoded) > _MAX_SECTION_KEY_ELEMENTS_JSON_CHARS:
        errors.append(ArtifactPlanDiagnostic(
            "section_key_elements_limit_exceeded",
            "Section key_elements exceeds the "
            f"{_MAX_SECTION_KEY_ELEMENTS_JSON_CHARS}-character compiler limit.",
            field,
        ))
        return None
    return encoded


def _compile_sections(
    execution: Mapping[str, Any],
    output: Mapping[str, Any],
    quality: Mapping[str, Any],
    all_artifacts: Sequence[ArtifactPathPlan],
    bindings: Mapping[str, tuple[str, str]],
    selected_workers: tuple[str, ...] | None,
    errors: list[ArtifactPlanDiagnostic],
) -> tuple[ArtifactSectionPlan, ...]:
    raw_sections = output.get("sections") or []
    if not isinstance(raw_sections, list):
        errors.append(ArtifactPlanDiagnostic(
            "sections_not_list",
            "Output sections must be an ordered list.",
            "output_contract.sections",
        ))
        return ()
    worker_ids = _declared_worker_ids(execution)
    normalized: list[
        tuple[
            int,
            int,
            str,
            str,
            tuple[str, ...],
            str | None,
            str | None,
            str | None,
            str,
            tuple[str, ...] | None,
            tuple[str, ...] | None,
        ]
    ] = []
    seen_sections: set[str] = set()
    seen_orders: set[int] = set()
    for index, raw in enumerate(raw_sections):
        if not isinstance(raw, Mapping):
            errors.append(ArtifactPlanDiagnostic(
                "section_not_object",
                "Every output section must be an object.",
                f"output_contract.sections[{index}]",
            ))
            continue
        section_id = str(raw.get("id") or "").strip()
        if not section_id:
            errors.append(ArtifactPlanDiagnostic(
                "section_id_missing",
                "Every output section needs an exact id.",
                f"output_contract.sections[{index}].id",
            ))
            continue
        folded = section_id.casefold()
        if folded in seen_sections:
            errors.append(ArtifactPlanDiagnostic(
                "duplicate_section_id",
                "Output section ids are unique case-insensitively.",
                f"output_contract.sections[{index}].id",
            ))
            continue
        seen_sections.add(folded)
        title = str(raw.get("title") or raw.get("section") or section_id).strip()
        raw_workers = _string_list(raw.get("source_workers") or raw.get("source_worker"))
        if raw_workers is None:
            errors.append(ArtifactPlanDiagnostic(
                "source_workers_not_list",
                "Section source_workers must be a string list.",
                f"output_contract.sections[{index}].source_workers",
            ))
            raw_workers = []
        if len(set(raw_workers)) != len(raw_workers):
            errors.append(ArtifactPlanDiagnostic(
                "duplicate_section_source_worker",
                "A source worker may appear only once per section.",
                f"output_contract.sections[{index}].source_workers",
            ))
        source_workers = tuple(dict.fromkeys(raw_workers))
        if worker_ids:
            for worker in source_workers:
                if worker not in worker_ids:
                    errors.append(ArtifactPlanDiagnostic(
                        "unknown_section_source_worker",
                        f"Section source worker {worker!r} is not declared by the execution contract.",
                        f"output_contract.sections[{index}].source_workers",
                    ))
        raw_order = raw.get("order", index + 1)
        if (
            not isinstance(raw_order, int)
            or isinstance(raw_order, bool)
            or raw_order < 1
        ):
            errors.append(ArtifactPlanDiagnostic(
                "invalid_section_order",
                "Section order must be a positive integer.",
                f"output_contract.sections[{index}].order",
            ))
            order = index + 1
        else:
            order = raw_order
        if order in seen_orders:
            errors.append(ArtifactPlanDiagnostic(
                "duplicate_section_order",
                "Section order values must be unique.",
                f"output_contract.sections[{index}].order",
            ))
        seen_orders.add(order)
        applicability = _bounded_section_text(
            raw.get("applicability"),
            field=f"output_contract.sections[{index}].applicability",
            limit=_MAX_SECTION_METADATA_CHARS,
            errors=errors,
        )
        source_file = _bounded_section_text(
            raw.get("source_file"),
            field=f"output_contract.sections[{index}].source_file",
            limit=_MAX_SECTION_SOURCE_FILE_CHARS,
            errors=errors,
        )
        key_elements_json = _canonical_section_key_elements(
            raw.get("key_elements"),
            field=f"output_contract.sections[{index}].key_elements",
            errors=errors,
        )
        if selected_workers is None:
            activation_status = "unknown"
            selected_source_workers = None
            unselected_source_workers = None
        else:
            selected_set = set(selected_workers)
            selected_source_workers = tuple(
                worker for worker in source_workers if worker in selected_set
            )
            unselected_source_workers = tuple(
                worker for worker in source_workers if worker not in selected_set
            )
            activation_status = (
                "active"
                if not source_workers or selected_source_workers
                else "inactive"
            )
        normalized.append((
            order,
            index,
            section_id,
            title,
            source_workers,
            applicability,
            key_elements_json,
            source_file,
            activation_status,
            selected_source_workers,
            unselected_source_workers,
        ))

    artifact_by_template: dict[str, list[ArtifactPathPlan]] = {}
    artifact_by_path: dict[str, list[ArtifactPathPlan]] = {}
    for artifact in all_artifacts:
        artifact_by_template.setdefault(artifact.template.casefold(), []).append(artifact)
        if artifact.path:
            artifact_by_path.setdefault(artifact.path.casefold(), []).append(artifact)

    section_artifacts: dict[str, ArtifactPathPlan] = {}
    raw_mappings = quality.get("section_file_mapping") or []
    if not isinstance(raw_mappings, list):
        errors.append(ArtifactPlanDiagnostic(
            "section_file_mapping_not_list",
            "Section/file mappings must be an ordered list.",
            "quality_contract.section_file_mapping",
        ))
        raw_mappings = []
    for index, mapping in enumerate(raw_mappings):
        if not isinstance(mapping, Mapping):
            continue
        file_template = mapping.get("file")
        if not isinstance(file_template, str) or not file_template:
            errors.append(ArtifactPlanDiagnostic(
                "section_mapping_file_missing",
                "Section/file mappings need an exact file path.",
                f"quality_contract.section_file_mapping[{index}].file",
            ))
            continue
        rendered = _render_artifact_path(
            "section_mapping",
            file_template,
            f"quality_contract.section_file_mapping[{index}].file",
            bindings,
            errors,
        )
        matches = list(artifact_by_template.get(file_template.casefold(), []))
        if rendered.path:
            matches.extend(artifact_by_path.get(rendered.path.casefold(), []))
        matches = list({id(item): item for item in matches}.values())
        if len(matches) != 1:
            errors.append(ArtifactPlanDiagnostic(
                "section_mapping_artifact_match_error",
                "Each section/file mapping must match exactly one declared artifact.",
                f"quality_contract.section_file_mapping[{index}].file",
            ))
            continue
        section_ids = _string_list(mapping.get("section_ids"))
        if section_ids is None:
            errors.append(ArtifactPlanDiagnostic(
                "section_mapping_ids_not_list",
                "section_ids must be an exact string list.",
                f"quality_contract.section_file_mapping[{index}].section_ids",
            ))
            continue
        for section_id in section_ids:
            folded = section_id.casefold()
            if folded not in seen_sections:
                errors.append(ArtifactPlanDiagnostic(
                    "unknown_section_mapping_id",
                    f"Section mapping references undeclared section {section_id!r}.",
                    f"quality_contract.section_file_mapping[{index}].section_ids",
                ))
                continue
            previous = section_artifacts.get(folded)
            if previous is not None:
                errors.append(ArtifactPlanDiagnostic(
                    "duplicate_section_artifact_match",
                    "A section may be mapped exactly once.",
                    f"quality_contract.section_file_mapping[{index}].section_ids",
                ))
                continue
            section_artifacts[folded] = matches[0]

    return tuple(
        ArtifactSectionPlan(
            section_id=section_id,
            title=title,
            order=order,
            source_workers=source_workers,
            artifact_template=(
                section_artifacts[section_id.casefold()].template
                if section_id.casefold() in section_artifacts else None
            ),
            artifact_path=(
                section_artifacts[section_id.casefold()].path
                if section_id.casefold() in section_artifacts else None
            ),
            applicability=applicability,
            key_elements_json=key_elements_json,
            source_file=source_file,
            activation_status=activation_status,
            selected_source_workers=selected_source_workers,
            unselected_source_workers=unselected_source_workers,
        )
        for (
            order,
            _,
            section_id,
            title,
            source_workers,
            applicability,
            key_elements_json,
            source_file,
            activation_status,
            selected_source_workers,
            unselected_source_workers,
        ) in sorted(normalized)
    )


def _required_markers(
    output: Mapping[str, Any],
    quality: Mapping[str, Any],
    errors: list[ArtifactPlanDiagnostic],
) -> tuple[str, ...]:
    result: list[str] = []
    for field, value in (
        ("quality_contract.required_module_markers", quality.get("required_module_markers")),
        ("output_contract.required_markers", output.get("required_markers")),
    ):
        values = _string_list(value)
        if values is None:
            errors.append(ArtifactPlanDiagnostic(
                "required_markers_not_list",
                "Required markers must be exact string lists.",
                field,
            ))
            continue
        for marker in values:
            if not marker or marker != marker.strip():
                errors.append(ArtifactPlanDiagnostic(
                    "invalid_required_marker",
                    "Required markers must be non-empty strings without surrounding whitespace.",
                    field,
                ))
                continue
            if marker in result:
                errors.append(ArtifactPlanDiagnostic(
                    "duplicate_required_marker",
                    "Required markers may be declared only once.",
                    field,
                ))
                continue
            result.append(marker)
    return tuple(result)


def _compile_merge_order(
    output: Mapping[str, Any],
    modular: Sequence[ArtifactPathPlan],
    bindings: Mapping[str, tuple[str, str]],
    errors: list[ArtifactPlanDiagnostic],
) -> tuple[ArtifactPathPlan, ...]:
    explicit: Any = None
    explicit_field = ""
    for key in ("merge_input_order", "merge_input_files", "merge_inputs"):
        if key in output:
            explicit = output.get(key)
            explicit_field = f"output_contract.{key}"
            break
    declaration_orders: list[list[str]] = []
    for declaration in output.get("merge_declarations") or []:
        if not isinstance(declaration, Mapping):
            continue
        for key in ("input_order", "input_files", "inputs"):
            if key in declaration:
                values = _string_list(declaration.get(key))
                if values is not None:
                    declaration_orders.append(values)
                break
    unique_declaration_orders = list(dict.fromkeys(
        tuple(order) for order in declaration_orders
    ))
    if len(unique_declaration_orders) > 1:
        errors.append(ArtifactPlanDiagnostic(
            "ambiguous_merge_input_order",
            "Merge declarations specify more than one input order.",
            "output_contract.merge_declarations",
        ))
    if explicit is None and unique_declaration_orders:
        explicit = list(unique_declaration_orders[0])
        explicit_field = "output_contract.merge_declarations"
    elif explicit is not None and unique_declaration_orders:
        values = _string_list(explicit)
        if values is not None and tuple(values) != unique_declaration_orders[0]:
            errors.append(ArtifactPlanDiagnostic(
                "conflicting_merge_input_order",
                "Top-level and nested merge input orders differ.",
                "output_contract",
            ))

    if explicit is None:
        return tuple(modular)
    values = _string_list(explicit)
    if values is None:
        errors.append(ArtifactPlanDiagnostic(
            "merge_input_order_not_list",
            "Merge input order must be an exact string list.",
            explicit_field,
        ))
        return ()

    selected: list[ArtifactPathPlan] = []
    used_indexes: set[int] = set()
    for index, item in enumerate(values):
        rendered = _render_artifact_path(
            "merge_input",
            item,
            f"{explicit_field}[{index}]",
            bindings,
            errors,
        )
        matches = [
            module_index
            for module_index, module in enumerate(modular)
            if module.template.casefold() == item.casefold()
            or (
                rendered.path is not None
                and module.path is not None
                and module.path.casefold() == rendered.path.casefold()
            )
        ]
        if len(matches) != 1:
            errors.append(ArtifactPlanDiagnostic(
                "merge_input_match_error",
                "Each merge input must match exactly one declared modular artifact.",
                f"{explicit_field}[{index}]",
            ))
            continue
        module_index = matches[0]
        if module_index in used_indexes:
            errors.append(ArtifactPlanDiagnostic(
                "duplicate_merge_input",
                "Each modular artifact may occur only once in the merge plan.",
                f"{explicit_field}[{index}]",
            ))
            continue
        used_indexes.add(module_index)
        selected.append(modular[module_index])
    if len(selected) != len(modular) or len(used_indexes) != len(modular):
        errors.append(ArtifactPlanDiagnostic(
            "incomplete_merge_input_order",
            "The merge plan must contain every modular artifact exactly once.",
            explicit_field,
        ))
    return tuple(selected)


def _merge_separator(
    output: Mapping[str, Any],
    errors: list[ArtifactPlanDiagnostic],
) -> str:
    candidates: list[Any] = []
    if "merge_separator" in output:
        candidates.append(output.get("merge_separator"))
    for declaration in output.get("merge_declarations") or []:
        if isinstance(declaration, Mapping) and "separator" in declaration:
            candidates.append(declaration.get("separator"))
    normalized = list(dict.fromkeys(
        value for value in candidates if isinstance(value, str)
    ))
    if any(not isinstance(value, str) for value in candidates):
        errors.append(ArtifactPlanDiagnostic(
            "merge_separator_not_string",
            "Merge separator must be a string.",
            "output_contract.merge_separator",
        ))
    if len(normalized) > 1:
        errors.append(ArtifactPlanDiagnostic(
            "ambiguous_merge_separator",
            "Merge declarations specify different separators.",
            "output_contract",
        ))
    separator = normalized[0] if normalized else ""
    if len(separator) > _MAX_SEPARATOR_CHARS or any(
        ord(char) < 32 and char not in "\n\r\t" for char in separator
    ):
        errors.append(ArtifactPlanDiagnostic(
            "unsafe_merge_separator",
            "Merge separator is too large or contains unsafe control characters.",
            "output_contract.merge_separator",
        ))
        return ""
    return separator


def _validate_counts_and_uniqueness(
    output: Mapping[str, Any],
    deliverables: Sequence[ArtifactPathPlan],
    modular: Sequence[ArtifactPathPlan],
    ancillary: Sequence[ArtifactPathPlan],
    final: ArtifactPathPlan | None,
    bindings: Mapping[str, tuple[str, str]],
    errors: list[ArtifactPlanDiagnostic],
) -> None:
    all_artifacts = (
        list(deliverables)
        + list(modular)
        + list(ancillary)
        + ([final] if final else [])
    )
    seen_templates: dict[str, int] = {}
    seen_paths: dict[str, int] = {}
    for index, artifact in enumerate(all_artifacts):
        template_key = artifact.template.casefold()
        if template_key in seen_templates:
            errors.append(ArtifactPlanDiagnostic(
                "duplicate_artifact_template",
                "Every declared artifact template must be unique.",
                f"artifacts[{index}]",
            ))
        seen_templates[template_key] = index
        if artifact.path:
            path_key = artifact.path.casefold()
            if path_key in seen_paths:
                errors.append(ArtifactPlanDiagnostic(
                    "duplicate_resolved_artifact_path",
                    "Bindings resolve multiple artifacts to the same path.",
                    f"artifacts[{index}]",
                ))
            seen_paths[path_key] = index

    modular_count = output.get("declared_modular_file_count")
    if modular_count is not None and (
        not isinstance(modular_count, int)
        or isinstance(modular_count, bool)
        or modular_count != len(modular)
    ):
        errors.append(ArtifactPlanDiagnostic(
            "declared_modular_count_mismatch",
            "Declared modular file count does not match the exact modular path list.",
            "output_contract.declared_modular_file_count",
        ))
    total_count = output.get("declared_file_count")
    accounted = (
        len(deliverables) + len(modular) + len(ancillary) + (1 if final else 0)
    )
    if total_count is not None and (
        not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count != accounted
    ):
        errors.append(ArtifactPlanDiagnostic(
            "declared_artifact_count_mismatch",
            "Declared file count does not match deliverable + modular + ancillary + final artifacts.",
            "output_contract.declared_file_count",
        ))

    policy = output.get("artifact_set_policy")
    if isinstance(policy, Mapping) and str(policy.get("mode") or "").casefold() == "exact":
        policy_items = _string_list(policy.get("artifacts"))
        if policy_items is None:
            errors.append(ArtifactPlanDiagnostic(
                "artifact_policy_not_list",
                "Exact artifact-set policy must contain a string artifact list.",
                "output_contract.artifact_set_policy.artifacts",
            ))
        elif policy_items:
            # Format documents sometimes give the final slot a generic literal
            # alias while the orchestrator supplies the canonical templated
            # target.  Reconcile that alias only when the compiler identified
            # exactly one format-final slot; never pattern-match or guess.
            format_finals = _string_list(
                output.get("declared_format_final_artifacts")
            )
            if format_finals is None:
                errors.append(ArtifactPlanDiagnostic(
                    "invalid_format_final_artifacts",
                    "Format final artifacts must be an exact string list.",
                    "output_contract.declared_format_final_artifacts",
                ))
                format_finals = []
            alias = format_finals[0] if len(format_finals) == 1 and final else None
            rendered_policy: list[ArtifactPathPlan] = []
            for index, item in enumerate(policy_items):
                if alias is not None and item.casefold() == alias.casefold():
                    rendered_policy.append(final)
                    continue
                rendered_policy.append(_render_artifact_path(
                    "policy",
                    item,
                    f"output_contract.artifact_set_policy.artifacts[{index}]",
                    bindings,
                    errors,
                ))
            declared_keys = {
                (item.path or item.template).casefold() for item in all_artifacts
            }
            policy_keys = {
                (item.path or item.template).casefold() for item in rendered_policy
            }
            if len(policy_keys) != len(rendered_policy):
                errors.append(ArtifactPlanDiagnostic(
                    "duplicate_artifact_policy_match",
                    "Exact artifact-set policy entries must be unique.",
                    "output_contract.artifact_set_policy.artifacts",
                ))
            if policy_keys != declared_keys:
                errors.append(ArtifactPlanDiagnostic(
                    "artifact_policy_set_mismatch",
                    "Exact artifact-set policy must equal the compiled artifact set.",
                    "output_contract.artifact_set_policy.artifacts",
                ))


def _plan_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "artifact-plan-v1:" + hashlib.sha256(encoded).hexdigest()[:24]


def compile_artifact_plan(
    execution_contract: Mapping[str, Any],
    output_contract: Mapping[str, Any] | None = None,
    bindings: Mapping[str, str] | None = None,
    *,
    selection_context: Mapping[str, Any] | None = None,
) -> ArtifactPlan:
    """Compile exact Skill declarations into one immutable artifact plan.

    No values are inferred from user prose. Placeholder-bearing paths become
    concrete only when the caller supplies an explicit case-insensitive
    binding (for example ``{"project": "alpha"}`` for ``{PROJECT}``). Section
    activation is likewise projected only from an explicit
    ``selection_context.selected_workers`` list; without it every declared
    section remains present with activation status ``unknown``.
    """
    errors: list[ArtifactPlanDiagnostic] = []
    warnings: list[ArtifactPlanDiagnostic] = []
    if not isinstance(execution_contract, Mapping):
        errors.append(ArtifactPlanDiagnostic(
            "execution_contract_not_mapping",
            "Skill execution contract must be an object.",
            "execution_contract",
        ))
        execution_contract = {}
    execution, output, quality = _normalize_contracts(
        execution_contract,
        output_contract,
    )
    if not output:
        errors.append(ArtifactPlanDiagnostic(
            "output_contract_missing",
            "A deterministic artifact plan requires a structured output contract.",
            "output_contract",
        ))
    raw_merge_declarations = output.get("merge_declarations")
    if raw_merge_declarations is not None and (
        not isinstance(raw_merge_declarations, list)
        or any(
            not isinstance(item, Mapping)
            for item in raw_merge_declarations
        )
    ):
        errors.append(ArtifactPlanDiagnostic(
            "merge_declarations_not_list",
            "Merge declarations must be a list of objects.",
            "output_contract.merge_declarations",
        ))
    diagnostics = execution.get("diagnostics")
    if isinstance(diagnostics, Mapping) and (
        diagnostics.get("valid") is False or diagnostics.get("errors")
    ):
        errors.append(ArtifactPlanDiagnostic(
            "execution_contract_invalid",
            "The Skill execution contract contains compiler errors.",
            "execution_contract.diagnostics",
        ))

    normalized_bindings = _normalize_bindings(bindings, errors)
    selected_workers = _normalize_selected_workers(
        selection_context,
        _declared_worker_ids(execution),
        errors,
    )
    deliverables = _path_list(
        "deliverable",
        output.get("declared_artifacts"),
        "output_contract.declared_artifacts",
        normalized_bindings,
        errors,
    )
    modular = _path_list(
        "modular",
        output.get("declared_modular_files"),
        "output_contract.declared_modular_files",
        normalized_bindings,
        errors,
    )
    ancillary = _path_list(
        "ancillary",
        output.get("declared_ancillary_files"),
        "output_contract.declared_ancillary_files",
        normalized_bindings,
        errors,
    )
    final_template = _collect_final_template(output, errors)
    final = (
        _render_artifact_path(
            "final",
            final_template,
            "output_contract.declared_final_artifact",
            normalized_bindings,
            errors,
        )
        if final_template is not None else None
    )

    _validate_counts_and_uniqueness(
        output,
        deliverables,
        modular,
        ancillary,
        final,
        normalized_bindings,
        errors,
    )
    all_artifacts = (
        tuple(deliverables)
        + tuple(modular)
        + tuple(ancillary)
        + ((final,) if final else ())
    )
    sections = _compile_sections(
        execution,
        output,
        quality,
        all_artifacts,
        normalized_bindings,
        selected_workers,
        errors,
    )
    markers = _required_markers(output, quality, errors)
    merge_inputs = _compile_merge_order(
        output,
        modular,
        normalized_bindings,
        errors,
    )
    separator = _merge_separator(output, errors)
    # A final artifact can be a semantic synthesis, rendered document, archive,
    # or other independently produced deliverable.  Its mere declaration does
    # not authorize byte-concatenating modular artifacts; only the Skill's
    # explicit merge contract does.  Legacy packages that predate the boolean
    # flag may opt in with an explicit merge command; a final path alone never
    # implies byte concatenation.
    merge_required = _merge_is_required(output)
    if merge_required and not modular:
        errors.append(ArtifactPlanDiagnostic(
            "merge_inputs_missing",
            "A required merge needs at least one modular artifact.",
            "output_contract.declared_modular_files",
        ))
    if merge_required and final is None:
        errors.append(ArtifactPlanDiagnostic(
            "merge_target_missing",
            "A mandatory merge needs one declared final artifact.",
            "output_contract.declared_final_artifact",
        ))

    placeholder_names = sorted({
        placeholder
        for artifact in all_artifacts
        for placeholder in artifact.unresolved_placeholders
    }, key=str.casefold)
    used_binding_names = {
        str(match.group(1) or match.group(2)).casefold()
        for artifact in all_artifacts
        for match in _NAMED_PLACEHOLDER_RE.finditer(artifact.template)
    }
    for folded, (raw_key, _) in normalized_bindings.items():
        if folded not in used_binding_names:
            warnings.append(ArtifactPlanDiagnostic(
                "unused_artifact_binding",
                f"Binding {raw_key!r} is not referenced by any artifact path.",
                f"bindings.{raw_key}",
            ))

    valid = not errors and not placeholder_names
    input_paths = tuple(
        item.path for item in merge_inputs if item.path is not None
    )
    merge = ArtifactMergePlan(
        required=merge_required,
        dispatchable=(
            valid
            and merge_required
            and final is not None
            and final.path is not None
            and len(input_paths) == len(merge_inputs)
            and bool(input_paths)
        ),
        input_templates=tuple(item.template for item in merge_inputs),
        input_paths=input_paths,
        output_template=final.template if final else None,
        output_path=final.path if final else None,
        separator=separator,
    )
    canonical_bindings = tuple(sorted(
        ((raw_key.casefold(), value) for raw_key, value in normalized_bindings.values()),
        key=lambda item: item[0],
    ))
    fingerprint_payload = {
        "bindings": canonical_bindings,
        "deliverables": [item.to_dict() for item in deliverables],
        "modular": [item.to_dict() for item in modular],
        "ancillary": [item.to_dict() for item in ancillary],
        "final": final.to_dict() if final else None,
        "selection_context": (
            {"selected_workers": list(selected_workers)}
            if selected_workers is not None else None
        ),
        "sections": [item.to_dict() for item in sections],
        "required_markers": markers,
        "merge": merge.to_dict(),
        "errors": [item.to_dict() for item in errors],
        "warnings": [item.to_dict() for item in warnings],
    }
    return ArtifactPlan(
        schema_version="chatds.artifact-plan.v1",
        plan_id=_plan_fingerprint(fingerprint_payload),
        valid=valid,
        bindings=canonical_bindings,
        deliverable_artifacts=tuple(deliverables),
        modular_artifacts=tuple(modular),
        ancillary_artifacts=tuple(ancillary),
        final_artifact=final,
        selected_workers=selected_workers,
        sections=sections,
        required_markers=markers,
        merge=merge,
        unresolved_placeholders=tuple(placeholder_names),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


__all__ = [
    "ArtifactMergePlan",
    "ArtifactPathPlan",
    "ArtifactPlan",
    "ArtifactPlanDiagnostic",
    "ArtifactPlanError",
    "ArtifactSectionPlan",
    "compile_artifact_plan",
]
