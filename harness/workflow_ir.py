"""Content-addressed, provider-neutral Workflow IR for instruction-only Skills.

This module deliberately has no dependency on the agent loop.  It provides the
closed compiler boundary needed between model-authored workflow planning and
the existing deterministic worker/wave runtime:

* structurally segment authoritative Markdown into stable instruction units;
* validate a typed, bounded, content-addressed model-authored workflow graph;
* prove bidirectional instruction -> node (and optional output) coverage;
* bind every executable node to an already-issued capability candidate ID;
* lower the validated graph to the existing worker/wave plan shape; and
* evaluate execution receipts without treating one shared tool call as proof
  that multiple declared workflow nodes ran.

The parser is intentionally language-neutral.  It recognizes Markdown
structure rather than English verbs or domain vocabulary.  The model may
classify a unit as required/optional/advisory, but it cannot omit a unit, invent
authority, add unknown schema fields, or submit an unbounded/partial graph.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any

from delegated_result_contract import project_object_result_contract


WORKFLOW_IR_SCHEMA_VERSION = "1"
WORKFLOW_PLAN_SCHEMA_VERSION = "1"

MAX_DOCUMENT_CHARS = 1_000_000
MAX_DOCUMENT_LINES = 50_000
MAX_WORKFLOW_DOCUMENTS = 16
MAX_INSTRUCTION_UNITS = 4_096
MAX_INSTRUCTION_UNIT_CHARS = 32_768
MAX_SOURCE_PATH_CHARS = 1_024
MAX_IR_JSON_BYTES = 1_000_000
MAX_WORKFLOW_PLAN_JSON_BYTES = 128 * 1024
MAX_IR_JSON_DEPTH = 32
MAX_IR_JSON_VALUES = 100_000
MAX_JSON_STRING_CHARS = 131_072
MAX_NODES = 512
MAX_DEPENDENCIES_PER_NODE = 128
MAX_TOTAL_DEPENDENCIES = 8_192
MAX_CAPABILITY_IDS = 2_048
MAX_CAPABILITIES_PER_NODE = 128
MAX_OUTPUTS = 256
MAX_REFERENCES_PER_ITEM = 512
MAX_IDENTIFIER_CHARS = 160
MAX_SHORT_TEXT_CHARS = 2_048
MAX_RESULT_SCHEMA_BYTES = 65_536
MAX_PARALLELISM = 64
MAX_ROUND = 128
MAX_ITERATIONS_PER_NODE = 128
MAX_INSTRUCTION_RANGES_PER_NODE = 512
MAX_TOTAL_INSTRUCTION_RANGES = 4_096
MAX_INSTRUCTION_PREVIEW_CHARS = 64
MAX_NODES_PER_INSTRUCTION = 16

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:@/-]{0,159}$")
_ATX_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)")
_SETEXT_HEADING_RE = re.compile(r"^[ \t]{0,3}(?:=+|-+)[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^[ \t]{0,3}>")
_THEMATIC_RE = re.compile(
    r"^[ \t]{0,3}(?:(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,})$"
)
_LIST_ITEM_RE = re.compile(
    r"^[ \t]{0,3}(?:[-+*]|\d{1,9}[.)、]|[^\W\d_][^\s|]{0,7}、)[ \t]+",
    re.UNICODE,
)
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")

_NODE_KINDS = frozenset(
    {
        "classify",
        "retrieve",
        "delegate",
        "tool",
        "aggregate",
        "verify",
        "synthesize",
        "artifact",
        "approval",
    }
)
_EXECUTORS = frozenset({"harness", "child_agent", "native_tool", "skill_script", "mcp"})
_REQUIREMENTS = frozenset({"required", "conditional", "optional", "advisory"})
_DISPOSITIONS = frozenset({"mapped", "not_applicable", "unsupported"})
_RECEIPT_STATUSES = frozenset(
    {"succeeded", "degraded", "failed", "unsupported", "skipped"}
)

_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "complete",
        "skill",
        "documents",
        "capability_catalog_sha256",
        "nodes",
        "coverage",
        "outputs",
        "policies",
        "counts",
        "ir_sha256",
    }
)
_SKILL_FIELDS = frozenset({"name", "version"})
_DOCUMENT_FIELDS = frozenset(
    {"path", "source_sha256", "instruction_set_sha256", "unit_count"}
)
_NODE_FIELDS = frozenset(
    {
        "id",
        "kind",
        "executor",
        "title",
        "role",
        "phase",
        "round",
        "required",
        "instruction_ids",
        "depends_on",
        "capability_ids",
        "result_id",
        "result_schema",
        "output_ids",
        "join_policy",
    }
)
_COVERAGE_FIELDS = frozenset(
    {
        "instruction_id",
        "requirement",
        "disposition",
        "node_ids",
        "output_ids",
        "reason",
    }
)
_OUTPUT_FIELDS = frozenset({"id", "required", "instruction_ids", "producer_node_ids"})
_POLICY_FIELDS = frozenset(
    {
        "completion_policy",
        "failure_policy",
        "max_parallelism",
        "max_iterations_per_node",
    }
)
_COUNT_FIELDS = frozenset(
    {"documents", "instruction_units", "nodes", "coverage", "outputs"}
)
_RECEIPT_FIELDS = frozenset({"node_id", "status", "result_sha256"})
_WORKFLOW_PLAN_FIELDS = frozenset({"schema_version", "nodes", "outputs"})
_WORKFLOW_PLAN_NODE_FIELDS = frozenset(
    {
        "id",
        "kind",
        "title",
        "role",
        "phase",
        "round",
        "instruction_ranges",
        "depends_on",
        "capability_ids",
        "result_schema",
    }
)
_WORKFLOW_PLAN_RANGE_FIELDS = frozenset(
    {"start_instruction_id", "end_instruction_id"}
)
_WORKFLOW_PLAN_OUTPUT_FIELDS = frozenset({"id", "producer_node_ids"})
_CHILD_AGENT_NODE_KINDS = frozenset(
    {
        "classify",
        "retrieve",
        "delegate",
        "aggregate",
        "verify",
        "synthesize",
        "artifact",
    }
)


class InstructionDocumentError(ValueError):
    """Raised when authoritative Markdown cannot be safely canonicalized."""

    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code}{f' at {path}' if path else ''}: {message}")


class WorkflowIRValidationError(ValueError):
    """Stable, machine-readable failure for an invalid model-authored IR."""

    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code}{f' at {path}' if path else ''}: {message}")


class WorkflowPlanAdapterError(ValueError):
    """Raised when a valid IR cannot be represented by the legacy plan shape."""

    def __init__(self, code: str, message: str, *, node_id: str = "") -> None:
        self.code = code
        self.node_id = node_id
        super().__init__(f"{code}{f' for {node_id}' if node_id else ''}: {message}")


@dataclass(frozen=True)
class InstructionUnit:
    """One exact, structurally-delimited unit from an authoritative document."""

    id: str
    source_path: str
    source_sha256: str
    kind: str
    start_line: int
    end_line: int
    text_sha256: str
    text: str

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text_sha256": self.text_sha256,
        }
        if include_text:
            result["text"] = self.text
        return result


@dataclass(frozen=True)
class InstructionDocument:
    """Canonical, content-addressed instruction catalog for one Markdown file."""

    source_path: str
    source_sha256: str
    canonical_sha256: str
    instruction_set_sha256: str
    char_count: int
    line_count: int
    units: tuple[InstructionUnit, ...]

    def binding_dict(self) -> dict[str, Any]:
        return {
            "path": self.source_path,
            "source_sha256": self.source_sha256,
            "instruction_set_sha256": self.instruction_set_sha256,
            "unit_count": len(self.units),
        }

    def prompt_dict(self) -> dict[str, Any]:
        return {
            **self.binding_dict(),
            "canonical_sha256": self.canonical_sha256,
            "units": [unit.to_dict() for unit in self.units],
        }


@dataclass(frozen=True)
class WorkflowDocumentBinding:
    path: str
    source_sha256: str
    instruction_set_sha256: str
    unit_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "source_sha256": self.source_sha256,
            "instruction_set_sha256": self.instruction_set_sha256,
            "unit_count": self.unit_count,
        }


@dataclass(frozen=True)
class WorkflowNode:
    id: str
    kind: str
    executor: str
    title: str
    role: str
    phase: str
    round: int | None
    required: bool
    instruction_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    capability_ids: tuple[str, ...]
    result_id: str
    result_schema_json: str
    output_ids: tuple[str, ...]
    join_policy: str

    @property
    def result_schema(self) -> dict[str, Any]:
        return json.loads(self.result_schema_json)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "executor": self.executor,
            "title": self.title,
            "role": self.role,
            "phase": self.phase,
            "required": self.required,
            "instruction_ids": list(self.instruction_ids),
            "depends_on": list(self.depends_on),
            "capability_ids": list(self.capability_ids),
            "result_id": self.result_id,
            "result_schema": self.result_schema,
            "output_ids": list(self.output_ids),
            "join_policy": self.join_policy,
        }
        if self.round is not None:
            result["round"] = self.round
        return result


@dataclass(frozen=True)
class WorkflowOutput:
    id: str
    required: bool
    instruction_ids: tuple[str, ...]
    producer_node_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "required": self.required,
            "instruction_ids": list(self.instruction_ids),
            "producer_node_ids": list(self.producer_node_ids),
        }


@dataclass(frozen=True)
class InstructionCoverage:
    instruction_id: str
    requirement: str
    disposition: str
    node_ids: tuple[str, ...]
    output_ids: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruction_id": self.instruction_id,
            "requirement": self.requirement,
            "disposition": self.disposition,
            "node_ids": list(self.node_ids),
            "output_ids": list(self.output_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WorkflowPolicies:
    completion_policy: str
    failure_policy: str
    max_parallelism: int
    max_iterations_per_node: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "completion_policy": self.completion_policy,
            "failure_policy": self.failure_policy,
            "max_parallelism": self.max_parallelism,
            "max_iterations_per_node": self.max_iterations_per_node,
        }


@dataclass(frozen=True)
class WorkflowIR:
    """Validated immutable workflow graph.

    ``instruction_units`` are retained so later independent verifiers can
    produce exact source-span findings without trusting model-copied prose.
    """

    schema_version: str
    skill_name: str
    skill_version: str
    documents: tuple[WorkflowDocumentBinding, ...]
    instruction_units: tuple[InstructionUnit, ...]
    capability_catalog_sha256: str
    nodes: tuple[WorkflowNode, ...]
    coverage: tuple[InstructionCoverage, ...]
    outputs: tuple[WorkflowOutput, ...]
    policies: WorkflowPolicies
    ir_sha256: str

    @property
    def node_map(self) -> dict[str, WorkflowNode]:
        return {node.id: node for node in self.nodes}

    @property
    def coverage_map(self) -> dict[str, InstructionCoverage]:
        return {item.instruction_id: item for item in self.coverage}

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "complete": True,
            "skill": {
                "name": self.skill_name,
                **({"version": self.skill_version} if self.skill_version else {}),
            },
            "documents": [document.to_dict() for document in self.documents],
            "capability_catalog_sha256": self.capability_catalog_sha256,
            "nodes": [node.to_dict() for node in self.nodes],
            "coverage": [item.to_dict() for item in self.coverage],
            "outputs": [output.to_dict() for output in self.outputs],
            "policies": self.policies.to_dict(),
            "counts": {
                "documents": len(self.documents),
                "instruction_units": len(self.instruction_units),
                "nodes": len(self.nodes),
                "coverage": len(self.coverage),
                "outputs": len(self.outputs),
            },
        }
        if include_digest:
            result["ir_sha256"] = self.ir_sha256
        return result


@dataclass(frozen=True)
class InstructionExecutionFinding:
    instruction_id: str
    requirement: str
    status: str
    node_ids: tuple[str, ...]
    missing_node_ids: tuple[str, ...]
    degraded_node_ids: tuple[str, ...]
    failed_node_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruction_id": self.instruction_id,
            "requirement": self.requirement,
            "status": self.status,
            "node_ids": list(self.node_ids),
            "missing_node_ids": list(self.missing_node_ids),
            "degraded_node_ids": list(self.degraded_node_ids),
            "failed_node_ids": list(self.failed_node_ids),
        }


@dataclass(frozen=True)
class InstructionExecutionCoverageReport:
    complete: bool
    workflow_ir_sha256: str
    findings: tuple[InstructionExecutionFinding, ...]
    blocking_instruction_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "workflow_ir_sha256": self.workflow_ir_sha256,
            "findings": [finding.to_dict() for finding in self.findings],
            "blocking_instruction_ids": list(self.blocking_instruction_ids),
        }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _safe_source_path(raw_path: Any) -> str:
    if not isinstance(raw_path, str):
        raise InstructionDocumentError(
            "invalid_source_path", "source_path must be a string"
        )
    path = raw_path.strip()
    if not path or len(path) > MAX_SOURCE_PATH_CHARS or "\\" in path or "\x00" in path:
        raise InstructionDocumentError(
            "invalid_source_path", "source_path is empty, unsafe, or too long"
        )
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or str(pure) != path
    ):
        raise InstructionDocumentError(
            "invalid_source_path",
            "source_path must be a normalized relative POSIX path",
        )
    return path


def _is_table_separator(line: str) -> bool:
    stripped = line.strip().strip("|")
    if "|" not in stripped:
        return False
    cells = [cell.strip() for cell in stripped.split("|")]
    return len(cells) >= 2 and all(
        bool(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell)) for cell in cells
    )


def _is_table_start(lines: Sequence[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and "|" in lines[index]
        and _is_table_separator(lines[index + 1])
    )


def _is_block_start(lines: Sequence[str], index: int) -> bool:
    line = lines[index]
    if not line.strip():
        return True
    if (
        _ATX_HEADING_RE.match(line)
        or _FENCE_RE.match(line)
        or _LIST_ITEM_RE.match(line)
        or _BLOCKQUOTE_RE.match(line)
        or _THEMATIC_RE.match(line)
        or _is_table_start(lines, index)
    ):
        return True
    return (
        index + 1 < len(lines)
        and bool(line.strip())
        and bool(_SETEXT_HEADING_RE.match(lines[index + 1]))
    )


def canonicalize_skill_markdown(
    markdown: str,
    *,
    source_path: str = "SKILL.md",
    max_chars: int = MAX_DOCUMENT_CHARS,
    max_units: int = MAX_INSTRUCTION_UNITS,
) -> InstructionDocument:
    """Create stable instruction units from one complete Markdown document.

    Bounds may be lowered by callers/tests but never raised above the hard
    module limits.  Unterminated frontmatter or code fences are rejected so a
    provider-truncated document cannot silently become planning authority.
    """

    path = _safe_source_path(source_path)
    if not isinstance(markdown, str):
        raise InstructionDocumentError(
            "invalid_document", "Markdown content must be a string", path=path
        )
    if not 1 <= max_chars <= MAX_DOCUMENT_CHARS:
        raise ValueError("max_chars must be within the hard document bound")
    if not 1 <= max_units <= MAX_INSTRUCTION_UNITS:
        raise ValueError("max_units must be within the hard instruction-unit bound")
    try:
        raw_bytes = markdown.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InstructionDocumentError(
            "invalid_unicode", "Markdown contains invalid Unicode", path=path
        ) from exc
    if "\x00" in markdown:
        raise InstructionDocumentError(
            "invalid_document", "Markdown contains a NUL byte", path=path
        )
    if len(markdown) > max_chars:
        raise InstructionDocumentError(
            "document_too_large",
            f"Markdown exceeds the {max_chars} character bound",
            path=path,
        )

    canonical = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lines = canonical.split("\n")
    if len(lines) > MAX_DOCUMENT_LINES:
        raise InstructionDocumentError(
            "document_too_many_lines",
            f"Markdown exceeds the {MAX_DOCUMENT_LINES} line bound",
            path=path,
        )

    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    canonical_sha256 = _sha256_text(canonical)
    units: list[InstructionUnit] = []

    def emit(kind: str, start_index: int, end_exclusive: int) -> None:
        text = "\n".join(lines[start_index:end_exclusive])
        if not text.strip():
            return
        if len(text) > MAX_INSTRUCTION_UNIT_CHARS:
            raise InstructionDocumentError(
                "instruction_unit_too_large",
                (
                    f"{kind} block at lines {start_index + 1}-"
                    f"{end_exclusive} exceeds the unit bound"
                ),
                path=path,
            )
        text_sha256 = _sha256_text(text)
        identity = (
            f"{path}\0{source_sha256}\0{kind}\0{start_index + 1}\0"
            f"{end_exclusive}\0{text_sha256}"
        )
        units.append(
            InstructionUnit(
                id=f"iu-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}",
                source_path=path,
                source_sha256=source_sha256,
                kind=kind,
                start_line=start_index + 1,
                end_line=end_exclusive,
                text_sha256=text_sha256,
                text=text,
            )
        )
        if len(units) > max_units:
            raise InstructionDocumentError(
                "too_many_instruction_units",
                f"Markdown exceeds the {max_units} instruction-unit bound",
                path=path,
            )

    index = 0
    if lines and lines[0].strip() == "---":
        closing = next(
            (
                candidate
                for candidate in range(1, len(lines))
                if lines[candidate].strip() in {"---", "..."}
            ),
            None,
        )
        if closing is None:
            raise InstructionDocumentError(
                "unterminated_frontmatter",
                "opening frontmatter marker has no closing marker",
                path=path,
            )
        index = closing + 1

    while index < len(lines):
        line = lines[index]
        if not line.strip() or _THEMATIC_RE.match(line):
            index += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            marker_char = marker[0]
            closing = None
            for candidate in range(index + 1, len(lines)):
                candidate_match = _FENCE_RE.match(lines[candidate])
                if (
                    candidate_match
                    and candidate_match.group(1)[0] == marker_char
                    and len(candidate_match.group(1)) >= len(marker)
                    and not candidate_match.group(2).strip()
                ):
                    closing = candidate
                    break
            if closing is None:
                raise InstructionDocumentError(
                    "unterminated_code_fence",
                    f"code fence opened at line {index + 1} is incomplete",
                    path=path,
                )
            emit("code_block", index, closing + 1)
            index = closing + 1
            continue

        if _ATX_HEADING_RE.match(line):
            emit("heading", index, index + 1)
            index += 1
            continue

        if (
            index + 1 < len(lines)
            and line.strip()
            and _SETEXT_HEADING_RE.match(lines[index + 1])
        ):
            emit("heading", index, index + 2)
            index += 2
            continue

        if _is_table_start(lines, index):
            end = index + 2
            while end < len(lines) and lines[end].strip() and "|" in lines[end]:
                end += 1
            emit("table", index, end)
            index = end
            continue

        if _LIST_ITEM_RE.match(line):
            end = index + 1
            while end < len(lines):
                continuation = lines[end]
                if not continuation.strip():
                    break
                if _is_block_start(lines, end):
                    break
                if continuation.startswith(("  ", "\t")):
                    end += 1
                    continue
                break
            emit("list_item", index, end)
            index = end
            continue

        if _BLOCKQUOTE_RE.match(line):
            end = index + 1
            while end < len(lines) and _BLOCKQUOTE_RE.match(lines[end]):
                end += 1
            emit("blockquote", index, end)
            index = end
            continue

        end = index + 1
        while end < len(lines) and lines[end].strip():
            if _is_block_start(lines, end):
                break
            end += 1
        emit("paragraph", index, end)
        index = end

    unit_projection = [
        {
            "id": unit.id,
            "kind": unit.kind,
            "start_line": unit.start_line,
            "end_line": unit.end_line,
            "text_sha256": unit.text_sha256,
        }
        for unit in units
    ]
    instruction_set_sha256 = _sha256_text(
        _canonical_json(
            {
                "source_path": path,
                "source_sha256": source_sha256,
                "canonical_sha256": canonical_sha256,
                "units": unit_projection,
            }
        )
    )
    return InstructionDocument(
        source_path=path,
        source_sha256=source_sha256,
        canonical_sha256=canonical_sha256,
        instruction_set_sha256=instruction_set_sha256,
        char_count=len(markdown),
        line_count=len(lines),
        units=tuple(units),
    )


def instruction_catalog_payload(
    documents: Sequence[InstructionDocument],
) -> dict[str, Any]:
    """Return the bounded, exact model-visible instruction catalog."""

    normalized, units = _validate_authoritative_documents(documents)
    digest = _sha256_text(
        _canonical_json([document.binding_dict() for document in normalized])
    )
    return {
        "schema_version": WORKFLOW_IR_SCHEMA_VERSION,
        "catalog_sha256": digest,
        "documents": [document.prompt_dict() for document in normalized],
        "counts": {
            "documents": len(normalized),
            "instruction_units": len(units),
        },
        "policy": {
            "every_instruction_unit_requires_one_coverage_record": True,
            "unknown_ids_rejected": True,
            "authority_is_content_addressed": True,
        },
    }


def workflow_plan_instruction_catalog_payload(
    documents: Sequence[InstructionDocument],
) -> dict[str, Any]:
    """Return the compact semantic-planning projection of exact authority.

    The full instruction text and every content/digest binding stay runtime
    owned.  The model already received the authoritative documents through
    ``skill_view``; it needs only stable unit coordinates plus a short preview
    to group adjacent instructions into semantic workflow nodes.  The exact
    catalog digest binds this projection to the immutable runtime documents.
    """

    normalized, units = _validate_authoritative_documents(documents)
    digest = _sha256_text(
        _canonical_json([document.binding_dict() for document in normalized])
    )

    def preview(text: str) -> str:
        collapsed = " ".join(str(text or "").split())
        return collapsed[:MAX_INSTRUCTION_PREVIEW_CHARS]

    return {
        "schema_version": WORKFLOW_PLAN_SCHEMA_VERSION,
        "catalog_sha256": digest,
        "documents": [
            {
                "path": document.source_path,
                "unit_count": len(document.units),
                "units": [
                    {
                        "id": unit.id,
                        "kind": unit.kind,
                        "start_line": unit.start_line,
                        "end_line": unit.end_line,
                        "preview": preview(unit.text),
                    }
                    for unit in document.units
                ],
            }
            for document in normalized
        ],
        "counts": {
            "documents": len(normalized),
            "instruction_units": len(units),
        },
        "policy": {
            "emit_compact_workflow_plan_only": True,
            "coalesce_adjacent_units_into_ranges": True,
            "runtime_expands_and_validates_complete_ir": True,
            "unknown_or_stale_ids_rejected": True,
        },
    }


def _validate_authoritative_documents(
    documents: Sequence[InstructionDocument],
) -> tuple[tuple[InstructionDocument, ...], tuple[InstructionUnit, ...]]:
    if isinstance(documents, (str, bytes)) or not isinstance(documents, Sequence):
        raise WorkflowIRValidationError(
            "invalid_documents", "documents must be a sequence"
        )
    if not documents:
        raise WorkflowIRValidationError(
            "missing_documents", "at least one authoritative document is required"
        )
    if len(documents) > MAX_WORKFLOW_DOCUMENTS:
        raise WorkflowIRValidationError(
            "too_many_documents",
            (
                "workflow instruction authority exceeds the "
                f"{MAX_WORKFLOW_DOCUMENTS} document bound"
            ),
        )
    paths: set[str] = set()
    unit_ids: set[str] = set()
    units: list[InstructionUnit] = []
    normalized: list[InstructionDocument] = []
    for index, document in enumerate(documents):
        if not isinstance(document, InstructionDocument):
            raise WorkflowIRValidationError(
                "invalid_document",
                "every document must be an InstructionDocument",
                path=f"documents[{index}]",
            )
        if document.source_path in paths:
            raise WorkflowIRValidationError(
                "duplicate_document_path",
                f"duplicate authoritative document {document.source_path!r}",
                path=f"documents[{index}].path",
            )
        paths.add(document.source_path)
        normalized.append(document)
        for unit in document.units:
            if unit.id in unit_ids:
                raise WorkflowIRValidationError(
                    "duplicate_instruction_id",
                    f"duplicate generated instruction ID {unit.id!r}",
                )
            unit_ids.add(unit.id)
            units.append(unit)
            if len(units) > MAX_INSTRUCTION_UNITS:
                raise WorkflowIRValidationError(
                    "too_many_instruction_units",
                    (
                        "combined authoritative documents exceed the "
                        f"{MAX_INSTRUCTION_UNITS} unit bound"
                    ),
                )
    return tuple(normalized), tuple(units)


def _raise_ir(code: str, message: str, path: str = "") -> None:
    raise WorkflowIRValidationError(code, message, path=path)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _raise_ir("invalid_type", "expected an object", path)
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _raise_ir("invalid_type", "expected an array", path)
    return value


def _known_fields(value: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        _raise_ir(
            "unknown_field",
            f"unknown fields are not allowed: {', '.join(unknown[:8])}",
            path,
        )


def _required(value: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in value:
        _raise_ir("missing_field", f"required field {key!r} is missing", path)
    return value[key]


def _text(
    value: Any,
    path: str,
    *,
    required: bool = True,
    max_chars: int = MAX_SHORT_TEXT_CHARS,
) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        _raise_ir("invalid_type", "expected a string", path)
    result = value.strip()
    if required and not result:
        _raise_ir("empty_string", "value must not be empty", path)
    if len(result) > max_chars:
        _raise_ir("text_too_long", f"value exceeds {max_chars} characters", path)
    if any(ord(char) < 32 and char not in "\t\n\r" for char in result):
        _raise_ir(
            "invalid_control_character", "value contains control characters", path
        )
    return result


def _identifier(value: Any, path: str) -> str:
    result = _text(value, path, max_chars=MAX_IDENTIFIER_CHARS)
    if not _IDENTIFIER_RE.fullmatch(result):
        _raise_ir(
            "invalid_identifier",
            "identifier must use the bounded portable identifier alphabet",
            path,
        )
    return result


def _sha256(value: Any, path: str) -> str:
    result = _text(value, path, max_chars=64)
    if not _SHA256_RE.fullmatch(result):
        _raise_ir("invalid_sha256", "expected a lowercase SHA-256 digest", path)
    return result


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _raise_ir("invalid_type", "expected a boolean", path)
    return value


def _bounded_int(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _raise_ir("invalid_type", "expected an integer", path)
    if not minimum <= value <= maximum:
        _raise_ir(
            "integer_out_of_bounds",
            f"value must be between {minimum} and {maximum}",
            path,
        )
    return value


def _id_list(
    value: Any,
    path: str,
    *,
    max_items: int = MAX_REFERENCES_PER_ITEM,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    raw = _list(value, path)
    if len(raw) > max_items:
        _raise_ir(
            "too_many_references",
            f"array exceeds the {max_items} item bound",
            path,
        )
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        item_id = _identifier(item, f"{path}[{index}]")
        if item_id in seen:
            _raise_ir(
                "duplicate_reference",
                f"duplicate ID {item_id!r}",
                f"{path}[{index}]",
            )
        seen.add(item_id)
        result.append(item_id)
    if not allow_empty and not result:
        _raise_ir("empty_array", "at least one ID is required", path)
    return tuple(result)


def _assert_finite_bounded_json(value: Any) -> None:
    stack: list[tuple[Any, int, str]] = [(value, 0, "$")]
    seen_containers: set[int] = set()
    count = 0
    while stack:
        current, depth, path = stack.pop()
        count += 1
        if count > MAX_IR_JSON_VALUES:
            _raise_ir(
                "ir_value_limit_exceeded",
                f"IR exceeds the {MAX_IR_JSON_VALUES} JSON value bound",
                path,
            )
        if depth > MAX_IR_JSON_DEPTH:
            _raise_ir(
                "ir_depth_limit_exceeded",
                f"IR exceeds the {MAX_IR_JSON_DEPTH} level depth bound",
                path,
            )
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                _raise_ir("non_finite_number", "NaN and infinity are forbidden", path)
            continue
        if isinstance(current, str):
            if len(current) > MAX_JSON_STRING_CHARS:
                _raise_ir(
                    "json_string_too_long",
                    f"string exceeds {MAX_JSON_STRING_CHARS} characters",
                    path,
                )
            continue
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in seen_containers:
                _raise_ir(
                    "recursive_or_aliased_json",
                    "recursive/shared container references are forbidden",
                    path,
                )
            seen_containers.add(identity)
            for key, child in current.items():
                if not isinstance(key, str):
                    _raise_ir("invalid_json_key", "object keys must be strings", path)
                stack.append((child, depth + 1, f"{path}.{key}"))
            continue
        if isinstance(current, list):
            identity = id(current)
            if identity in seen_containers:
                _raise_ir(
                    "recursive_or_aliased_json",
                    "recursive/shared container references are forbidden",
                    path,
                )
            seen_containers.add(identity)
            for index, child in enumerate(current):
                stack.append((child, depth + 1, f"{path}[{index}]"))
            continue
        _raise_ir(
            "non_json_value",
            f"value of type {type(current).__name__} is not JSON",
            path,
        )


def _parse_result_schema(value: Any, path: str) -> str:
    schema = _mapping(value, path)
    encoded = _canonical_json(schema)
    if len(encoded.encode("utf-8")) > MAX_RESULT_SCHEMA_BYTES:
        _raise_ir(
            "result_schema_too_large",
            f"result schema exceeds {MAX_RESULT_SCHEMA_BYTES} bytes",
            path,
        )
    # Delegate execution uses the native registry's bounded JSON-Schema
    # dialect.  Validate that exact dialect before a workflow can be accepted
    # or installed, rather than discovering an invalid schema only after its
    # child has entered the execution queue.
    from tools.registry import json_schema_shape_error

    schema_error = json_schema_shape_error(
        schema,
        schema_path=path,
        reject_unsupported_keywords=True,
    )
    if schema_error is not None:
        _raise_ir(
            "invalid_result_schema",
            "result schema is not executable by the bounded runtime: "
            + schema_error,
            path,
        )
    try:
        project_object_result_contract(schema, schema_path=path)
    except ValueError as exc:
        _raise_ir(
            "result_contract_transport_incompatible",
            str(exc),
            path,
        )
    return encoded


def _cycle_path(nodes: Sequence[WorkflowNode]) -> tuple[str, ...]:
    graph = {node.id: node.depends_on for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def visit(node_id: str) -> tuple[str, ...] | None:
        if node_id in visiting:
            start = path.index(node_id)
            return tuple(path[start:] + [node_id])
        if node_id in visited:
            return None
        visiting.add(node_id)
        path.append(node_id)
        for dependency in graph.get(node_id, ()):
            result = visit(dependency)
            if result:
                return result
        path.pop()
        visiting.remove(node_id)
        visited.add(node_id)
        return None

    for node in nodes:
        result = visit(node.id)
        if result:
            return result
    return ()


def _topological_nodes(nodes: Sequence[WorkflowNode]) -> tuple[WorkflowNode, ...]:
    node_map = {node.id: node for node in nodes}
    order = {node.id: index for index, node in enumerate(nodes)}
    indegree = {node.id: len(node.depends_on) for node in nodes}
    dependents: dict[str, list[str]] = {node.id: [] for node in nodes}
    for node in nodes:
        for dependency in node.depends_on:
            dependents[dependency].append(node.id)
    ready = deque(node.id for node in nodes if indegree[node.id] == 0)
    result: list[WorkflowNode] = []
    while ready:
        node_id = ready.popleft()
        result.append(node_map[node_id])
        newly_ready: list[str] = []
        for dependent in dependents[node_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                newly_ready.append(dependent)
        for dependent in sorted(newly_ready, key=order.__getitem__):
            ready.append(dependent)
    if len(result) != len(nodes):
        cycle = _cycle_path(nodes)
        _raise_ir(
            "workflow_cycle",
            "workflow dependencies contain a cycle"
            + (f": {' -> '.join(cycle)}" if cycle else ""),
            "nodes",
        )
    return tuple(result)


def _executor_kind_is_valid(kind: str, executor: str) -> bool:
    allowed = {
        "classify": {"harness", "child_agent", "native_tool"},
        "retrieve": {"child_agent", "native_tool", "skill_script", "mcp"},
        "delegate": {"child_agent"},
        "tool": {"native_tool", "skill_script", "mcp"},
        "aggregate": {"harness", "child_agent"},
        "verify": {
            "harness",
            "child_agent",
            "native_tool",
            "skill_script",
            "mcp",
        },
        "synthesize": {"harness", "child_agent"},
        "artifact": {"harness", "child_agent", "native_tool", "skill_script"},
        "approval": {"harness"},
    }
    return executor in allowed[kind]


def validate_workflow_ir(
    payload: Mapping[str, Any],
    *,
    documents: Sequence[InstructionDocument],
    skill_name: str,
    capability_catalog_sha256: str,
    available_capability_ids: Iterable[str],
    strict_instruction_execution: bool = False,
) -> WorkflowIR:
    """Validate and freeze one complete model-authored Workflow IR.

    Authority is supplied by the caller, never by ``payload``.  The payload
    must bind exactly to the current documents and capability catalog.
    """

    if not isinstance(strict_instruction_execution, bool):
        _raise_ir(
            "invalid_runtime_policy",
            "strict_instruction_execution must be a runtime-owned boolean",
        )
    _assert_finite_bounded_json(payload)
    try:
        encoded_payload = _canonical_json(payload).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkflowIRValidationError(
            "invalid_json", "payload is not finite JSON"
        ) from exc
    if len(encoded_payload) > MAX_IR_JSON_BYTES:
        _raise_ir(
            "ir_too_large",
            f"IR exceeds the {MAX_IR_JSON_BYTES} byte bound",
        )

    authoritative_documents, instruction_units = _validate_authoritative_documents(
        documents
    )
    root = _mapping(payload, "$")
    _known_fields(root, _ROOT_FIELDS, "$")
    if _text(_required(root, "schema_version", "$"), "$.schema_version") != (
        WORKFLOW_IR_SCHEMA_VERSION
    ):
        _raise_ir(
            "unsupported_schema_version",
            f"schema_version must be {WORKFLOW_IR_SCHEMA_VERSION!r}",
            "$.schema_version",
        )
    if _required(root, "complete", "$") is not True:
        _raise_ir(
            "incomplete_ir",
            "complete must be exactly true; partial plans are never installed",
            "$.complete",
        )

    expected_skill_name = _text(
        skill_name, "skill_name", max_chars=MAX_IDENTIFIER_CHARS
    )
    skill = _mapping(_required(root, "skill", "$"), "$.skill")
    _known_fields(skill, _SKILL_FIELDS, "$.skill")
    submitted_skill_name = _text(
        _required(skill, "name", "$.skill"),
        "$.skill.name",
        max_chars=MAX_IDENTIFIER_CHARS,
    )
    if submitted_skill_name != expected_skill_name:
        _raise_ir(
            "skill_identity_mismatch",
            "submitted skill name does not match backend-selected Skill",
            "$.skill.name",
        )
    skill_version = _text(
        skill.get("version"),
        "$.skill.version",
        required=False,
        max_chars=128,
    )

    expected_catalog_digest = _sha256(
        capability_catalog_sha256, "capability_catalog_sha256"
    )
    submitted_catalog_digest = _sha256(
        _required(root, "capability_catalog_sha256", "$"),
        "$.capability_catalog_sha256",
    )
    if submitted_catalog_digest != expected_catalog_digest:
        _raise_ir(
            "capability_catalog_mismatch",
            "IR was planned against a different capability catalog",
            "$.capability_catalog_sha256",
        )

    if isinstance(available_capability_ids, (str, bytes)) or not isinstance(
        available_capability_ids, Iterable
    ):
        _raise_ir(
            "invalid_capability_catalog",
            "available_capability_ids must be an iterable of exact IDs",
        )
    capability_ids: set[str] = set()
    for index, raw_id in enumerate(available_capability_ids):
        capability_id = _identifier(raw_id, f"available_capability_ids[{index}]")
        if capability_id in capability_ids:
            _raise_ir(
                "duplicate_capability_id",
                f"backend capability catalog contains duplicate ID {capability_id!r}",
                f"available_capability_ids[{index}]",
            )
        capability_ids.add(capability_id)
        if len(capability_ids) > MAX_CAPABILITY_IDS:
            _raise_ir(
                "too_many_capabilities",
                f"capability catalog exceeds the {MAX_CAPABILITY_IDS} ID bound",
            )

    submitted_documents = _list(_required(root, "documents", "$"), "$.documents")
    if len(submitted_documents) != len(authoritative_documents):
        _raise_ir(
            "document_binding_mismatch",
            "IR must bind every and only the authoritative documents",
            "$.documents",
        )
    submitted_by_path: dict[str, WorkflowDocumentBinding] = {}
    authoritative_by_path = {
        document.source_path: document for document in authoritative_documents
    }
    for index, raw_document in enumerate(submitted_documents):
        path_prefix = f"$.documents[{index}]"
        item = _mapping(raw_document, path_prefix)
        _known_fields(item, _DOCUMENT_FIELDS, path_prefix)
        try:
            source_path = _safe_source_path(_required(item, "path", path_prefix))
        except InstructionDocumentError as exc:
            _raise_ir(exc.code, str(exc), f"{path_prefix}.path")
        if source_path in submitted_by_path:
            _raise_ir(
                "duplicate_document_path",
                f"duplicate document binding {source_path!r}",
                f"{path_prefix}.path",
            )
        source_sha256 = _sha256(
            _required(item, "source_sha256", path_prefix),
            f"{path_prefix}.source_sha256",
        )
        instruction_set_sha256 = _sha256(
            _required(item, "instruction_set_sha256", path_prefix),
            f"{path_prefix}.instruction_set_sha256",
        )
        unit_count = _bounded_int(
            _required(item, "unit_count", path_prefix),
            f"{path_prefix}.unit_count",
            0,
            MAX_INSTRUCTION_UNITS,
        )
        authoritative = authoritative_by_path.get(source_path)
        if (
            authoritative is None
            or source_sha256 != authoritative.source_sha256
            or instruction_set_sha256 != authoritative.instruction_set_sha256
            or unit_count != len(authoritative.units)
        ):
            _raise_ir(
                "document_binding_mismatch",
                "document digest or unit count does not match current authority",
                path_prefix,
            )
        submitted_by_path[source_path] = WorkflowDocumentBinding(
            path=source_path,
            source_sha256=source_sha256,
            instruction_set_sha256=instruction_set_sha256,
            unit_count=unit_count,
        )
    document_bindings = tuple(
        submitted_by_path[document.source_path] for document in authoritative_documents
    )

    raw_nodes = _list(_required(root, "nodes", "$"), "$.nodes")
    if not raw_nodes:
        _raise_ir("missing_nodes", "at least one workflow node is required", "$.nodes")
    if len(raw_nodes) > MAX_NODES:
        _raise_ir(
            "too_many_nodes",
            f"workflow exceeds the {MAX_NODES} node bound",
            "$.nodes",
        )
    nodes: list[WorkflowNode] = []
    node_ids: set[str] = set()
    result_ids: set[str] = set()
    total_dependencies = 0
    for index, raw_node in enumerate(raw_nodes):
        prefix = f"$.nodes[{index}]"
        item = _mapping(raw_node, prefix)
        _known_fields(item, _NODE_FIELDS, prefix)
        node_id = _identifier(_required(item, "id", prefix), f"{prefix}.id")
        if node_id in node_ids:
            _raise_ir(
                "duplicate_node_id",
                f"duplicate workflow node {node_id!r}",
                f"{prefix}.id",
            )
        node_ids.add(node_id)
        kind = _text(_required(item, "kind", prefix), f"{prefix}.kind")
        if kind not in _NODE_KINDS:
            _raise_ir(
                "unsupported_node_kind",
                f"kind must be one of {sorted(_NODE_KINDS)}",
                f"{prefix}.kind",
            )
        executor = _text(_required(item, "executor", prefix), f"{prefix}.executor")
        if executor not in _EXECUTORS:
            _raise_ir(
                "unsupported_executor",
                f"executor must be one of {sorted(_EXECUTORS)}",
                f"{prefix}.executor",
            )
        if not _executor_kind_is_valid(kind, executor):
            _raise_ir(
                "executor_kind_mismatch",
                f"executor {executor!r} cannot execute node kind {kind!r}",
                prefix,
            )
        title = _text(
            item.get("title"),
            f"{prefix}.title",
            required=False,
            max_chars=MAX_SHORT_TEXT_CHARS,
        )
        role = _text(
            item.get("role"),
            f"{prefix}.role",
            required=False,
            max_chars=MAX_SHORT_TEXT_CHARS,
        )
        phase = _text(
            item.get("phase"),
            f"{prefix}.phase",
            required=False,
            max_chars=MAX_IDENTIFIER_CHARS,
        )
        round_value = (
            _bounded_int(item["round"], f"{prefix}.round", 1, MAX_ROUND)
            if "round" in item
            else None
        )
        required_node = _boolean(
            _required(item, "required", prefix), f"{prefix}.required"
        )
        instruction_ids = _id_list(
            _required(item, "instruction_ids", prefix),
            f"{prefix}.instruction_ids",
            allow_empty=False,
        )
        dependencies = _id_list(
            _required(item, "depends_on", prefix),
            f"{prefix}.depends_on",
            max_items=MAX_DEPENDENCIES_PER_NODE,
        )
        if node_id in dependencies:
            _raise_ir(
                "self_dependency",
                "a workflow node cannot depend on itself",
                f"{prefix}.depends_on",
            )
        total_dependencies += len(dependencies)
        if total_dependencies > MAX_TOTAL_DEPENDENCIES:
            _raise_ir(
                "too_many_dependencies",
                ("workflow exceeds the " f"{MAX_TOTAL_DEPENDENCIES} dependency bound"),
                "$.nodes",
            )
        node_capabilities = _id_list(
            _required(item, "capability_ids", prefix),
            f"{prefix}.capability_ids",
            max_items=MAX_CAPABILITIES_PER_NODE,
        )
        unknown_capabilities = sorted(set(node_capabilities) - capability_ids)
        if unknown_capabilities:
            _raise_ir(
                "unknown_capability_id",
                (
                    "node references capability IDs not issued by the backend: "
                    + ", ".join(unknown_capabilities[:8])
                ),
                f"{prefix}.capability_ids",
            )
        if executor != "harness" and not node_capabilities:
            _raise_ir(
                "missing_capability_binding",
                "non-harness nodes require at least one exact capability ID",
                f"{prefix}.capability_ids",
            )
        if executor == "harness" and node_capabilities:
            _raise_ir(
                "unexpected_capability_binding",
                "harness-owned nodes cannot claim model-selected capabilities",
                f"{prefix}.capability_ids",
            )
        result_id = _identifier(
            _required(item, "result_id", prefix), f"{prefix}.result_id"
        )
        if result_id in result_ids:
            _raise_ir(
                "duplicate_result_id",
                f"duplicate workflow result ID {result_id!r}",
                f"{prefix}.result_id",
            )
        result_ids.add(result_id)
        result_schema_json = _parse_result_schema(
            _required(item, "result_schema", prefix),
            f"{prefix}.result_schema",
        )
        output_ids = _id_list(
            _required(item, "output_ids", prefix), f"{prefix}.output_ids"
        )
        join_policy = _text(
            _required(item, "join_policy", prefix), f"{prefix}.join_policy"
        )
        if join_policy != "all":
            _raise_ir(
                "unsupported_join_policy",
                "only fail-closed 'all' joins are currently supported",
                f"{prefix}.join_policy",
            )
        nodes.append(
            WorkflowNode(
                id=node_id,
                kind=kind,
                executor=executor,
                title=title,
                role=role,
                phase=phase,
                round=round_value,
                required=required_node,
                instruction_ids=instruction_ids,
                depends_on=dependencies,
                capability_ids=node_capabilities,
                result_id=result_id,
                result_schema_json=result_schema_json,
                output_ids=output_ids,
                join_policy=join_policy,
            )
        )

    node_map = {node.id: node for node in nodes}
    instruction_id_set = {unit.id for unit in instruction_units}
    for node in nodes:
        unknown_dependencies = sorted(set(node.depends_on) - node_ids)
        if unknown_dependencies:
            _raise_ir(
                "unknown_dependency",
                (
                    f"node {node.id!r} depends on undeclared nodes: "
                    + ", ".join(unknown_dependencies[:8])
                ),
                f"nodes.{node.id}.depends_on",
            )
        unknown_instructions = sorted(set(node.instruction_ids) - instruction_id_set)
        if unknown_instructions:
            _raise_ir(
                "unknown_instruction_id",
                (
                    f"node {node.id!r} references unknown instruction IDs: "
                    + ", ".join(unknown_instructions[:8])
                ),
                f"nodes.{node.id}.instruction_ids",
            )
        if node.required:
            optional_dependencies = [
                dependency
                for dependency in node.depends_on
                if not node_map[dependency].required
            ]
            if optional_dependencies:
                _raise_ir(
                    "required_node_depends_on_optional",
                    (
                        f"required node {node.id!r} depends on optional nodes: "
                        + ", ".join(optional_dependencies)
                    ),
                    f"nodes.{node.id}.depends_on",
                )
    _topological_nodes(nodes)

    raw_outputs = _list(_required(root, "outputs", "$"), "$.outputs")
    if len(raw_outputs) > MAX_OUTPUTS:
        _raise_ir(
            "too_many_outputs",
            f"workflow exceeds the {MAX_OUTPUTS} output bound",
            "$.outputs",
        )
    outputs: list[WorkflowOutput] = []
    output_ids: set[str] = set()
    for index, raw_output in enumerate(raw_outputs):
        prefix = f"$.outputs[{index}]"
        item = _mapping(raw_output, prefix)
        _known_fields(item, _OUTPUT_FIELDS, prefix)
        output_id = _identifier(_required(item, "id", prefix), f"{prefix}.id")
        if output_id in output_ids:
            _raise_ir(
                "duplicate_output_id",
                f"duplicate workflow output {output_id!r}",
                f"{prefix}.id",
            )
        output_ids.add(output_id)
        output_required = _boolean(
            _required(item, "required", prefix), f"{prefix}.required"
        )
        output_instruction_ids = _id_list(
            _required(item, "instruction_ids", prefix),
            f"{prefix}.instruction_ids",
            allow_empty=False,
        )
        producer_node_ids = _id_list(
            _required(item, "producer_node_ids", prefix),
            f"{prefix}.producer_node_ids",
            allow_empty=False,
        )
        unknown_instruction_ids = sorted(
            set(output_instruction_ids) - instruction_id_set
        )
        if unknown_instruction_ids:
            _raise_ir(
                "unknown_instruction_id",
                "output references unknown instruction IDs",
                f"{prefix}.instruction_ids",
            )
        unknown_producers = sorted(set(producer_node_ids) - node_ids)
        if unknown_producers:
            _raise_ir(
                "unknown_output_producer",
                "output references undeclared producer nodes",
                f"{prefix}.producer_node_ids",
            )
        if output_required and any(
            not node_map[node_id].required for node_id in producer_node_ids
        ):
            _raise_ir(
                "required_output_has_optional_producer",
                "every producer of a required output must be a required node",
                f"{prefix}.producer_node_ids",
            )
        outputs.append(
            WorkflowOutput(
                id=output_id,
                required=output_required,
                instruction_ids=output_instruction_ids,
                producer_node_ids=producer_node_ids,
            )
        )
    output_map = {output.id: output for output in outputs}
    for node in nodes:
        unknown_node_outputs = sorted(set(node.output_ids) - output_ids)
        if unknown_node_outputs:
            _raise_ir(
                "unknown_output_id",
                (
                    f"node {node.id!r} references unknown outputs: "
                    + ", ".join(unknown_node_outputs[:8])
                ),
                f"nodes.{node.id}.output_ids",
            )
        for output_id in node.output_ids:
            if node.id not in output_map[output_id].producer_node_ids:
                _raise_ir(
                    "output_producer_mismatch",
                    "node/output producer mapping must be bidirectional",
                    f"nodes.{node.id}.output_ids",
                )
    for output in outputs:
        for producer_id in output.producer_node_ids:
            if output.id not in node_map[producer_id].output_ids:
                _raise_ir(
                    "output_producer_mismatch",
                    "output/node producer mapping must be bidirectional",
                    f"outputs.{output.id}.producer_node_ids",
                )

    raw_coverage = _list(_required(root, "coverage", "$"), "$.coverage")
    if len(raw_coverage) != len(instruction_units):
        _raise_ir(
            "instruction_coverage_omission",
            (
                "coverage must contain exactly one record for every "
                "authoritative instruction unit"
            ),
            "$.coverage",
        )
    coverage: list[InstructionCoverage] = []
    covered_instruction_ids: set[str] = set()
    for index, raw_coverage_item in enumerate(raw_coverage):
        prefix = f"$.coverage[{index}]"
        item = _mapping(raw_coverage_item, prefix)
        _known_fields(item, _COVERAGE_FIELDS, prefix)
        instruction_id = _identifier(
            _required(item, "instruction_id", prefix),
            f"{prefix}.instruction_id",
        )
        if instruction_id in covered_instruction_ids:
            _raise_ir(
                "duplicate_instruction_coverage",
                f"duplicate coverage record for {instruction_id!r}",
                f"{prefix}.instruction_id",
            )
        if instruction_id not in instruction_id_set:
            _raise_ir(
                "unknown_instruction_id",
                f"coverage references unknown instruction {instruction_id!r}",
                f"{prefix}.instruction_id",
            )
        covered_instruction_ids.add(instruction_id)
        requirement = _text(
            _required(item, "requirement", prefix), f"{prefix}.requirement"
        )
        if requirement not in _REQUIREMENTS:
            _raise_ir(
                "invalid_requirement",
                f"requirement must be one of {sorted(_REQUIREMENTS)}",
                f"{prefix}.requirement",
            )
        disposition = _text(
            _required(item, "disposition", prefix), f"{prefix}.disposition"
        )
        if disposition not in _DISPOSITIONS:
            _raise_ir(
                "invalid_disposition",
                f"disposition must be one of {sorted(_DISPOSITIONS)}",
                f"{prefix}.disposition",
            )
        coverage_node_ids = _id_list(
            _required(item, "node_ids", prefix), f"{prefix}.node_ids"
        )
        coverage_output_ids = _id_list(
            _required(item, "output_ids", prefix), f"{prefix}.output_ids"
        )
        reason = _text(
            _required(item, "reason", prefix),
            f"{prefix}.reason",
            required=False,
            max_chars=MAX_SHORT_TEXT_CHARS,
        )
        if disposition == "mapped":
            if not coverage_node_ids:
                _raise_ir(
                    "mapped_instruction_without_node",
                    "mapped instructions require at least one node",
                    f"{prefix}.node_ids",
                )
        elif coverage_node_ids or coverage_output_ids:
            _raise_ir(
                "unmapped_instruction_has_references",
                "non-mapped instructions cannot reference nodes or outputs",
                prefix,
            )
        if disposition != "mapped" and not reason:
            _raise_ir(
                "missing_disposition_reason",
                "non-mapped instructions require an explicit reason",
                f"{prefix}.reason",
            )
        if requirement == "required" and disposition != "mapped":
            _raise_ir(
                "required_instruction_unmapped",
                "required instructions must map to executable nodes",
                prefix,
            )
        unknown_coverage_nodes = sorted(set(coverage_node_ids) - node_ids)
        if unknown_coverage_nodes:
            _raise_ir(
                "unknown_node_id",
                "coverage references undeclared workflow nodes",
                f"{prefix}.node_ids",
            )
        unknown_coverage_outputs = sorted(set(coverage_output_ids) - output_ids)
        if unknown_coverage_outputs:
            _raise_ir(
                "unknown_output_id",
                "coverage references undeclared outputs",
                f"{prefix}.output_ids",
            )
        if requirement in {"required", "conditional"} and disposition == "mapped":
            optional_nodes = [
                node_id
                for node_id in coverage_node_ids
                if not node_map[node_id].required
            ]
            if optional_nodes:
                _raise_ir(
                    "required_instruction_maps_optional_node",
                    (
                        "required/active conditional instructions cannot rely "
                        "on optional nodes"
                    ),
                    f"{prefix}.node_ids",
                )
        coverage.append(
            InstructionCoverage(
                instruction_id=instruction_id,
                requirement=requirement,
                disposition=disposition,
                node_ids=coverage_node_ids,
                output_ids=coverage_output_ids,
                reason=reason,
            )
        )
    missing_coverage = sorted(instruction_id_set - covered_instruction_ids)
    if missing_coverage:
        _raise_ir(
            "instruction_coverage_omission",
            "coverage omits authoritative instruction IDs",
            "$.coverage",
        )
    coverage_map = {item.instruction_id: item for item in coverage}

    for node in nodes:
        for instruction_id in node.instruction_ids:
            item = coverage_map[instruction_id]
            if item.disposition != "mapped" or node.id not in item.node_ids:
                _raise_ir(
                    "instruction_node_coverage_mismatch",
                    "node/instruction mapping must be bidirectional",
                    f"nodes.{node.id}.instruction_ids",
                )
    for item in coverage:
        for node_id in item.node_ids:
            if item.instruction_id not in node_map[node_id].instruction_ids:
                _raise_ir(
                    "instruction_node_coverage_mismatch",
                    "instruction/node mapping must be bidirectional",
                    f"coverage.{item.instruction_id}.node_ids",
                )
        for output_id in item.output_ids:
            if item.instruction_id not in output_map[output_id].instruction_ids:
                _raise_ir(
                    "instruction_output_coverage_mismatch",
                    "instruction/output mapping must be bidirectional",
                    f"coverage.{item.instruction_id}.output_ids",
                )
    for output in outputs:
        for instruction_id in output.instruction_ids:
            item = coverage_map[instruction_id]
            if item.disposition != "mapped" or output.id not in item.output_ids:
                _raise_ir(
                    "instruction_output_coverage_mismatch",
                    "output/instruction mapping must be bidirectional",
                    f"outputs.{output.id}.instruction_ids",
                )

    if strict_instruction_execution:
        # A mandatory prose-to-graph compilation must not let the same model
        # that authored the graph dismiss executable source material as
        # "advisory" or "not applicable".  Headings are structural; every
        # other exact Markdown unit is conservatively runtime-required and
        # must map to at least one required executable node.  Multiple units
        # may legitimately describe one node, but none may disappear.
        executable_instruction_ids = {
            unit.id for unit in instruction_units if unit.kind != "heading"
        }
        if not executable_instruction_ids:
            _raise_ir(
                "no_executable_instruction_units",
                "mandatory Workflow IR authority contains only headings",
                "$.coverage",
            )
        for instruction_id in sorted(executable_instruction_ids):
            item = coverage_map[instruction_id]
            if (
                item.requirement not in {"required", "conditional"}
                or item.disposition != "mapped"
                or not item.node_ids
            ):
                _raise_ir(
                    "runtime_required_instruction_unmapped",
                    (
                        "every non-heading instruction unit in a mandatory "
                        "Workflow IR must map to required execution"
                    ),
                    f"coverage.{instruction_id}",
                )
        if not any(node.required for node in nodes):
            _raise_ir(
                "empty_required_workflow",
                "mandatory Workflow IR requires at least one required node",
                "$.nodes",
            )

    policy_payload = _mapping(_required(root, "policies", "$"), "$.policies")
    _known_fields(policy_payload, _POLICY_FIELDS, "$.policies")
    completion_policy = _text(
        _required(policy_payload, "completion_policy", "$.policies"),
        "$.policies.completion_policy",
    )
    if completion_policy != "all_required":
        _raise_ir(
            "unsupported_completion_policy",
            "only all_required completion is supported",
            "$.policies.completion_policy",
        )
    failure_policy = _text(
        _required(policy_payload, "failure_policy", "$.policies"),
        "$.policies.failure_policy",
    )
    if failure_policy != "fail_closed":
        _raise_ir(
            "unsupported_failure_policy",
            "only fail_closed failure handling is supported",
            "$.policies.failure_policy",
        )
    policies = WorkflowPolicies(
        completion_policy=completion_policy,
        failure_policy=failure_policy,
        max_parallelism=_bounded_int(
            _required(policy_payload, "max_parallelism", "$.policies"),
            "$.policies.max_parallelism",
            1,
            MAX_PARALLELISM,
        ),
        max_iterations_per_node=_bounded_int(
            _required(policy_payload, "max_iterations_per_node", "$.policies"),
            "$.policies.max_iterations_per_node",
            1,
            MAX_ITERATIONS_PER_NODE,
        ),
    )

    counts = _mapping(_required(root, "counts", "$"), "$.counts")
    _known_fields(counts, _COUNT_FIELDS, "$.counts")
    expected_counts = {
        "documents": len(authoritative_documents),
        "instruction_units": len(instruction_units),
        "nodes": len(nodes),
        "coverage": len(coverage),
        "outputs": len(outputs),
    }
    for name, expected in expected_counts.items():
        submitted = _bounded_int(
            _required(counts, name, "$.counts"),
            f"$.counts.{name}",
            0,
            max(
                MAX_INSTRUCTION_UNITS,
                MAX_NODES,
                MAX_OUTPUTS,
            ),
        )
        if submitted != expected:
            _raise_ir(
                "count_mismatch",
                f"declared {name} count {submitted} does not equal {expected}",
                f"$.counts.{name}",
            )

    # Preserve declaration order in the immutable IR.  The validated
    # topological order is recomputed by adapters so the model cannot smuggle
    # execution order through array position.
    provisional = WorkflowIR(
        schema_version=WORKFLOW_IR_SCHEMA_VERSION,
        skill_name=submitted_skill_name,
        skill_version=skill_version,
        documents=document_bindings,
        instruction_units=instruction_units,
        capability_catalog_sha256=submitted_catalog_digest,
        nodes=tuple(nodes),
        coverage=tuple(coverage),
        outputs=tuple(outputs),
        policies=policies,
        ir_sha256="",
    )
    ir_sha256 = _sha256_text(_canonical_json(provisional.to_dict(include_digest=False)))
    if "ir_sha256" in root:
        submitted_ir_sha256 = _sha256(root["ir_sha256"], "$.ir_sha256")
        if submitted_ir_sha256 != ir_sha256:
            _raise_ir(
                "workflow_ir_digest_mismatch",
                "submitted IR digest does not match the validated canonical graph",
                "$.ir_sha256",
            )
    return replace(provisional, ir_sha256=ir_sha256)


def _default_workflow_plan_result_schema() -> dict[str, Any]:
    """Return a domain-neutral typed child-result envelope."""

    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "evidence": {"type": "array", "items": {}},
            "gaps": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "evidence", "gaps"],
        "additionalProperties": False,
    }


def compile_workflow_plan(
    payload: Mapping[str, Any],
    *,
    documents: Sequence[InstructionDocument],
    skill_name: str,
    capability_catalog_sha256: str,
    available_capability_ids: Iterable[str],
    mandatory_node_capability_ids: Iterable[str] = (),
    skill_version: str = "",
    strict_instruction_execution: bool = True,
) -> WorkflowIR:
    """Compile a compact semantic plan into the complete authoritative IR.

    The model groups exact instruction coordinates and declares only semantic
    dependencies/capability needs.  Runtime-owned bindings, coverage, node
    execution policy, result identities, output mappings, counts, and digest
    are derived here and then revalidated through :func:`validate_workflow_ir`.
    No model-authored plan can grant a tool or weaken mandatory coverage.
    """

    if not isinstance(strict_instruction_execution, bool):
        _raise_ir(
            "invalid_runtime_policy",
            "strict_instruction_execution must be a runtime-owned boolean",
        )
    _assert_finite_bounded_json(payload)
    try:
        encoded = _canonical_json(payload).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkflowIRValidationError(
            "invalid_workflow_plan_json",
            "workflow plan is not finite JSON",
        ) from exc
    if len(encoded) > MAX_WORKFLOW_PLAN_JSON_BYTES:
        _raise_ir(
            "workflow_plan_too_large",
            (
                "semantic workflow plan exceeds the "
                f"{MAX_WORKFLOW_PLAN_JSON_BYTES} byte bound"
            ),
        )

    authoritative_documents, instruction_units = _validate_authoritative_documents(
        documents
    )
    root = _mapping(payload, "$")
    _known_fields(root, _WORKFLOW_PLAN_FIELDS, "$")
    if _text(
        _required(root, "schema_version", "$"),
        "$.schema_version",
    ) != WORKFLOW_PLAN_SCHEMA_VERSION:
        _raise_ir(
            "unsupported_workflow_plan_schema_version",
            f"schema_version must be {WORKFLOW_PLAN_SCHEMA_VERSION!r}",
            "$.schema_version",
        )

    if isinstance(available_capability_ids, (str, bytes)) or not isinstance(
        available_capability_ids, Iterable
    ):
        _raise_ir(
            "invalid_capability_catalog",
            "available_capability_ids must be an iterable of exact IDs",
        )
    available_ids_list: list[Any] = []
    for raw_capability_id in available_capability_ids:
        available_ids_list.append(raw_capability_id)
        if len(available_ids_list) > MAX_CAPABILITY_IDS:
            _raise_ir(
                "too_many_capability_ids",
                (
                    "available capability selection exceeds the "
                    f"{MAX_CAPABILITY_IDS} ID bound"
                ),
                "available_capability_ids",
            )
    available_ids = tuple(available_ids_list)
    if isinstance(mandatory_node_capability_ids, (str, bytes)) or not isinstance(
        mandatory_node_capability_ids, Iterable
    ):
        _raise_ir(
            "invalid_capability_catalog",
            "mandatory_node_capability_ids must be an iterable of exact IDs",
        )
    raw_mandatory_values: list[Any] = []
    for raw_capability_id in mandatory_node_capability_ids:
        raw_mandatory_values.append(raw_capability_id)
        if len(raw_mandatory_values) > MAX_CAPABILITIES_PER_NODE:
            _raise_ir(
                "too_many_references",
                (
                    "mandatory node capability selection exceeds the "
                    f"{MAX_CAPABILITIES_PER_NODE} ID bound"
                ),
                "mandatory_node_capability_ids",
            )
    raw_mandatory_ids = _id_list(
        raw_mandatory_values,
        "mandatory_node_capability_ids",
        max_items=MAX_CAPABILITIES_PER_NODE,
    )
    mandatory_ids = tuple(sorted(raw_mandatory_ids))
    available_id_set: set[str] = set()
    for index, raw_id in enumerate(available_ids):
        capability_id = _identifier(
            raw_id, f"available_capability_ids[{index}]"
        )
        if capability_id in available_id_set:
            _raise_ir(
                "duplicate_capability_id",
                f"duplicate available capability ID {capability_id!r}",
                f"available_capability_ids[{index}]",
            )
        available_id_set.add(capability_id)
    unknown_mandatory = sorted(set(mandatory_ids) - available_id_set)
    if unknown_mandatory:
        _raise_ir(
            "unknown_mandatory_node_capability_id",
            "runtime mandatory node capabilities are outside the selection",
            "mandatory_node_capability_ids",
        )

    location_by_id: dict[str, tuple[int, int]] = {}
    unit_by_id: dict[str, InstructionUnit] = {}
    for document_index, document in enumerate(authoritative_documents):
        for unit_index, unit in enumerate(document.units):
            location_by_id[unit.id] = (document_index, unit_index)
            unit_by_id[unit.id] = unit

    raw_nodes = _list(_required(root, "nodes", "$"), "$.nodes")
    if not raw_nodes:
        _raise_ir(
            "workflow_plan_missing_nodes",
            "at least one semantic workflow node is required",
            "$.nodes",
        )
    if len(raw_nodes) > MAX_NODES:
        _raise_ir(
            "too_many_nodes",
            f"workflow plan exceeds the {MAX_NODES} node bound",
            "$.nodes",
        )

    plan_nodes: list[dict[str, Any]] = []
    seen_node_ids: set[str] = set()
    total_ranges = 0
    for node_index, raw_node in enumerate(raw_nodes):
        prefix = f"$.nodes[{node_index}]"
        item = _mapping(raw_node, prefix)
        _known_fields(item, _WORKFLOW_PLAN_NODE_FIELDS, prefix)
        node_id = _identifier(_required(item, "id", prefix), f"{prefix}.id")
        if node_id in seen_node_ids:
            _raise_ir(
                "duplicate_node_id",
                f"duplicate workflow node {node_id!r}",
                f"{prefix}.id",
            )
        seen_node_ids.add(node_id)
        kind = _text(_required(item, "kind", prefix), f"{prefix}.kind")
        if kind not in _CHILD_AGENT_NODE_KINDS:
            _raise_ir(
                "unsupported_workflow_plan_node_kind",
                (
                    "compact workflow nodes must be executable by the "
                    f"child-agent runtime: {sorted(_CHILD_AGENT_NODE_KINDS)}"
                ),
                f"{prefix}.kind",
            )

        raw_ranges = _list(
            _required(item, "instruction_ranges", prefix),
            f"{prefix}.instruction_ranges",
        )
        if not raw_ranges:
            _raise_ir(
                "workflow_plan_node_missing_instruction_range",
                "every workflow node requires at least one instruction range",
                f"{prefix}.instruction_ranges",
            )
        if len(raw_ranges) > MAX_INSTRUCTION_RANGES_PER_NODE:
            _raise_ir(
                "too_many_instruction_ranges",
                (
                    "node exceeds the "
                    f"{MAX_INSTRUCTION_RANGES_PER_NODE} range bound"
                ),
                f"{prefix}.instruction_ranges",
            )
        total_ranges += len(raw_ranges)
        if total_ranges > MAX_TOTAL_INSTRUCTION_RANGES:
            _raise_ir(
                "too_many_instruction_ranges",
                (
                    "workflow exceeds the "
                    f"{MAX_TOTAL_INSTRUCTION_RANGES} total range bound"
                ),
                "$.nodes",
            )

        expanded_ids: list[str] = []
        expanded_seen: set[str] = set()
        for range_index, raw_range in enumerate(raw_ranges):
            range_prefix = f"{prefix}.instruction_ranges[{range_index}]"
            range_item = _mapping(raw_range, range_prefix)
            _known_fields(
                range_item, _WORKFLOW_PLAN_RANGE_FIELDS, range_prefix
            )
            start_id = _identifier(
                _required(range_item, "start_instruction_id", range_prefix),
                f"{range_prefix}.start_instruction_id",
            )
            end_id = _identifier(
                _required(range_item, "end_instruction_id", range_prefix),
                f"{range_prefix}.end_instruction_id",
            )
            if start_id not in location_by_id:
                _raise_ir(
                    "unknown_workflow_plan_instruction_id",
                    "range start is unknown or stale",
                    f"{range_prefix}.start_instruction_id",
                )
            if end_id not in location_by_id:
                _raise_ir(
                    "unknown_workflow_plan_instruction_id",
                    "range end is unknown or stale",
                    f"{range_prefix}.end_instruction_id",
                )
            start_document, start_index = location_by_id[start_id]
            end_document, end_index = location_by_id[end_id]
            if start_document != end_document:
                _raise_ir(
                    "cross_document_instruction_range",
                    "one instruction range cannot cross document boundaries",
                    range_prefix,
                )
            if start_index > end_index:
                _raise_ir(
                    "reversed_instruction_range",
                    "instruction range start follows its end",
                    range_prefix,
                )
            for unit in authoritative_documents[start_document].units[
                start_index : end_index + 1
            ]:
                if unit.id in expanded_seen:
                    _raise_ir(
                        "overlapping_instruction_range",
                        "ranges within one node must not overlap",
                        range_prefix,
                    )
                expanded_seen.add(unit.id)
                expanded_ids.append(unit.id)
        if not any(unit_by_id[item].kind != "heading" for item in expanded_ids):
            _raise_ir(
                "workflow_plan_node_has_no_executable_instruction",
                "a required child node cannot contain headings only",
                f"{prefix}.instruction_ranges",
            )
        # Range order is a model presentation choice, not workflow semantics.
        # Canonicalize against the frozen document coordinates so equivalent
        # compact plans compile to one content-addressed runtime IR.
        expanded_ids.sort(key=location_by_id.__getitem__)

        dependencies = _id_list(
            _required(item, "depends_on", prefix),
            f"{prefix}.depends_on",
            max_items=MAX_DEPENDENCIES_PER_NODE,
        )
        selected_capabilities = _id_list(
            _required(item, "capability_ids", prefix),
            f"{prefix}.capability_ids",
            max_items=MAX_CAPABILITIES_PER_NODE,
        )
        node_capabilities = tuple(
            sorted(set(selected_capabilities) | set(mandatory_ids))
        )
        unknown_capabilities = sorted(set(node_capabilities) - available_id_set)
        if unknown_capabilities:
            _raise_ir(
                "unknown_capability_id",
                "workflow node references an unselected capability ID",
                f"{prefix}.capability_ids",
            )
        result_schema = item.get("result_schema")
        if result_schema is None:
            result_schema = _default_workflow_plan_result_schema()
        _parse_result_schema(result_schema, f"{prefix}.result_schema")
        plan_nodes.append(
            {
                "id": node_id,
                "kind": kind,
                "title": _text(
                    item.get("title"),
                    f"{prefix}.title",
                    required=False,
                    max_chars=MAX_SHORT_TEXT_CHARS,
                ),
                "role": _text(
                    item.get("role"),
                    f"{prefix}.role",
                    required=False,
                    max_chars=MAX_SHORT_TEXT_CHARS,
                ),
                "phase": _text(
                    item.get("phase"),
                    f"{prefix}.phase",
                    required=False,
                    max_chars=MAX_IDENTIFIER_CHARS,
                ),
                "round": (
                    _bounded_int(
                        item["round"], f"{prefix}.round", 1, MAX_ROUND
                    )
                    if "round" in item
                    else None
                ),
                "instruction_ids": tuple(expanded_ids),
                "depends_on": tuple(sorted(dependencies)),
                "capability_ids": node_capabilities,
                "result_schema": result_schema,
            }
        )

    raw_outputs = _list(root.get("outputs", []), "$.outputs")
    if len(raw_outputs) > MAX_OUTPUTS:
        _raise_ir(
            "too_many_outputs",
            f"workflow plan exceeds the {MAX_OUTPUTS} output bound",
            "$.outputs",
        )
    plan_outputs: list[dict[str, Any]] = []
    seen_output_ids: set[str] = set()
    for output_index, raw_output in enumerate(raw_outputs):
        prefix = f"$.outputs[{output_index}]"
        item = _mapping(raw_output, prefix)
        _known_fields(item, _WORKFLOW_PLAN_OUTPUT_FIELDS, prefix)
        output_id = _identifier(_required(item, "id", prefix), f"{prefix}.id")
        if output_id in seen_output_ids:
            _raise_ir(
                "duplicate_output_id",
                f"duplicate workflow output {output_id!r}",
                f"{prefix}.id",
            )
        seen_output_ids.add(output_id)
        producers = _id_list(
            _required(item, "producer_node_ids", prefix),
            f"{prefix}.producer_node_ids",
            allow_empty=False,
        )
        unknown_producers = sorted(set(producers) - seen_node_ids)
        if unknown_producers:
            _raise_ir(
                "unknown_output_producer",
                "workflow output references undeclared producer nodes",
                f"{prefix}.producer_node_ids",
            )
        plan_outputs.append(
            {
                "id": output_id,
                "producer_node_ids": tuple(sorted(producers)),
            }
        )

    plan_nodes.sort(key=lambda item: item["id"])
    plan_outputs.sort(key=lambda item: item["id"])
    output_ids_by_node: dict[str, list[str]] = {
        item["id"]: [] for item in plan_nodes
    }
    node_instruction_ids = {
        item["id"]: tuple(item["instruction_ids"]) for item in plan_nodes
    }
    complete_outputs: list[dict[str, Any]] = []
    for output in plan_outputs:
        output_instruction_ids = {
            instruction_id
            for producer_id in output["producer_node_ids"]
            for instruction_id in node_instruction_ids[producer_id]
        }
        ordered_output_instruction_ids = [
            unit.id
            for unit in instruction_units
            if unit.id in output_instruction_ids
        ]
        for producer_id in output["producer_node_ids"]:
            output_ids_by_node[producer_id].append(output["id"])
        complete_outputs.append(
            {
                "id": output["id"],
                "required": True,
                "instruction_ids": ordered_output_instruction_ids,
                "producer_node_ids": list(output["producer_node_ids"]),
            }
        )

    node_ids_by_instruction: dict[str, list[str]] = {
        unit.id: [] for unit in instruction_units
    }
    for node in plan_nodes:
        for instruction_id in node["instruction_ids"]:
            node_ids_by_instruction[instruction_id].append(node["id"])
    excessive_fanout = next(
        (
            instruction_id
            for instruction_id, mapped_node_ids
            in node_ids_by_instruction.items()
            if len(mapped_node_ids) > MAX_NODES_PER_INSTRUCTION
        ),
        None,
    )
    if excessive_fanout is not None:
        _raise_ir(
            "instruction_fanout_too_large",
            (
                "one instruction may be assigned to at most "
                f"{MAX_NODES_PER_INSTRUCTION} workflow nodes"
            ),
            f"coverage.{excessive_fanout}",
        )
    missing_executable = [
        unit.id
        for unit in instruction_units
        if unit.kind != "heading" and not node_ids_by_instruction[unit.id]
    ]
    if strict_instruction_execution and missing_executable:
        _raise_ir(
            "runtime_required_instruction_unmapped",
            "compact workflow plan leaves executable instruction units unmapped",
            f"coverage.{missing_executable[0]}",
        )

    output_ids_by_instruction: dict[str, list[str]] = {
        unit.id: [] for unit in instruction_units
    }
    for output in complete_outputs:
        for instruction_id in output["instruction_ids"]:
            output_ids_by_instruction[instruction_id].append(output["id"])

    complete_nodes: list[dict[str, Any]] = []
    for node in plan_nodes:
        natural_result_id = f"result-{node['id']}"
        result_id = (
            natural_result_id
            if len(natural_result_id) <= MAX_IDENTIFIER_CHARS
            else "result-" + _sha256_text(node["id"])[:32]
        )
        complete_node = {
            "id": node["id"],
            "kind": node["kind"],
            "executor": "child_agent",
            "title": node["title"] or node["id"],
            "role": node["role"] or node["title"] or node["id"],
            "phase": node["phase"] or "workflow",
            "required": True,
            "instruction_ids": list(node["instruction_ids"]),
            "depends_on": list(node["depends_on"]),
            "capability_ids": list(node["capability_ids"]),
            "result_id": result_id,
            "result_schema": node["result_schema"],
            "output_ids": sorted(output_ids_by_node[node["id"]]),
            "join_policy": "all",
        }
        if node["round"] is not None:
            complete_node["round"] = node["round"]
        complete_nodes.append(complete_node)

    coverage: list[dict[str, Any]] = []
    for unit in instruction_units:
        mapped_nodes = sorted(node_ids_by_instruction[unit.id])
        if mapped_nodes:
            coverage.append(
                {
                    "instruction_id": unit.id,
                    "requirement": (
                        "advisory" if unit.kind == "heading" else "required"
                    ),
                    "disposition": "mapped",
                    "node_ids": mapped_nodes,
                    "output_ids": sorted(output_ids_by_instruction[unit.id]),
                    "reason": "",
                }
            )
        else:
            coverage.append(
                {
                    "instruction_id": unit.id,
                    "requirement": "advisory",
                    "disposition": "not_applicable",
                    "node_ids": [],
                    "output_ids": [],
                    "reason": "Structural heading not assigned to an execution node.",
                }
            )

    full_payload: dict[str, Any] = {
        "schema_version": WORKFLOW_IR_SCHEMA_VERSION,
        "complete": True,
        "skill": {
            "name": _text(
                skill_name, "skill_name", max_chars=MAX_IDENTIFIER_CHARS
            ),
            **(
                {"version": _text(skill_version, "skill_version", max_chars=128)}
                if str(skill_version or "").strip()
                else {}
            ),
        },
        "documents": [
            document.binding_dict() for document in authoritative_documents
        ],
        "capability_catalog_sha256": _sha256(
            capability_catalog_sha256, "capability_catalog_sha256"
        ),
        "nodes": complete_nodes,
        "coverage": coverage,
        "outputs": complete_outputs,
        "policies": {
            "completion_policy": "all_required",
            "failure_policy": "fail_closed",
            "max_parallelism": min(
                MAX_PARALLELISM, max(1, min(8, len(complete_nodes)))
            ),
            "max_iterations_per_node": 32,
        },
        "counts": {
            "documents": len(authoritative_documents),
            "instruction_units": len(instruction_units),
            "nodes": len(complete_nodes),
            "coverage": len(coverage),
            "outputs": len(complete_outputs),
        },
    }
    return validate_workflow_ir(
        full_payload,
        documents=authoritative_documents,
        skill_name=skill_name,
        capability_catalog_sha256=capability_catalog_sha256,
        available_capability_ids=available_ids,
        strict_instruction_execution=strict_instruction_execution,
    )


def workflow_plan_json_schema() -> dict[str, Any]:
    """Return the bounded model-facing compact semantic-plan schema."""

    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_IDENTIFIER_CHARS,
        "pattern": _IDENTIFIER_RE.pattern,
    }

    def id_array(max_items: int, *, min_items: int = 0) -> dict[str, Any]:
        return {
            "type": "array",
            "items": dict(identifier),
            "minItems": min_items,
            "maxItems": max_items,
        }

    def strict_object(
        properties: Mapping[str, Any], required: Sequence[str]
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": dict(properties),
            "required": list(required),
            "additionalProperties": False,
        }

    range_schema = strict_object(
        {
            "start_instruction_id": dict(identifier),
            "end_instruction_id": dict(identifier),
        },
        ("start_instruction_id", "end_instruction_id"),
    )
    node_schema = strict_object(
        {
            "id": dict(identifier),
            "kind": {
                "type": "string",
                "enum": sorted(_CHILD_AGENT_NODE_KINDS),
            },
            "title": {"type": "string", "maxLength": MAX_SHORT_TEXT_CHARS},
            "role": {"type": "string", "maxLength": MAX_SHORT_TEXT_CHARS},
            "phase": {"type": "string", "maxLength": MAX_IDENTIFIER_CHARS},
            "round": {"type": "integer", "minimum": 1, "maximum": MAX_ROUND},
            "instruction_ranges": {
                "type": "array",
                "items": range_schema,
                "minItems": 1,
                "maxItems": MAX_INSTRUCTION_RANGES_PER_NODE,
                "description": (
                    "Inclusive same-document ranges. Coalesce adjacent units; "
                    "the runtime expands exact IDs and proves full coverage."
                ),
            },
            "depends_on": id_array(MAX_DEPENDENCIES_PER_NODE),
            "capability_ids": id_array(MAX_CAPABILITIES_PER_NODE),
            "result_schema": {
                "type": "object",
                "maxProperties": MAX_REFERENCES_PER_ITEM,
                "description": (
                    "Optional typed child result schema. Omit to use the "
                    "runtime's generic summary/evidence/gaps envelope."
                ),
            },
        },
        ("id", "kind", "instruction_ranges", "depends_on", "capability_ids"),
    )
    output_schema = strict_object(
        {
            "id": dict(identifier),
            "producer_node_ids": id_array(
                MAX_REFERENCES_PER_ITEM, min_items=1
            ),
        },
        ("id", "producer_node_ids"),
    )
    return strict_object(
        {
            "schema_version": {
                "type": "string",
                "const": WORKFLOW_PLAN_SCHEMA_VERSION,
            },
            "nodes": {
                "type": "array",
                "items": node_schema,
                "minItems": 1,
                "maxItems": MAX_NODES,
            },
            "outputs": {
                "type": "array",
                "items": output_schema,
                "maxItems": MAX_OUTPUTS,
            },
        },
        ("schema_version", "nodes"),
    )


def workflow_ir_json_schema() -> dict[str, Any]:
    """Return a fresh strict tool-input schema matching Workflow IR v1.

    JSON Schema cannot express the backend's content-digest, graph, coverage,
    depth, or canonical-byte checks.  Those remain mandatory in
    :func:`validate_workflow_ir`; this shape merely rejects malformed nested
    tool arguments before they reach that authority boundary.
    """

    identifier = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_IDENTIFIER_CHARS,
        "pattern": _IDENTIFIER_RE.pattern,
    }
    digest = {
        "type": "string",
        "pattern": _SHA256_RE.pattern,
    }

    def id_array(max_items: int, *, min_items: int = 0) -> dict[str, Any]:
        return {
            "type": "array",
            "items": dict(identifier),
            "minItems": min_items,
            "maxItems": max_items,
        }

    def strict_object(
        properties: Mapping[str, Any],
        required: Sequence[str],
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": dict(properties),
            "required": list(required),
            "additionalProperties": False,
        }

    document_schema = strict_object(
        {
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_SOURCE_PATH_CHARS,
            },
            "source_sha256": dict(digest),
            "instruction_set_sha256": dict(digest),
            "unit_count": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_INSTRUCTION_UNITS,
            },
        },
        ("path", "source_sha256", "instruction_set_sha256", "unit_count"),
    )
    node_schema = strict_object(
        {
            "id": dict(identifier),
            "kind": {"type": "string", "enum": sorted(_NODE_KINDS)},
            "executor": {"type": "string", "enum": sorted(_EXECUTORS)},
            "title": {
                "type": "string",
                "maxLength": MAX_SHORT_TEXT_CHARS,
            },
            "role": {
                "type": "string",
                "maxLength": MAX_SHORT_TEXT_CHARS,
            },
            "phase": {
                "type": "string",
                "maxLength": MAX_IDENTIFIER_CHARS,
            },
            "round": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_ROUND,
            },
            "required": {"type": "boolean"},
            "instruction_ids": id_array(MAX_REFERENCES_PER_ITEM, min_items=1),
            "depends_on": id_array(MAX_DEPENDENCIES_PER_NODE),
            "capability_ids": id_array(MAX_CAPABILITIES_PER_NODE),
            "result_id": dict(identifier),
            "result_schema": {
                "type": "object",
                "maxProperties": MAX_REFERENCES_PER_ITEM,
                "description": (
                    "Bounded JSON Schema metadata. The backend additionally "
                    "enforces canonical byte, value-count, depth, and finite-"
                    "number limits."
                ),
            },
            "output_ids": id_array(MAX_REFERENCES_PER_ITEM),
            "join_policy": {"type": "string", "const": "all"},
        },
        (
            "id",
            "kind",
            "executor",
            "required",
            "instruction_ids",
            "depends_on",
            "capability_ids",
            "result_id",
            "result_schema",
            "output_ids",
            "join_policy",
        ),
    )
    coverage_schema = strict_object(
        {
            "instruction_id": dict(identifier),
            "requirement": {
                "type": "string",
                "enum": sorted(_REQUIREMENTS),
            },
            "disposition": {
                "type": "string",
                "enum": sorted(_DISPOSITIONS),
            },
            "node_ids": id_array(MAX_REFERENCES_PER_ITEM),
            "output_ids": id_array(MAX_REFERENCES_PER_ITEM),
            "reason": {
                "type": "string",
                "maxLength": MAX_SHORT_TEXT_CHARS,
            },
        },
        (
            "instruction_id",
            "requirement",
            "disposition",
            "node_ids",
            "output_ids",
            "reason",
        ),
    )
    output_schema = strict_object(
        {
            "id": dict(identifier),
            "required": {"type": "boolean"},
            "instruction_ids": id_array(MAX_REFERENCES_PER_ITEM, min_items=1),
            "producer_node_ids": id_array(MAX_REFERENCES_PER_ITEM, min_items=1),
        },
        ("id", "required", "instruction_ids", "producer_node_ids"),
    )
    policies_schema = strict_object(
        {
            "completion_policy": {
                "type": "string",
                "const": "all_required",
            },
            "failure_policy": {
                "type": "string",
                "const": "fail_closed",
            },
            "max_parallelism": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PARALLELISM,
            },
            "max_iterations_per_node": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_ITERATIONS_PER_NODE,
            },
        },
        (
            "completion_policy",
            "failure_policy",
            "max_parallelism",
            "max_iterations_per_node",
        ),
    )
    counts_schema = strict_object(
        {
            "documents": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_WORKFLOW_DOCUMENTS,
            },
            "instruction_units": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_INSTRUCTION_UNITS,
            },
            "nodes": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_NODES,
            },
            "coverage": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_INSTRUCTION_UNITS,
            },
            "outputs": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_OUTPUTS,
            },
        },
        ("documents", "instruction_units", "nodes", "coverage", "outputs"),
    )
    return strict_object(
        {
            "schema_version": {
                "type": "string",
                "const": WORKFLOW_IR_SCHEMA_VERSION,
            },
            "complete": {"type": "boolean", "const": True},
            "skill": strict_object(
                {
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_IDENTIFIER_CHARS,
                    },
                    "version": {
                        "type": "string",
                        "maxLength": 128,
                    },
                },
                ("name",),
            ),
            "documents": {
                "type": "array",
                "items": document_schema,
                "minItems": 1,
                "maxItems": MAX_WORKFLOW_DOCUMENTS,
            },
            "capability_catalog_sha256": dict(digest),
            "nodes": {
                "type": "array",
                "items": node_schema,
                "minItems": 1,
                "maxItems": MAX_NODES,
            },
            "coverage": {
                "type": "array",
                "items": coverage_schema,
                "minItems": 1,
                "maxItems": MAX_INSTRUCTION_UNITS,
            },
            "outputs": {
                "type": "array",
                "items": output_schema,
                "maxItems": MAX_OUTPUTS,
            },
            "policies": policies_schema,
            "counts": counts_schema,
            "ir_sha256": dict(digest),
        },
        (
            "schema_version",
            "complete",
            "skill",
            "documents",
            "capability_catalog_sha256",
            "nodes",
            "coverage",
            "outputs",
            "policies",
            "counts",
        ),
    )


class _DuplicateJSONObjectKey(ValueError):
    pass


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONObjectKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def parse_and_validate_workflow_ir(
    raw_json: str,
    *,
    documents: Sequence[InstructionDocument],
    skill_name: str,
    capability_catalog_sha256: str,
    available_capability_ids: Iterable[str],
) -> WorkflowIR:
    """Strict JSON entrypoint for streamed/model-authored workflow payloads."""

    if not isinstance(raw_json, str):
        _raise_ir("invalid_json", "raw workflow payload must be a JSON string")
    try:
        encoded = raw_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkflowIRValidationError(
            "invalid_unicode", "workflow JSON contains invalid Unicode"
        ) from exc
    if len(encoded) > MAX_IR_JSON_BYTES:
        _raise_ir(
            "ir_too_large",
            f"IR exceeds the {MAX_IR_JSON_BYTES} byte bound",
        )
    try:
        payload = json.loads(
            raw_json,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJSONObjectKey as exc:
        _raise_ir(
            "duplicate_json_key",
            f"duplicate JSON object key {str(exc)!r}",
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        _raise_ir(
            "malformed_or_truncated_json",
            f"workflow payload is not one complete JSON object: {str(exc)[:240]}",
        )
    if not isinstance(payload, Mapping):
        _raise_ir("invalid_type", "workflow JSON root must be an object", "$")
    return validate_workflow_ir(
        payload,
        documents=documents,
        skill_name=skill_name,
        capability_catalog_sha256=capability_catalog_sha256,
        available_capability_ids=available_capability_ids,
    )


def verify_instruction_execution_coverage(
    workflow_ir: WorkflowIR,
    receipts: Sequence[Mapping[str, Any]],
) -> InstructionExecutionCoverageReport:
    """Evaluate exact per-node receipts against instruction coverage.

    One receipt can satisfy only its exact node ID.  A required instruction
    mapped to N independent nodes remains pending until all N receipts exist.
    """

    if not isinstance(workflow_ir, WorkflowIR):
        _raise_ir("invalid_workflow_ir", "workflow_ir must be validated first")
    if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence):
        _raise_ir("invalid_receipts", "receipts must be an array")
    node_map = workflow_ir.node_map
    receipt_statuses: dict[str, str] = {}
    for index, raw_receipt in enumerate(receipts):
        prefix = f"receipts[{index}]"
        receipt = _mapping(raw_receipt, prefix)
        _known_fields(receipt, _RECEIPT_FIELDS, prefix)
        node_id = _identifier(
            _required(receipt, "node_id", prefix), f"{prefix}.node_id"
        )
        if node_id not in node_map:
            _raise_ir(
                "unknown_receipt_node",
                f"receipt references undeclared node {node_id!r}",
                f"{prefix}.node_id",
            )
        if node_id in receipt_statuses:
            _raise_ir(
                "duplicate_node_receipt",
                f"duplicate terminal receipt for {node_id!r}",
                f"{prefix}.node_id",
            )
        status = _text(_required(receipt, "status", prefix), f"{prefix}.status")
        if status not in _RECEIPT_STATUSES:
            _raise_ir(
                "invalid_receipt_status",
                f"status must be one of {sorted(_RECEIPT_STATUSES)}",
                f"{prefix}.status",
            )
        digest_value = receipt.get("result_sha256")
        if status in {"succeeded", "degraded"}:
            _sha256(digest_value, f"{prefix}.result_sha256")
        elif digest_value not in {None, ""}:
            _sha256(digest_value, f"{prefix}.result_sha256")
        receipt_statuses[node_id] = status

    findings: list[InstructionExecutionFinding] = []
    blocking: list[str] = []
    for item in workflow_ir.coverage:
        if item.disposition != "mapped":
            status = item.disposition
            missing: tuple[str, ...] = ()
            degraded: tuple[str, ...] = ()
            failed: tuple[str, ...] = ()
        else:
            missing = tuple(
                node_id for node_id in item.node_ids if node_id not in receipt_statuses
            )
            degraded = tuple(
                node_id
                for node_id in item.node_ids
                if receipt_statuses.get(node_id) == "degraded"
            )
            failed = tuple(
                node_id
                for node_id in item.node_ids
                if receipt_statuses.get(node_id) in {"failed", "unsupported", "skipped"}
            )
            if missing:
                status = "pending"
            elif failed:
                status = "failed"
            elif degraded:
                status = "degraded"
            else:
                status = "verified"
        if (
            item.requirement in {"required", "conditional"}
            and item.disposition == "mapped"
            and status != "verified"
        ):
            blocking.append(item.instruction_id)
        findings.append(
            InstructionExecutionFinding(
                instruction_id=item.instruction_id,
                requirement=item.requirement,
                status=status,
                node_ids=item.node_ids,
                missing_node_ids=missing,
                degraded_node_ids=degraded,
                failed_node_ids=failed,
            )
        )
    return InstructionExecutionCoverageReport(
        complete=not blocking,
        workflow_ir_sha256=workflow_ir.ir_sha256,
        findings=tuple(findings),
        blocking_instruction_ids=tuple(blocking),
    )


def compile_worker_wave_plan(workflow_ir: WorkflowIR) -> dict[str, Any]:
    """Lower a validated IR to the existing worker/wave execution-plan shape.

    This first adapter is deliberately conservative: the legacy runtime
    delegates both workers and aggregation steps to child agents, so nodes
    owned by another executor are rejected instead of silently rerouted.
    Explicit aggregate nodes and every required node downstream of one are
    lowered to ``aggregation_steps``; nodes that only depend on ordinary
    workers remain workers.  This topology-aware closure preserves an early
    verifier as a worker while allowing aggregate -> verify/synthesize/
    artifact chains without accepting a graph that the adapter later rejects.
    """

    if not isinstance(workflow_ir, WorkflowIR):
        raise WorkflowPlanAdapterError(
            "invalid_workflow_ir", "workflow_ir must be validated first"
        )
    required_nodes = [node for node in workflow_ir.nodes if node.required]
    for node in required_nodes:
        if node.executor != "child_agent":
            raise WorkflowPlanAdapterError(
                "unsupported_executor_for_worker_plan",
                (
                    "the legacy worker/wave runtime can lower only "
                    "child_agent-owned nodes"
                ),
                node_id=node.id,
            )

    required_node_ids = {node.id for node in required_nodes}
    aggregate_ids = {
        node.id for node in required_nodes if node.kind == "aggregate"
    }
    changed = True
    while changed:
        changed = False
        for node in required_nodes:
            if (
                node.id not in aggregate_ids
                and any(
                    dependency in aggregate_ids
                    for dependency in node.depends_on
                )
            ):
                aggregate_ids.add(node.id)
                changed = True
    worker_nodes = [node for node in required_nodes if node.id not in aggregate_ids]
    worker_ids = {node.id for node in worker_nodes}
    for node in worker_nodes:
        aggregate_dependencies = [
            dependency for dependency in node.depends_on if dependency in aggregate_ids
        ]
        if aggregate_dependencies:
            raise WorkflowPlanAdapterError(
                "worker_depends_on_aggregation",
                (
                    "the legacy runtime cannot dispatch a worker after an "
                    "aggregation step"
                ),
                node_id=node.id,
            )

    topological = [
        node
        for node in _topological_nodes(required_nodes)
        if node.id in required_node_ids
    ]
    worker_topological = [node for node in topological if node.id in worker_ids]
    level_by_worker: dict[str, int] = {}
    for node in worker_topological:
        worker_dependencies = [
            dependency for dependency in node.depends_on if dependency in worker_ids
        ]
        level_by_worker[node.id] = (
            max(level_by_worker[dependency] for dependency in worker_dependencies) + 1
            if worker_dependencies
            else 0
        )

    workers_by_level: dict[int, list[str]] = {}
    for node in worker_topological:
        workers_by_level.setdefault(level_by_worker[node.id], []).append(node.id)
    wave_id_by_worker: dict[str, str] = {}
    waves: list[dict[str, Any]] = []
    for level in sorted(workers_by_level):
        level_workers = workers_by_level[level]
        wave_id = f"ir-wave-{level + 1:03d}"
        for worker_id in level_workers:
            wave_id_by_worker[worker_id] = wave_id
        dependency_wave_ids: list[str] = []
        for worker_id in level_workers:
            node = next(node for node in worker_nodes if node.id == worker_id)
            for dependency in node.depends_on:
                dependency_wave = wave_id_by_worker.get(dependency)
                if (
                    dependency_wave
                    and dependency_wave != wave_id
                    and dependency_wave not in dependency_wave_ids
                ):
                    dependency_wave_ids.append(dependency_wave)
        waves.append(
            {
                "id": wave_id,
                "mode": "parallel" if len(level_workers) > 1 else "sequential",
                "workers": list(level_workers),
                "dependencies": dependency_wave_ids,
                "batch_limit": workflow_ir.policies.max_parallelism,
            }
        )

    instruction_units = {
        unit.id: unit for unit in workflow_ir.instruction_units
    }

    def instruction_refs(node: WorkflowNode) -> list[dict[str, Any]]:
        return [
            {
                "instruction_id": instruction_id,
                "source_path": instruction_units[instruction_id].source_path,
                "source_sha256": instruction_units[
                    instruction_id
                ].source_sha256,
                "start_line": instruction_units[instruction_id].start_line,
                "end_line": instruction_units[instruction_id].end_line,
                "text_sha256": instruction_units[instruction_id].text_sha256,
            }
            for instruction_id in node.instruction_ids
        ]

    def instruction_paths(node: WorkflowNode) -> list[str]:
        return list(
            dict.fromkeys(
                instruction_units[instruction_id].source_path
                for instruction_id in node.instruction_ids
            )
        )

    def instruction_source_bindings(
        node: WorkflowNode,
    ) -> list[dict[str, str]]:
        bindings: list[dict[str, str]] = []
        seen_paths: set[str] = set()
        for instruction_id in node.instruction_ids:
            unit = instruction_units[instruction_id]
            if unit.source_path in seen_paths:
                continue
            seen_paths.add(unit.source_path)
            bindings.append({
                "resource_path": unit.source_path,
                "sha256": unit.source_sha256,
            })
        return bindings

    worker_map: dict[str, dict[str, Any]] = {}
    for node in worker_topological:
        worker_map[node.id] = {
            "id": node.id,
            "name": node.title or node.id,
            "role_hint": node.role or node.title,
            # The legacy delegation runtime requires a concrete worker_file.
            # For prose-compiled Workflow IR, the first frozen instruction
            # source is the deterministic controller-owned worker contract.
            "file": instruction_paths(node)[0],
            "dependencies": [
                dependency for dependency in node.depends_on if dependency in worker_ids
            ],
            "instruction_ids": list(node.instruction_ids),
            "instruction_refs": instruction_refs(node),
            "instruction_source_bindings": instruction_source_bindings(node),
            "local_resources": instruction_paths(node),
            "capability_candidate_ids": list(node.capability_ids),
            "output_schema": node.result_schema,
            "result_id": node.result_id,
            "required_gate_ids": list(node.output_ids),
            "workflow_ir_kind": node.kind,
            "phase": node.phase,
            "round": node.round,
            "max_iterations": workflow_ir.policies.max_iterations_per_node,
        }

    aggregation_steps: list[dict[str, Any]] = []
    for node in topological:
        if node.id not in aggregate_ids:
            continue
        aggregation_steps.append(
            {
                "id": node.id,
                "description": node.title or node.role or node.id,
                "method": "workflow_ir",
                "required": True,
                "depends_on": [
                    dependency
                    for dependency in node.depends_on
                    if dependency in aggregate_ids
                ],
                "input_worker_ids": [
                    dependency
                    for dependency in node.depends_on
                    if dependency in worker_ids
                ],
                "instruction_ids": list(node.instruction_ids),
                "instruction_refs": instruction_refs(node),
                "instruction_source_bindings": (
                    instruction_source_bindings(node)
                ),
                "local_resources": instruction_paths(node),
                "capability_candidate_ids": list(node.capability_ids),
                "output_schema": node.result_schema,
                "result_id": node.result_id,
                "output_ids": list(node.output_ids),
                "checks": [
                    {"id": output_id, "required": True}
                    for output_id in node.output_ids
                ],
                "phase": node.phase,
                "round": node.round,
                "max_iterations": workflow_ir.policies.max_iterations_per_node,
            }
        )

    logical_outputs = [output.to_dict() for output in workflow_ir.outputs]
    diagnostics = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "source": "workflow_ir",
        "workflow_ir_sha256": workflow_ir.ir_sha256,
    }
    return {
        "schema_version": "workflow-ir-worker-plan-v1",
        "workflow_ir_sha256": workflow_ir.ir_sha256,
        "selection": "workflow_ir",
        "route_id": "workflow-ir",
        "route": {},
        "workers": worker_map,
        "declared_routes": [],
        "intent_classification": {},
        "available_bootstrap_sources": [],
        "available_aggregation_steps": list(aggregation_steps),
        "required_workers": [node.id for node in worker_topological],
        "waves": waves,
        "bootstrap_sources": [],
        "aggregation_steps": aggregation_steps,
        # Logical output IDs are not filesystem authority.  Artifact path
        # binding remains the responsibility of the existing output-contract
        # compiler, so this adapter never invents file paths or density rules.
        "requires_full_output": False,
        "requires_artifact_output": False,
        "output_contract": {},
        "workflow_ir_outputs": logical_outputs,
        "instruction_coverage": [item.to_dict() for item in workflow_ir.coverage],
        "diagnostics": diagnostics,
    }
