"""Typed, least-privilege capability planning for standard Agent Skills.

The Agent Skills format deliberately leaves the Markdown instruction body
free-form.  Consequently this module does not try to translate English (or
any other language) verbs into tool names.  Instead it builds a finite catalog
from capabilities that the backend has already authorized for the run.  After
the complete canonical ``SKILL.md`` has been disclosed, the model may classify
catalog entries as required or optional through one typed tool call.

The model can only *select* catalog entries.  It cannot create a native tool,
script digest, command grant, HTTP prefix, MCP name, or package resource.  A
selection is a bounded run-scoped authorization, not a one-call token:
``required`` additionally asks for one exact minimum dispatch receipt before
terminal synthesis, while ``optional`` has no receipt obligation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlsplit

from skills.http_grants import canonical_https_prefix, canonical_https_request_url
from workflow_ir import (
    InstructionDocument,
    WorkflowIRValidationError,
    WorkflowPlanAdapterError,
    compile_workflow_plan,
    compile_worker_wave_plan,
    instruction_catalog_payload,
    validate_workflow_ir,
    workflow_plan_instruction_catalog_payload,
)


CAPABILITY_PLAN_TOOL_NAME = "submit_skill_capability_plan"
CAPABILITY_PLAN_SCHEMA_VERSION = 1
CALLABLE_SKILL_RESULT_RECEIPT_VERSION = 1
SKILL_PROCESS_EVIDENCE_RECEIPT_VERSION = 1
MAX_CAPABILITY_CANDIDATES = 512
MAX_PLAN_SELECTIONS = 256
MAX_OPTIONAL_SELECTIONS = 32
MAX_TOTAL_SELECTIONS = 64
MAX_NATIVE_CAPABILITY_CANDIDATES = 32
MAX_AUTHORITY_DOCUMENTS = 16
MAX_AUTHORITY_DOCUMENT_CHARS = 256_000
MAX_UNSUPPORTED_ITEMS = 64
MAX_WORKFLOW_INSTRUCTION_CATALOG_BYTES = 750_000
MAX_SANDBOX_EGRESS_PREFIXES = 256

# Fixed-shape, URL/body-free handler facts that may cross the child/parent
# boundary for exact capability matching.  Keep one projection authority so
# a validator change cannot silently depend on a field dropped by one of the
# several event/knowledge-gate adapters.
EXACT_CAPABILITY_RESULT_RECEIPT_FIELDS = (
    "sha256",
    "error_code",
    "request_sent",
    "request_number",
    "root_request_number",
    "matched_skill",
    "matched_prefix_sha256",
    "status",
    "plan_sha256",
    "activated_group_ids",
    "unresolved_group_ids",
    "unknown_check_ids",
    "process_evidence_receipt",
)


def project_exact_capability_result_receipt(
    result_data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project only fixed-shape evidence metadata across trust boundaries."""

    if not isinstance(result_data, dict):
        return {}
    return {
        key: result_data[key]
        for key in EXACT_CAPABILITY_RESULT_RECEIPT_FIELDS
        if result_data.get(key) is not None
    }

# These public bridge schemas are never selectable without a more specific,
# backend-issued candidate.  This keeps a model from turning the presence of a
# runner schema into package/script/egress authority.
_EXACT_GRANT_BRIDGES = frozenset({
    "run_skill_python",
    "run_skill_script",
    "run_skill_process",
    "run_declared_command",
    "skill_http_get",
    "skill_http_post_json",
})
_PLANNING_CONTROL_TOOLS = frozenset({
    CAPABILITY_PLAN_TOOL_NAME,
    "submit_knowledge_gate_decisions",
    "skills_list",
    "skill_view",
})
_CALLABLE_SKILL_RUNNER_TOOLS = frozenset({
    "run_skill_process",
    "run_skill_python",
})
_TYPED_RESULT_FAILURE_STATUSES = frozenset({
    "blocked",
    "error",
    "failed",
    "timeout",
})
_TYPED_RESULT_POSITIVE_STATUSES = frozenset({
    "complete",
    "completed",
    "ok",
    "pass",
    "passed",
    "success",
    "succeeded",
})
_PROCESS_ID_RE = re.compile(r"^sp_[A-Za-z0-9_-]{24,96}$")
_PUBLIC_PROCESS_METHOD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_PROCESS_RECEIPT_COMPLETION_KINDS = frozenset({
    "structured_call",
    "cli_exit",
    "artifact_sync",
    "artifact_close",
})


@dataclass(frozen=True)
class CapabilityPlanResult:
    """Authoritative result of validating one model-authored selection."""

    valid: bool
    payload: dict[str, Any]


def _nonempty_typed_error(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return bool(value)


def build_callable_skill_result_receipt(
    tool_name: str,
    result_data: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Classify only a callable runner's immediate returned ``result``.

    Runner transport/execution can succeed while the called Skill function
    returns a typed failure object. Inspecting only the exact top-level
    ``result`` prevents ordinary rows containing nested ``error`` fields from
    being mistaken for a failed evidence acquisition.
    """

    if tool_name not in _CALLABLE_SKILL_RUNNER_TOOLS:
        return None
    process_receipt = normalize_skill_process_evidence_receipt(
        (result_data or {}).get("process_evidence_receipt")
        if isinstance(result_data, dict)
        else None
    )
    if (
        tool_name == "run_skill_process"
        and process_receipt is not None
        and process_receipt.get("completion_kind") == "structured_call"
    ):
        return dict(process_receipt["callable_result_receipt"])

    returned = (
        result_data.get("result")
        if isinstance(result_data, dict)
        else None
    )
    if not isinstance(returned, dict):
        return {
            "version": CALLABLE_SKILL_RESULT_RECEIPT_VERSION,
            "result_object_observed": False,
            "typed_failure": False,
            "positive_success_observed": False,
            "failure_reason_codes": [],
        }

    status = (
        str(returned.get("status") or "").strip().casefold()
        if isinstance(returned.get("status"), str)
        else ""
    )
    positive_success = bool(
        status in _TYPED_RESULT_POSITIVE_STATUSES
        or returned.get("success") is True
        or returned.get("ok") is True
    )
    failure_reasons: list[str] = []
    if status in _TYPED_RESULT_FAILURE_STATUSES:
        failure_reasons.append("typed_status_failure")
    if returned.get("success") is False:
        failure_reasons.append("typed_success_false")
    if returned.get("ok") is False:
        failure_reasons.append("typed_ok_false")
    if (
        _nonempty_typed_error(returned.get("error"))
        and not positive_success
    ):
        failure_reasons.append("typed_error_without_positive_success")
    return {
        "version": CALLABLE_SKILL_RESULT_RECEIPT_VERSION,
        "result_object_observed": True,
        "typed_failure": bool(failure_reasons),
        "positive_success_observed": positive_success,
        "failure_reason_codes": failure_reasons,
    }


def callable_skill_result_receipt_is_failure(value: Any) -> bool:
    """Consume only the versioned machine receipt; legacy absence is neutral."""

    return bool(
        isinstance(value, dict)
        and value.get("version")
        == CALLABLE_SKILL_RESULT_RECEIPT_VERSION
        and value.get("typed_failure") is True
    )


def callable_skill_result_evidence_outcome(
    tool_name: str,
    result_data: dict[str, Any] | None,
    transport_outcome: str,
) -> str:
    """Project transport success into its evidence-acquisition outcome."""

    normalized_transport = str(transport_outcome or "")
    if tool_name == "run_skill_process":
        if normalized_transport.casefold() != "success":
            return normalized_transport
        process_receipt = normalize_skill_process_evidence_receipt(
            (result_data or {}).get("process_evidence_receipt")
            if isinstance(result_data, dict)
            else None
        )
        # Opening a lease, enqueueing a method, polling incomplete output, or
        # syncing no artifact is transport progress only. It cannot satisfy an
        # exact evidence obligation or be downgraded into a failed attempt.
        if process_receipt is None:
            return "pending"
        return str(process_receipt["outcome"])

    receipt = build_callable_skill_result_receipt(tool_name, result_data)
    if (
        normalized_transport.casefold() == "success"
        and callable_skill_result_receipt_is_failure(receipt)
    ):
        return "error"
    return normalized_transport


def skill_process_artifact_manifest_sha256(
    artifacts: Iterable[dict[str, Any]],
) -> tuple[int, str] | None:
    """Return a canonical manifest identity for verified workspace artifacts."""

    rows: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            return None
        path = _safe_relative_path(item.get("path"))
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            path is None
            or path in seen_paths
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not _valid_sha256(digest)
        ):
            return None
        seen_paths.add(path)
        rows.append({
            "path": path,
            "size_bytes": size,
            "sha256": digest,
        })
    if not rows or len(rows) > 512:
        return None
    rows.sort(key=lambda row: row["path"])
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(rows), hashlib.sha256(encoded).hexdigest()


def _normalized_callable_receipt(value: Any) -> dict[str, Any] | None:
    if (
        not isinstance(value, dict)
        or set(value) != {
            "version",
            "result_object_observed",
            "typed_failure",
            "positive_success_observed",
            "failure_reason_codes",
        }
        or value.get("version") != CALLABLE_SKILL_RESULT_RECEIPT_VERSION
        or not isinstance(value.get("result_object_observed"), bool)
        or not isinstance(value.get("typed_failure"), bool)
        or not isinstance(value.get("positive_success_observed"), bool)
        or not isinstance(value.get("failure_reason_codes"), list)
        or len(value["failure_reason_codes"]) > 4
        or any(
            item not in {
                "typed_status_failure",
                "typed_success_false",
                "typed_ok_false",
                "typed_error_without_positive_success",
            }
            for item in value["failure_reason_codes"]
        )
        or value["typed_failure"] != bool(value["failure_reason_codes"])
    ):
        return None
    return {
        "version": CALLABLE_SKILL_RESULT_RECEIPT_VERSION,
        "result_object_observed": value["result_object_observed"],
        "typed_failure": value["typed_failure"],
        "positive_success_observed": value["positive_success_observed"],
        "failure_reason_codes": list(value["failure_reason_codes"]),
    }


def _process_receipt_id(core: dict[str, Any]) -> str:
    identity = {
        "version": SKILL_PROCESS_EVIDENCE_RECEIPT_VERSION,
        "skill_name": core["skill_name"],
        "script_resource": core["script_resource"],
        "script_sha256": core["script_sha256"],
        "package_sha256": core["package_sha256"],
        "process_id": core["process_id"],
        # One structured method invocation is one evidence identity even when
        # both its call_result and its artifact sync are observed. CLI exit and
        # artifact sync likewise share the process execution identity.
        **({"call_id": core["call_id"]} if core.get("call_id") else {}),
        **(
            {
                "stdout_size_bytes": core["stdout_size_bytes"],
                "stdout_sha256": core["stdout_sha256"],
            }
            if core.get("stdout_sha256") is not None
            else {}
        ),
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "spr_" + hashlib.sha256(encoded).hexdigest()


def build_skill_process_evidence_receipt(
    *,
    skill_name: str,
    script_resource: str,
    script_sha256: str,
    package_sha256: str,
    process_id: str,
    invocation_mode: str,
    completion_kind: str,
    outcome: str,
    call_id: str | None = None,
    method_name: str | None = None,
    call_result_status: str | None = None,
    callable_result_receipt: dict[str, Any] | None = None,
    returncode: int | None = None,
    stdout_size_bytes: int | None = None,
    stdout_sha256: str | None = None,
    artifact_count: int | None = None,
    artifact_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Build one bounded, content-addressed terminal process receipt."""

    core: dict[str, Any] = {
        "version": SKILL_PROCESS_EVIDENCE_RECEIPT_VERSION,
        "skill_name": skill_name,
        "script_resource": script_resource,
        "script_sha256": script_sha256,
        "package_sha256": package_sha256,
        "process_id": process_id,
        "invocation_mode": invocation_mode,
        "completion_kind": completion_kind,
        "outcome": outcome,
    }
    if completion_kind == "structured_call":
        core.update({
            "call_id": call_id,
            "method_name": method_name,
            "call_result_status": call_result_status,
            "callable_result_receipt": callable_result_receipt,
        })
    elif completion_kind == "cli_exit":
        core["returncode"] = returncode
        if stdout_size_bytes is not None or stdout_sha256 is not None:
            core.update({
                "stdout_size_bytes": stdout_size_bytes,
                "stdout_sha256": stdout_sha256,
            })
    else:
        core.update({
            "artifact_count": artifact_count,
            "artifact_manifest_sha256": artifact_manifest_sha256,
        })
        if call_id is not None or method_name is not None:
            core.update({
                "call_id": call_id,
                "method_name": method_name,
            })
    core["receipt_id"] = _process_receipt_id(core)
    normalized = normalize_skill_process_evidence_receipt(core)
    if normalized is None:
        raise ValueError("invalid terminal Skill process evidence receipt")
    return normalized


def normalize_skill_process_evidence_receipt(
    value: Any,
) -> dict[str, Any] | None:
    """Validate a Harness-issued terminal receipt without trusting prose."""

    if not isinstance(value, dict):
        return None
    common_keys = {
        "version",
        "receipt_id",
        "skill_name",
        "script_resource",
        "script_sha256",
        "package_sha256",
        "process_id",
        "invocation_mode",
        "completion_kind",
        "outcome",
    }
    completion_kind = value.get("completion_kind")
    invocation_mode = value.get("invocation_mode")
    expected_keys = set(common_keys)
    if completion_kind == "structured_call":
        expected_keys.update({
            "call_id",
            "method_name",
            "call_result_status",
            "callable_result_receipt",
        })
    elif completion_kind == "cli_exit":
        expected_keys.add("returncode")
        if "stdout_size_bytes" in value or "stdout_sha256" in value:
            expected_keys.update({"stdout_size_bytes", "stdout_sha256"})
    elif completion_kind in {"artifact_sync", "artifact_close"}:
        expected_keys.update({
            "artifact_count",
            "artifact_manifest_sha256",
            "call_id",
            "method_name",
        })
    if set(value) != expected_keys:
        return None
    if (
        value.get("version") != SKILL_PROCESS_EVIDENCE_RECEIPT_VERSION
        or not isinstance(value.get("skill_name"), str)
        or not value["skill_name"]
        or len(value["skill_name"]) > 160
        or _safe_relative_path(value.get("script_resource")) is None
        or not _valid_sha256(value.get("script_sha256"))
        or not _valid_sha256(value.get("package_sha256"))
        or not isinstance(value.get("process_id"), str)
        or _PROCESS_ID_RE.fullmatch(value["process_id"]) is None
        or invocation_mode not in {"cli", "instance", "factory"}
        or completion_kind not in _PROCESS_RECEIPT_COMPLETION_KINDS
        or value.get("outcome") not in {"success", "error"}
    ):
        return None

    if completion_kind == "structured_call":
        callable_receipt = _normalized_callable_receipt(
            value.get("callable_result_receipt")
        )
        call_status = value.get("call_result_status")
        if (
            invocation_mode not in {"instance", "factory"}
            or not isinstance(value.get("call_id"), str)
            or not value["call_id"]
            or len(value["call_id"]) > 128
            or not isinstance(value.get("method_name"), str)
            or _PUBLIC_PROCESS_METHOD_RE.fullmatch(value["method_name"])
            is None
            or call_status not in {"success", "error"}
            or callable_receipt is None
            or (
                value["outcome"] == "success"
                and (
                    call_status != "success"
                    or callable_receipt["typed_failure"]
                )
            )
            or (
                value["outcome"] == "error"
                and call_status == "success"
                and not callable_receipt["typed_failure"]
            )
        ):
            return None
    elif completion_kind == "cli_exit":
        returncode = value.get("returncode")
        if (
            invocation_mode != "cli"
            or isinstance(returncode, bool)
            or not isinstance(returncode, int)
            or not -(2**31) <= returncode <= 2**31 - 1
            or (value["outcome"] == "success") != (returncode == 0)
        ):
            return None
        if "stdout_size_bytes" in value or "stdout_sha256" in value:
            stdout_size = value.get("stdout_size_bytes")
            if (
                isinstance(stdout_size, bool)
                or not isinstance(stdout_size, int)
                or not 1 <= stdout_size <= 2**63 - 1
                or not _valid_sha256(value.get("stdout_sha256"))
            ):
                return None
        callable_receipt = None
    else:
        artifact_count = value.get("artifact_count")
        if (
            value["outcome"] != "success"
            or invocation_mode not in {"instance", "factory"}
            or isinstance(artifact_count, bool)
            or not isinstance(artifact_count, int)
            or not 1 <= artifact_count <= 512
            or not _valid_sha256(value.get("artifact_manifest_sha256"))
            or not isinstance(value.get("call_id"), str)
            or not value["call_id"]
            or len(value["call_id"]) > 128
            or not isinstance(value.get("method_name"), str)
            or _PUBLIC_PROCESS_METHOD_RE.fullmatch(value["method_name"])
            is None
        ):
            return None
        callable_receipt = None

    normalized = dict(value)
    if callable_receipt is not None:
        normalized["callable_result_receipt"] = callable_receipt
    expected_receipt_id = _process_receipt_id(normalized)
    if value.get("receipt_id") != expected_receipt_id:
        return None
    return normalized


def _stable_id(kind: str, value: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{value}".encode("utf-8")).hexdigest()[:24]
    return f"{kind}-{digest}"


def _safe_relative_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if "\\" in value or "\x00" in value or any(ord(char) < 32 for char in value):
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return str(path)


def _current_loaded_resource_sha256(
    loaded_package: dict[str, Any],
    resource_path: str,
) -> str | None:
    """Hash one loader-owned package resource without following escapes."""

    skill_dir = loaded_package.get("skill_dir")
    if not isinstance(skill_dir, str) or not skill_dir:
        return None
    try:
        from skills.path_safety import validate_skill_resource

        root = Path(skill_dir).resolve(strict=True)
        checked = validate_skill_resource(
            root,
            resource_path,
            expected_kind="file",
            require_relative=True,
        )
        if not checked.valid or checked.path is None:
            return None
        return hashlib.sha256(checked.path.read_bytes()).hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None


def _body_references_path(body: str, path: str) -> bool:
    """Require one literal canonical path mention in authoritative prose/code.

    This deliberately has no language vocabulary and does not skip fenced
    blocks: a standard Skill may put its one-off invocation in a code fence.
    Exact matching prevents a basename such as ``test.py`` from granting an
    unrelated ``examples/test.py`` package file.
    """

    if not body or not path:
        return False
    # These common standard-Skill spellings all denote the same exact package
    # resource.  Match the complete token, longest first, rather than finding
    # the canonical suffix inside a larger/adjacent path.
    tokens = (
        f"${{SKILL_DIR}}/{path}",
        f"./{path}",
        path,
    )
    token_chars = (
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789_./-${}"
    )
    for token in tokens:
        start = 0
        while True:
            index = body.find(token, start)
            if index < 0:
                break
            before = body[index - 1] if index else ""
            after_index = index + len(token)
            after = body[after_index] if after_index < len(body) else ""
            if before not in token_chars and after not in token_chars:
                return True
            start = index + 1
    return False


def _dedupe_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        identifier = str(candidate.get("id") or "")
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        result.append(candidate)
        if len(result) >= MAX_CAPABILITY_CANDIDATES:
            break
    return result


def _native_capability_family(name: str) -> str:
    if name.startswith("browser_"):
        return "browser"
    if name.startswith("web_"):
        return "web_retrieval"
    if name in {"read_file", "search_files"}:
        return "workspace_read"
    if name in {"write_file", "patch_file", "merge_files", "skill_copy_resource"}:
        return "workspace_write"
    if name == "execute_code":
        return "compute"
    if name == "delegate_task":
        return "delegation"
    if name.startswith("image_") or name == "vision_analyze":
        return "media"
    if name.startswith("session") or name.startswith("sessions_"):
        return "session"
    return "native"


def _native_execution_environment(name: str) -> str:
    family = _native_capability_family(name)
    if family == "browser":
        return "browser_sidecar"
    if family == "compute":
        return "isolated_compute"
    if family in {"workspace_read", "workspace_write"}:
        return "workspace"
    if family == "web_retrieval":
        return "network_client"
    return "harness_runtime"


def _native_impact_level(name: str, metadata: dict[str, Any]) -> str:
    if metadata.get("destructive"):
        return "destructive"
    if name == "execute_code":
        return "isolated_execution"
    if metadata.get("mutates_global_state"):
        return "global_mutation"
    if metadata.get("mutates_workspace"):
        return "workspace_mutation"
    if metadata.get("read_only"):
        return "read_only"
    return "external_interaction"


def _valid_sha256(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _script_runtime_profile(path: str) -> str:
    """Derive a backend-owned runtime family from an exact file suffix.

    Natural-language framework names are instructions, not runtime authority
    and not reliable evidence of what a runtime image contains.
    """

    suffix = PurePosixPath(path).suffix.casefold()
    if suffix == ".py":
        return "python"
    if suffix in {".js", ".mjs", ".cjs"}:
        return "node"
    if suffix in {".sh", ".bash"}:
        return "posix_shell"
    return "registered_script_runner"


def _validated_authority_documents(
    loaded_package: dict[str, Any],
    documents: Iterable[dict[str, Any]],
) -> list[dict[str, str]]:
    """Validate bounded, root-linked, content-addressed references."""

    linked = loaded_package.get("linked_files")
    linked_paths: set[str] = set()
    if isinstance(linked, dict):
        for paths in linked.values():
            if not isinstance(paths, list):
                continue
            for raw_path in paths:
                path = _safe_relative_path(raw_path)
                if path is not None:
                    linked_paths.add(path)

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in documents:
        if not isinstance(raw, dict):
            continue
        path = _safe_relative_path(raw.get("resource_path"))
        digest = raw.get("sha256")
        content = raw.get("content")
        suffix = (
            PurePosixPath(path).suffix.casefold()
            if path is not None else ""
        )
        if (
            path is None
            or path in seen
            or path not in linked_paths
            or suffix in {".py", ".js", ".mjs", ".cjs", ".sh", ".bash"}
            or not _valid_sha256(digest)
            or not isinstance(content, str)
            or len(content) > MAX_AUTHORITY_DOCUMENT_CHARS
            or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest
        ):
            continue
        result.append({
            "resource_path": path,
            "sha256": str(digest),
            "content": content,
        })
        seen.add(path)
        if len(result) >= MAX_AUTHORITY_DOCUMENTS:
            break
    return result


def _validated_workflow_instruction_documents(
    documents: Iterable[InstructionDocument],
    *,
    body_sha256: str,
    authority_documents: Iterable[dict[str, str]],
    workflow_ir_required: bool,
) -> tuple[
    tuple[InstructionDocument, ...],
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Bind runtime-owned IR authority to the exact disclosed Skill closure."""

    if not isinstance(workflow_ir_required, bool):
        raise ValueError("workflow_ir_required must be a boolean")
    if isinstance(documents, (str, bytes)):
        raise ValueError(
            "instruction_documents must contain InstructionDocument objects"
        )
    normalized = tuple(documents)
    if workflow_ir_required and not normalized:
        raise ValueError(
            "workflow_ir_required needs runtime-owned instruction_documents"
        )
    if not normalized:
        return (), None, None

    try:
        projection = instruction_catalog_payload(normalized)
        planning_projection = workflow_plan_instruction_catalog_payload(
            normalized
        )
    except WorkflowIRValidationError as exc:
        raise ValueError(
            f"invalid workflow instruction documents ({exc.code}): {exc}"
        ) from exc

    authorized_digests = {
        "SKILL.md": body_sha256,
        **{
            str(document.get("resource_path") or ""): str(document.get("sha256") or "")
            for document in authority_documents
            if isinstance(document, dict)
        },
    }
    if not any(document.source_path == "SKILL.md" for document in normalized):
        raise ValueError(
            "workflow instruction authority must include canonical SKILL.md"
        )
    for document in normalized:
        if authorized_digests.get(document.source_path) != (document.source_sha256):
            raise ValueError(
                "workflow instruction document is outside the exact "
                f"content-addressed authority closure: {document.source_path}"
            )

    encoded = json.dumps(
        planning_projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_WORKFLOW_INSTRUCTION_CATALOG_BYTES:
        raise ValueError(
            "workflow instruction catalog exceeds the bounded model-visible "
            f"limit of {MAX_WORKFLOW_INSTRUCTION_CATALOG_BYTES} bytes"
        )
    return normalized, projection, planning_projection


def build_capability_catalog(
    *,
    skill_name: str,
    loaded_package: dict[str, Any],
    available_tools: Iterable[str],
    skill_package_sha256: str = "",
    runnable_scripts: Iterable[tuple[str, str]] = (),
    command_grants: Iterable[dict[str, Any]] = (),
    http_prefixes: Iterable[tuple[str, str]] = (),
    http_post_prefixes: Iterable[tuple[str, str]] = (),
    sandbox_egress_rules: Iterable[
        tuple[str, str, tuple[str, ...]]
    ] = (),
    native_browser_egress_rules: Iterable[
        tuple[str, tuple[str, ...]]
    ] = (),
    exact_mcp_names: Iterable[str] = (),
    native_tool_metadata: dict[str, dict[str, Any]] | None = None,
    authority_documents: Iterable[dict[str, Any]] = (),
    script_runtime_profiles: dict[str, dict[str, Any]] | None = None,
    required_native_tool_groups: Iterable[Iterable[str]] = (),
    instruction_documents: Iterable[InstructionDocument] = (),
    workflow_ir_required: bool = False,
) -> dict[str, Any]:
    """Build a finite catalog from backend-authorized, current capabilities.

    Package files are not executable merely because they exist.  A script or
    inert resource enters this catalog only when its exact canonical relative
    path is literally referenced by ``SKILL.md``.  Structured workflow
    closures are compiled elsewhere and do not use this standard-body path.
    """

    # Deferred to avoid importing the tools package while this compiler module
    # itself is being imported by a runner registered from ``tools.__init__``.
    from tools.session_sandbox_policy import (
        SessionSandboxPolicyError,
        normalize_http_url_prefix,
        normalize_session_sandbox_methods,
    )

    body = str(loaded_package.get("content") or "")
    body_sha256 = str(loaded_package.get("skill_md_sha256") or "")
    if not (
        len(body_sha256) == 64
        and all(char in "0123456789abcdef" for char in body_sha256)
    ):
        # Compatibility for programmatically constructed test/package records:
        # bind both parsed frontmatter and body, never body alone.
        canonical_document = json.dumps(
            {
                "frontmatter": loaded_package.get("frontmatter") or {},
                "body": body,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        body_sha256 = hashlib.sha256(canonical_document.encode("utf-8")).hexdigest()
    package_sha256 = (
        str(skill_package_sha256)
        if _valid_sha256(skill_package_sha256)
        else ""
    )
    ordered_tools = list(dict.fromkeys(
        str(name) for name in available_tools if isinstance(name, str) and name
    ))
    available = set(ordered_tools)
    http_prefix_rows = tuple(
        (granted_skill, prefix)
        for granted_skill, prefix in http_prefixes
        if isinstance(granted_skill, str)
        and isinstance(prefix, str)
    )
    http_post_prefix_rows = tuple(
        (granted_skill, prefix)
        for granted_skill, prefix in http_post_prefixes
        if isinstance(granted_skill, str)
        and isinstance(prefix, str)
    )
    sandbox_egress_url_prefixes = list(dict.fromkeys(
        prefix
        for granted_skill, prefix in (
            *http_prefix_rows,
            *http_post_prefix_rows,
        )
        if granted_skill == skill_name and prefix
    ))
    sandbox_methods_by_prefix: dict[str, set[str]] = {}
    for granted_skill, prefix, methods in sandbox_egress_rules:
        if granted_skill != skill_name:
            continue
        try:
            canonical_prefix = normalize_http_url_prefix(prefix)
            canonical_methods = normalize_session_sandbox_methods(methods)
        except SessionSandboxPolicyError:
            continue
        if canonical_prefix != prefix:
            continue
        sandbox_methods_by_prefix.setdefault(
            canonical_prefix,
            set(),
        ).update(canonical_methods)
    if not sandbox_methods_by_prefix:
        # Compatibility for callers compiled before the method-preserving
        # ledger existed. This remains exact-prefix GET/HEAD authority; POST is
        # added only from the separate explicit POST/GraphQL ledger.
        post_set = {
            prefix
            for granted_skill, prefix in http_post_prefix_rows
            if granted_skill == skill_name
        }
        for prefix in sandbox_egress_url_prefixes:
            try:
                canonical_prefix = normalize_http_url_prefix(prefix)
            except SessionSandboxPolicyError:
                continue
            methods = {"GET", "HEAD"}
            if prefix in post_set:
                methods.add("POST")
            sandbox_methods_by_prefix.setdefault(
                canonical_prefix,
                set(),
            ).update(methods)
    sandbox_egress_rule_rows = [
        {
            "methods": list(normalize_session_sandbox_methods(methods)),
            "url_prefix": prefix,
        }
        for prefix, methods in sorted(sandbox_methods_by_prefix.items())
    ]
    browser_methods_by_prefix = {
        prefix: set(methods)
        for prefix, methods in sandbox_methods_by_prefix.items()
    }
    for raw_prefix, raw_methods in native_browser_egress_rules:
        try:
            prefix = normalize_http_url_prefix(raw_prefix)
            methods = normalize_session_sandbox_methods(raw_methods)
        except SessionSandboxPolicyError:
            continue
        if prefix != raw_prefix:
            continue
        browser_methods_by_prefix.setdefault(prefix, set()).update(methods)
    native_browser_rule_rows = [
        {
            "methods": list(normalize_session_sandbox_methods(methods)),
            "url_prefix": prefix,
        }
        for prefix, methods in sorted(browser_methods_by_prefix.items())
    ]
    candidates: list[dict[str, Any]] = []

    metadata_by_tool = native_tool_metadata or {}
    native_candidate_count = 0
    for name in ordered_tools:
        if (
            name in _PLANNING_CONTROL_TOOLS
            or name in _EXACT_GRANT_BRIDGES
            or name.startswith("mcp_")
        ):
            continue
        if native_candidate_count >= MAX_NATIVE_CAPABILITY_CANDIDATES:
            continue
        metadata = dict(metadata_by_tool.get(name) or {})
        candidate = {
            "id": _stable_id("tool", name),
            "kind": "native_tool",
            "tool_name": name,
            "capability_family": _native_capability_family(name),
            "execution_environment": _native_execution_environment(name),
            "impact_level": _native_impact_level(name, metadata),
            "read_only": bool(metadata.get("read_only")),
            "salvage_safe": bool(metadata.get("salvage_safe")),
            "external_interaction": bool(
                metadata.get("external_interaction")
            ),
            "description": (
                "Backend-authorized native tool. Selecting it only narrows the "
                "existing run surface; it creates no additional authority."
            ),
        }
        if name == "browser_navigate" and native_browser_rule_rows:
            candidate["browser_egress_rules"] = [
                {
                    "methods": list(rule["methods"]),
                    "url_prefix": str(rule["url_prefix"]),
                }
                for rule in native_browser_rule_rows
            ]
            candidate["skill_name"] = skill_name
            candidate["skill_md_sha256"] = body_sha256
            if package_sha256:
                candidate["package_sha256"] = package_sha256
        candidates.append(candidate)
        native_candidate_count += 1

    linked = loaded_package.get("linked_files")
    linked_resources: list[str] = []
    referenced_resources: list[str] = []
    if isinstance(linked, dict):
        for paths in linked.values():
            if not isinstance(paths, list):
                continue
            for raw_path in paths:
                path = _safe_relative_path(raw_path)
                if path:
                    linked_resources.append(path)
                    if _body_references_path(body, path):
                        referenced_resources.append(path)
    linked_resources = list(dict.fromkeys(linked_resources))
    referenced_resources = list(dict.fromkeys(referenced_resources))

    trusted_documents = _validated_authority_documents(
        loaded_package,
        authority_documents,
    )
    runtime_profiles = (
        script_runtime_profiles
        if isinstance(script_runtime_profiles, dict) else {}
    )
    workflow = loaded_package.get("workflow_contract")
    if not isinstance(workflow, dict):
        workflow = {}
    runtime_manifest = loaded_package.get("runtime_profile_manifest")
    if not isinstance(runtime_manifest, dict):
        runtime_manifest = workflow.get(
            "_chatds_runtime_profile_manifest"
        )
    runtime_manifest_rows = {
        str(row.get("entrypoint") or ""): row
        for row in (
            runtime_manifest.get("scripts") or []
            if isinstance(runtime_manifest, dict) else []
        )
        if (
            isinstance(row, dict)
            and row.get("manifest_declared") is True
            and str(row.get("entrypoint") or "")
        )
    }
    # A bundled reference becomes instruction authority only after the root
    # names that exact path and a complete read supplies its current digest.
    root_linked_documents = [
        document
        for document in trusted_documents
        if document["resource_path"] in referenced_resources
    ]
    authority_by_referenced_path: dict[str, dict[str, str]] = {}
    for document in root_linked_documents:
        for path in linked_resources:
            if _body_references_path(document["content"], path):
                authority_by_referenced_path.setdefault(path, document)
    for path in authority_by_referenced_path:
        if path not in referenced_resources:
            referenced_resources.append(path)

    for path in referenced_resources:
        declaring_document = (
            None
            if _body_references_path(body, path)
            else authority_by_referenced_path.get(path)
        )
        resource_sha256 = _current_loaded_resource_sha256(
            loaded_package,
            path,
        )
        # A path-only candidate is vulnerable to same-path replacement after
        # planning. Runtime-loaded packages always provide skill_dir; if the
        # exact current bytes cannot be hashed, do not issue the candidate.
        if not _valid_sha256(resource_sha256):
            continue
        candidates.append({
            "id": _stable_id(
                "resource",
                f"{skill_name}\0{path}\0{resource_sha256}",
            ),
            "kind": "skill_resource",
            "skill_name": skill_name,
            "resource_path": path,
            "sha256": resource_sha256,
            "authority_chain": (
                [
                    {"resource_path": "SKILL.md", "sha256": body_sha256},
                    {
                        "resource_path": declaring_document["resource_path"],
                        "sha256": declaring_document["sha256"],
                    },
                ]
                if declaring_document is not None
                else [{"resource_path": "SKILL.md", "sha256": body_sha256}]
            ),
            "description": (
                "Exact package resource referenced by a content-addressed "
                "authorized instruction document."
            ),
        })

    for raw_path, raw_digest in runnable_scripts:
        path = _safe_relative_path(raw_path)
        digest = str(raw_digest or "")
        root_declared = bool(path and _body_references_path(body, path))
        declaring_document = (
            None if root_declared else authority_by_referenced_path.get(path)
        )
        if (
            path is None
            or path not in linked_resources
            or not (root_declared or declaring_document is not None)
            or not _valid_sha256(digest)
        ):
            continue
        profile_record = (
            runtime_profiles.get(path)
            if path is not None else None
        )
        runtime_profile = (
            str(profile_record.get("runtime_profile") or "")
            if isinstance(profile_record, dict) else ""
        )
        if runtime_profile and (
            runtime_profile not in {"base-v1", "browser-automation-v1"}
            or profile_record.get("package_sha256") != package_sha256
            or profile_record.get("script_sha256") != digest
        ):
            continue
        candidate_methods_by_prefix = {
            prefix: set(methods)
            for prefix, methods in sandbox_methods_by_prefix.items()
        }
        if len(candidate_methods_by_prefix) > MAX_SANDBOX_EGRESS_PREFIXES:
            continue
        candidate_sandbox_egress_rule_rows = [
            {
                "methods": list(
                    normalize_session_sandbox_methods(methods)
                ),
                "url_prefix": prefix,
            }
            for prefix, methods in sorted(
                candidate_methods_by_prefix.items()
            )
        ]
        manifest_row = runtime_manifest_rows.get(path)
        manifest_authority: dict[str, str] | None = None
        if manifest_row is not None:
            manifest_path = _safe_relative_path(
                manifest_row.get("runtime_manifest_path")
            )
            manifest_digest = str(
                manifest_row.get("runtime_manifest_sha256") or ""
            )
            if (
                manifest_path is None
                or PurePosixPath(manifest_path).parent
                != PurePosixPath(".")
                or not _valid_sha256(manifest_digest)
                or manifest_row.get("package_sha256")
                != package_sha256
                or manifest_row.get("script_sha256") != digest
                or manifest_row.get("runtime_profile")
                != runtime_profile
                or _current_loaded_resource_sha256(
                    loaded_package,
                    manifest_path,
                )
                != manifest_digest
            ):
                continue
            manifest_authority = {
                "resource_path": manifest_path,
                "sha256": manifest_digest,
            }
        required_cwd = (
            profile_record.get("required_cwd")
            if isinstance(profile_record, dict)
            else None
        )
        if required_cwd not in {None, "script", "skill"}:
            continue
        runner_tools = [
            name for name in (
                "run_skill_process", "run_skill_script", "run_skill_python",
            )
            if name in available
            and (name != "run_skill_process" or bool(package_sha256))
            and (name != "run_skill_python" or PurePosixPath(path).suffix == ".py")
            and (
                (
                    runtime_profile != "browser-automation-v1"
                    and required_cwd is None
                )
                or name == "run_skill_process"
            )
        ]
        if not runner_tools:
            continue
        script_identity = (
            f"{skill_name}\0{path}\0{digest}\0"
            f"{runtime_profile or _script_runtime_profile(path)}"
            f"\0{required_cwd or 'cwd-independent'}"
        )
        if package_sha256:
            # Persistent execution snapshots the complete package, not merely
            # the entrypoint, and one-shot isolated runners use the same
            # snapshot boundary when this digest is available. Put the
            # immutable package identity into both the candidate ID and the
            # derived runtime grant so adding or mutating a helper file
            # invalidates the old plan.
            script_identity += f"\0{package_sha256}"
        if manifest_authority is not None:
            script_identity += (
                f"\0{manifest_authority['resource_path']}"
                f"\0{manifest_authority['sha256']}"
            )
        authority_chain = (
            [
                {"resource_path": "SKILL.md", "sha256": body_sha256},
                {
                    "resource_path": declaring_document["resource_path"],
                    "sha256": declaring_document["sha256"],
                },
            ]
            if declaring_document is not None
            else [{"resource_path": "SKILL.md", "sha256": body_sha256}]
        )
        if (
            manifest_authority is not None
            and manifest_authority not in authority_chain
        ):
            authority_chain.append(manifest_authority)
        candidates.append({
            "id": _stable_id("script", script_identity),
            "kind": "skill_script",
            "skill_name": skill_name,
            "resource_path": path,
            "sha256": digest,
            "tool_names": runner_tools,
            **(
                {"package_sha256": package_sha256}
                if package_sha256
                else {}
            ),
            **(
                {"runtime_manifest": manifest_authority}
                if manifest_authority is not None
                else {}
            ),
            "runtime_profile": (
                runtime_profile or _script_runtime_profile(path)
            ),
            "language_runtime": _script_runtime_profile(path),
            **(
                {
                    "runtime_requirements": list(
                        profile_record.get("runtime_requirements") or []
                    ),
                    "runtime_commands": list(
                        profile_record.get("runtime_commands") or []
                    ),
                    "runtime_node_packages": list(
                        profile_record.get(
                            "runtime_node_packages"
                        ) or []
                    ),
                    "reachable_sources": list(
                        profile_record.get("reachable_sources") or []
                    ),
                    "required_cwd": required_cwd,
                }
                if isinstance(profile_record, dict) else {}
            ),
            "authority_chain": authority_chain,
            "sandbox_egress_url_prefixes": sandbox_egress_url_prefixes,
            "sandbox_egress_rules": (
                candidate_sandbox_egress_rule_rows
            ),
            "invocation_bound_user_url_egress": bool(
                isinstance(profile_record, dict)
                and profile_record.get("user_url_egress_available")
                and profile_record.get("user_url_egress_bindings")
            ),
            "description": (
                "Content-addressed script explicitly referenced by an exact "
                "authorized instruction document; choose one listed runner at "
                "invocation time. Direct network dialing is disabled; the "
                "selected script receives only the exact HTTP methods and URL "
                "prefixes compiled into this candidate through the signed "
                "policy proxy. Persistent execution remains "
                "bound to the same runtime-owned policy and opaque lease; this "
                "candidate grants no ambient network authority."
            ),
        })

    if "run_declared_command" in available:
        for grant in command_grants:
            if not isinstance(grant, dict):
                continue
            command_id = str(grant.get("id") or "")
            executable = str(grant.get("executable") or "")
            prefix = grant.get("argv_prefix")
            if (
                not command_id.startswith("command-")
                or not executable
                or not isinstance(prefix, list)
                or not all(isinstance(item, str) for item in prefix)
            ):
                continue
            candidates.append({
                "id": _stable_id("command", f"{skill_name}\0{command_id}"),
                "kind": "declared_command",
                "skill_name": skill_name,
                "command_id": command_id,
                "tool_name": "run_declared_command",
                "executable": executable,
                "fixed_argv": list(prefix),
                "additional_argv": True,
                "shell": False,
                "sandbox_egress_url_prefixes": (
                    sandbox_egress_url_prefixes
                ),
                "sandbox_egress_rules": sandbox_egress_rule_rows,
                "description": (
                    "Backend-compiled exact argv template. The executable and "
                    "fixed argv cannot be model-authored; direct networking is "
                    "disabled and only the exact compiled HTTP-method and URL-"
                    "prefix rules are available through the signed policy proxy."
                ),
            })

    for http_tool, id_kind, method, granted_prefixes in (
        ("skill_http_get", "http", "GET", http_prefix_rows),
        (
            "skill_http_post_json", "http-post-json", "POST JSON",
            http_post_prefix_rows,
        ),
    ):
        if http_tool not in available:
            continue
        for granted_skill, prefix in granted_prefixes:
            if granted_skill != skill_name or not isinstance(prefix, str):
                continue
            candidates.append({
                "id": _stable_id(id_kind, f"{skill_name}\0{prefix}"),
                "kind": "skill_http_prefix",
                "skill_name": skill_name,
                "url_prefix": prefix,
                "tool_name": http_tool,
                "http_method": method,
                "description": (
                    f"Exact credential-free HTTPS {method} prefix compiled "
                    "by backend policy."
                ),
            })

    exact_mcp = set(str(item) for item in exact_mcp_names if str(item))
    for name in ordered_tools:
        if name.startswith("mcp_") and name in exact_mcp:
            candidates.append({
                "id": _stable_id("mcp", name),
                "kind": "mcp_tool",
                "tool_name": name,
                "description": "Exact MCP tool declared by the selected package.",
            })

    candidates = _dedupe_candidates(candidates)
    native_candidate_ids_by_tool = {
        str(candidate.get("tool_name") or ""): str(candidate.get("id") or "")
        for candidate in candidates
        if (
            isinstance(candidate, dict)
            and candidate.get("kind") == "native_tool"
            and candidate.get("tool_name")
            and candidate.get("id")
        )
    }
    required_candidate_groups: list[list[str]] = []
    for raw_group in required_native_tool_groups:
        if not isinstance(raw_group, (list, tuple, set, frozenset)):
            continue
        group = list(dict.fromkeys(
            native_candidate_ids_by_tool.get(str(tool_name), "")
            for tool_name in raw_group
            if native_candidate_ids_by_tool.get(str(tool_name), "")
        ))
        if group and group not in required_candidate_groups:
            required_candidate_groups.append(group)
    authority_projection = [
        {
            "resource_path": document["resource_path"],
            "sha256": document["sha256"],
        }
        for document in root_linked_documents
    ]
    (
        runtime_instruction_documents,
        instruction_catalog,
        instruction_plan_catalog,
    ) = (
        _validated_workflow_instruction_documents(
            instruction_documents,
            body_sha256=body_sha256,
            authority_documents=authority_projection,
            workflow_ir_required=workflow_ir_required,
        )
    )
    workflow_projection = (
        {
            "workflow_ir_required": workflow_ir_required,
            "instruction_catalog_sha256": instruction_catalog["catalog_sha256"],
            "workflow_plan_catalog_sha256": hashlib.sha256(json.dumps(
                instruction_plan_catalog,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            "instruction_documents": [
                document.binding_dict() for document in runtime_instruction_documents
            ],
        }
        if instruction_catalog is not None
        else None
    )
    catalog_sha256 = hashlib.sha256(json.dumps(
        {
            "schema_version": CAPABILITY_PLAN_SCHEMA_VERSION,
            "skill_name": skill_name,
            "body_sha256": body_sha256,
            "authority_documents": authority_projection,
            "candidates": candidates,
            "required_candidate_groups": required_candidate_groups,
            **(
                {"workflow_ir": workflow_projection}
                if workflow_projection is not None else {}
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    catalog = {
        "schema_version": CAPABILITY_PLAN_SCHEMA_VERSION,
        "skill_name": skill_name,
        "body_sha256": body_sha256,
        "catalog_sha256": catalog_sha256,
        "catalog_revision": (
            len(authority_projection)
            + (1 if instruction_catalog is not None else 0)
        ),
        "authority_documents": authority_projection,
        "body_chars": int(loaded_package.get("skill_md_chars") or len(body)),
        "candidates": candidates,
        "candidate_count": len(candidates),
        "required_candidate_groups": required_candidate_groups,
        "planning_tool": CAPABILITY_PLAN_TOOL_NAME,
        "policy": {
            "selection_only": True,
            "unknown_ids_rejected": True,
            "selection_lifetime": "active_standard_skill_run",
            "selected_capabilities_reusable": True,
            "required_semantics": "minimum_exact_dispatch_receipt",
            "optional_semantics": "authorized_without_receipt_requirement",
            "execution_remains_bounded": True,
            "shell": False,
            "scripts_require_exact_path_and_sha256": True,
            "persistent_process_requires_exact_package_sha256": True,
            "amendments_require_exact_catalog_sha256": True,
            "authority_documents_require_exact_sha256": True,
            "unreferenced_package_scripts_excluded": True,
            "script_executor_network": (
                "direct_disabled_exact_origin_allowlist"
            ),
            "persistent_process_runtime": "backend_profile_and_lease_owned",
            "max_optional_selections": MAX_OPTIONAL_SELECTIONS,
            "max_total_selections": MAX_TOTAL_SELECTIONS,
            "max_native_candidates": MAX_NATIVE_CAPABILITY_CANDIDATES,
        },
    }
    if instruction_catalog is not None:
        # Runtime-owned immutable objects are intentionally omitted by
        # catalog_prompt_payload.  The exact bounded projection below is the
        # only model-visible form.
        catalog.update(
            {
                "instruction_documents": runtime_instruction_documents,
                "instruction_catalog": instruction_catalog,
                "instruction_plan_catalog": instruction_plan_catalog,
                "workflow_ir_required": workflow_ir_required,
            }
        )
        catalog["policy"].update(
            {
                "workflow_ir_content_addressed": True,
                "workflow_plan_catalog_content_addressed": True,
                "workflow_ir_unknown_or_omitted_units_rejected": True,
                "workflow_ir_selected_capabilities_only": True,
                "max_instruction_catalog_bytes": (
                    MAX_WORKFLOW_INSTRUCTION_CATALOG_BYTES
                ),
            }
        )
    return catalog


def catalog_prompt_payload(catalog: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded model-visible projection of one catalog."""

    runtime_instruction_catalog = catalog.get("instruction_catalog")
    instruction_catalog = catalog.get("instruction_plan_catalog")
    workflow_ir_available = (
        isinstance(runtime_instruction_catalog, dict)
        and isinstance(instruction_catalog, dict)
    )
    workflow_ir_required = (
        workflow_ir_available and catalog.get("workflow_ir_required") is True
    )
    workflow_guidance = ""
    if workflow_ir_available:
        workflow_guidance = (
            " The runtime also issued a compact content-addressed instruction "
            "index. Submit workflow_plan, not the runtime-owned Workflow IR. "
            "For every node, use inclusive same-document ranges using the "
            "catalog's exact document_id and one-based start_ordinal/end_ordinal; "
            "those selectors are valid only for the exact outer catalog_sha256 "
            "copied into this submit_skill_capability_plan call. "
            "The runtime deterministically late-binds them to frozen opaque "
            "instruction IDs. Do not invent or submit instruction IDs. Coalesce "
            "adjacent units whenever practical; redundant adjacent or overlapping "
            "ranges in one document are normalized by the runtime. Declare only "
            "semantic child nodes, dependencies, selected capability IDs, and "
            "optional typed result schemas/output producers. The runtime expands "
            "bindings, coverage, execution policy, counts, receipts, and digest "
            "and rejects unknown, stale, reversed, cross-document, incomplete, "
            "cyclic, unselected, or non-lowerable plans."
        )
        if workflow_ir_required:
            workflow_guidance += (
                " workflow_plan is mandatory for this catalog; omitting it "
                "fails closed and no capability plan is installed. Every "
                "non-heading instruction unit must occur in at least one node "
                "range. The runtime injects the selected delegate_task candidate "
                "into every node; capability_ids should contain only additional "
                "selected candidates needed by that exact child."
            )
        else:
            workflow_guidance += (
                " workflow_plan is optional for this simple catalog; omit it "
                "unless the disclosed instructions require an explicit graph."
            )

    payload = {
        "schema_version": catalog.get("schema_version"),
        "skill_name": catalog.get("skill_name"),
        "body_sha256": catalog.get("body_sha256"),
        "catalog_sha256": catalog.get("catalog_sha256"),
        "catalog_revision": catalog.get("catalog_revision", 0),
        "authority_documents": list(catalog.get("authority_documents") or [])[
            :MAX_AUTHORITY_DOCUMENTS
        ],
        "body_chars": catalog.get("body_chars"),
        "candidates": list(catalog.get("candidates") or [])[:MAX_CAPABILITY_CANDIDATES],
        "required_candidate_groups": [
            list(group)
            for group in (catalog.get("required_candidate_groups") or [])[
                :MAX_CAPABILITY_CANDIDATES
            ]
            if isinstance(group, list)
        ],
        "unavailable_capabilities": list(catalog.get("unavailable_capabilities") or [])[
            :MAX_UNSUPPORTED_ITEMS
        ],
        "instructions": (
            "After reading every SKILL.md page, submit one complete "
            "submit_skill_capability_plan call for the current attempt. If the "
            "runtime returns a typed validation code/path, remain on this planning "
            "frontier and correct that exact finding; do not proceed to execution. "
            "Put capability IDs needed to satisfy mandatory instructions in "
            "required, discretionary/supporting IDs in optional, and describe any "
            "instruction that no candidate can support in unsupported. Selected "
            "entries remain reusable during this bounded Skill run: required means "
            "at least one exact dispatch receipt is needed before finishing, not "
            "that the capability may be called only once; optional is authorized "
            "without a minimum receipt. Every required_candidate_groups entry is a "
            "runtime-owned user requirement: put at least one ID from each group in "
            "required (optional does not satisfy it). Reuse a selected capability when the task "
            "needs multiple files, queries, pages, or other distinct operations, "
            "then stop and synthesize when the Skill is complete. Never invent "
            "an ID, executable, argv, script path, digest, URL prefix, or MCP name. "
            "When catalog_revision is non-zero, copy its exact catalog_sha256 "
            "into the plan; this binds the content-addressed amendment learned "
            "from completely read references. "
            "Directions to solve or bypass CAPTCHA, authentication/authorization or "
            "access controls, rate limits, anti-bot mechanisms, consequential-action "
            "confirmation, or to conceal automation identity/fingerprints are outside "
            "this planner's authority: record them as unsupported and do not select a "
            "capability for them. Safe ordinary navigation and exact content-addressed "
            "scripts remain eligible for the user's legitimate task. Direct network "
            "dialing is disabled inside the single session sandbox; an exact Skill "
            "entrypoint receives only its frozen, runtime-compiled HTTP-method and "
            "URL-prefix rules through the signed policy proxy and "
            "cannot replace an independently required browser or remote-network "
            "capability. A listed persistent-process runner remains limited to its "
            "backend-owned runtime/egress policy and opaque lease."
            + workflow_guidance
        ),
    }
    if workflow_ir_available:
        payload.update(
            {
                "workflow_ir_required": workflow_ir_required,
                "workflow_plan_catalog": instruction_catalog,
            }
        )
    return payload


def _error(code: str, message: str, **extra: Any) -> CapabilityPlanResult:
    return CapabilityPlanResult(False, {
        "status": "error",
        "error_code": code,
        "error": message,
        **extra,
    })


def _actionable_workflow_plan_correction(
    exc: WorkflowIRValidationError,
    documents: tuple[InstructionDocument, ...],
    planning_catalog: dict[str, Any] | None,
) -> dict[str, Any]:
    """Translate a runtime-only instruction ID into model-writable coordinates.

    Provider-facing compact plans deliberately cannot submit opaque ``iu-*``
    identifiers.  Returning only such an identifier after validation therefore
    creates an impossible correction loop.  Preserve the internal path for
    diagnostics, but pair it with the exact content-addressed document handle,
    ordinal, and already-disclosed preview accepted by the public schema.
    """

    match = re.search(r"(?:^|\.)coverage\.(iu-[0-9a-f]{24})(?:$|\.)", exc.path)
    if match is None or not isinstance(planning_catalog, dict):
        return {}
    instruction_id = match.group(1)
    catalog_documents = planning_catalog.get("documents")
    if not isinstance(catalog_documents, list):
        return {}
    for document_index, document in enumerate(documents):
        if document_index >= len(catalog_documents):
            return {}
        public_document = catalog_documents[document_index]
        if not isinstance(public_document, dict):
            return {}
        public_units = public_document.get("units")
        if not isinstance(public_units, list):
            return {}
        for unit_index, unit in enumerate(document.units):
            if unit.id != instruction_id or unit_index >= len(public_units):
                continue
            public_unit = public_units[unit_index]
            if not isinstance(public_unit, dict):
                return {}
            document_id = str(public_document.get("document_id") or "")
            ordinal = unit_index + 1
            preview = str(public_unit.get("preview") or "")[:240]
            return {
                "workflow_plan_internal_error_path": exc.path,
                "workflow_plan_error_path": (
                    "$.workflow_plan.nodes[*].instruction_ranges"
                    f"[document_id={document_id},ordinal={ordinal}]"
                ),
                "workflow_plan_correction": {
                    "action": "include_instruction_in_node_range",
                    "document_id": document_id,
                    "start_ordinal": ordinal,
                    "end_ordinal": ordinal,
                    "source_path": document.source_path,
                    "kind": unit.kind,
                    "start_line": unit.start_line,
                    "end_line": unit.end_line,
                    "preview": preview,
                    "instruction": (
                        "Include this ordinal in at least one semantic node's "
                        "instruction_ranges, usually by extending an adjacent "
                        "same-document range. Do not submit the internal iu-* ID."
                    ),
                },
            }
    return {}


def _candidate_tool_names(candidate: dict[str, Any]) -> list[str]:
    names: list[str] = []
    tool_name = candidate.get("tool_name")
    if isinstance(tool_name, str) and tool_name:
        names.append(tool_name)
    raw_names = candidate.get("tool_names")
    if isinstance(raw_names, list):
        names.extend(
            str(name)
            for name in raw_names
            if isinstance(name, str) and name
        )
    return list(dict.fromkeys(names))


def _bind_worker_plan_capabilities(
    worker_plan: dict[str, Any],
    *,
    candidates: dict[str, dict[str, Any]],
    skill_name: str,
) -> dict[str, Any]:
    """Project exact candidate IDs into the existing child-task contract.

    ``delegate_task`` is a parent controller bridge, not a recursive child
    capability. Other candidates become an explicit required child tool or
    an exact local resource preload. The child still receives only grants
    already present in the accepted parent capability plan.
    """

    enriched = json.loads(json.dumps(worker_plan))

    def enrich(step: dict[str, Any]) -> None:
        candidate_ids = [
            str(identifier)
            for identifier in step.get("capability_candidate_ids") or []
            if isinstance(identifier, str) and identifier
        ]
        child_tools: list[str] = []
        local_resources = [
            str(path)
            for path in step.get("local_resources") or []
            if isinstance(path, str) and path
        ]
        local_grant_needed = False
        bindings: list[dict[str, Any]] = []
        for identifier in candidate_ids:
            candidate = candidates[identifier]
            kind = str(candidate.get("kind") or "")
            tool_names = _candidate_tool_names(candidate)
            child_tools.extend(
                name for name in tool_names if name != "delegate_task"
            )
            resource_path = candidate.get("resource_path")
            if kind == "skill_resource" and isinstance(resource_path, str):
                local_resources.append(resource_path)
            if kind in {
                "skill_script",
                "declared_command",
                "skill_http_prefix",
            }:
                local_grant_needed = True
            binding: dict[str, Any] = {
                "candidate_id": identifier,
                "kind": kind,
                "tool_names": tool_names,
            }
            # Retain the exact backend-issued authority coordinates needed to
            # narrow one child.  Tool names alone are not sufficient: several
            # scripts, commands, resources, or HTTP prefixes intentionally
            # share one bridge name inside a selected Skill.
            for field in (
                "skill_name",
                "skill_md_sha256",
                "resource_path",
                "sha256",
                "package_sha256",
                "tool_name",
                "command_id",
                "executable",
                "url_prefix",
                "http_method",
                "runtime_profile",
                "required_cwd",
                "schema_sha256",
                "descriptor_sha256",
            ):
                value = candidate.get(field)
                if isinstance(value, str) and value:
                    binding[field] = value
            fixed_argv = candidate.get("fixed_argv")
            if isinstance(fixed_argv, list) and all(
                isinstance(value, str) for value in fixed_argv
            ):
                binding["fixed_argv"] = list(fixed_argv)
            if isinstance(candidate.get("additional_argv"), bool):
                binding["additional_argv"] = candidate["additional_argv"]
            sandbox_egress = candidate.get(
                "sandbox_egress_url_prefixes"
            )
            if (
                isinstance(sandbox_egress, list)
                and all(
                    isinstance(prefix, str) and prefix
                    for prefix in sandbox_egress
                )
            ):
                binding["sandbox_egress_url_prefixes"] = list(
                    sandbox_egress
                )
            sandbox_rules = candidate.get("sandbox_egress_rules")
            if isinstance(sandbox_rules, list):
                binding["sandbox_egress_rules"] = [
                    {
                        "methods": list(rule.get("methods") or []),
                        "url_prefix": str(rule.get("url_prefix") or ""),
                    }
                    for rule in sandbox_rules
                    if isinstance(rule, dict)
                ]
            browser_rules = candidate.get("browser_egress_rules")
            if isinstance(browser_rules, list):
                binding["browser_egress_rules"] = [
                    {
                        "methods": list(rule.get("methods") or []),
                        "url_prefix": str(rule.get("url_prefix") or ""),
                    }
                    for rule in browser_rules
                    if isinstance(rule, dict)
                ]
            bindings.append(binding)
        step["tools"] = [
            {"tool": name, "required": True}
            for name in dict.fromkeys(child_tools)
        ]
        step["local_resources"] = list(dict.fromkeys(local_resources))
        if local_grant_needed:
            step["skills"] = [skill_name]
        step["capability_bindings"] = bindings
        step["capability_bindings_sha256"] = hashlib.sha256(
            json.dumps(
                bindings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    workers = enriched.get("workers")
    if isinstance(workers, dict):
        for step in workers.values():
            if isinstance(step, dict):
                enrich(step)
    for step in enriched.get("aggregation_steps") or []:
        if isinstance(step, dict):
            enrich(step)
    enriched["available_aggregation_steps"] = list(
        enriched.get("aggregation_steps") or []
    )
    return enriched


def validate_capability_plan(
    catalog: dict[str, Any] | None,
    *,
    skill_name: Any,
    body_sha256: Any,
    required: Any,
    optional: Any,
    unsupported: Any,
    catalog_sha256: Any = None,
    workflow_ir: Any = None,
    workflow_plan: Any = None,
) -> CapabilityPlanResult:
    """Validate a model selection and derive its exact effective closure."""

    # See the corresponding deferred import in ``build_capability_catalog``.
    from tools.session_sandbox_policy import (
        SessionSandboxPolicyError,
        normalize_http_url_prefix,
        normalize_session_sandbox_methods,
    )

    if not isinstance(catalog, dict):
        return _error(
            "capability_catalog_unavailable",
            "No runtime-owned capability catalog is active for this call.",
        )
    expected_skill = str(catalog.get("skill_name") or "")
    expected_digest = str(catalog.get("body_sha256") or "")
    if skill_name != expected_skill or body_sha256 != expected_digest:
        return _error(
            "capability_plan_identity_mismatch",
            "skill_name/body_sha256 must exactly match the current disclosed SKILL.md.",
            expected_skill_name=expected_skill,
            expected_body_sha256=expected_digest,
        )
    expected_catalog_digest = str(catalog.get("catalog_sha256") or "")
    catalog_revision = int(catalog.get("catalog_revision") or 0)
    if (
        (catalog_revision > 0 or catalog_sha256 is not None)
        and catalog_sha256 != expected_catalog_digest
    ):
        return _error(
            "capability_plan_catalog_identity_mismatch",
            "catalog_sha256 must exactly match the current content-addressed amendment.",
            expected_catalog_sha256=expected_catalog_digest,
            catalog_revision=catalog_revision,
        )
    if not isinstance(required, list) or not isinstance(optional, list):
        return _error(
            "capability_plan_invalid_selection",
            "required and optional must be JSON arrays of catalog IDs.",
        )
    if (
        len(required) > MAX_PLAN_SELECTIONS
        or len(optional) > MAX_PLAN_SELECTIONS
        or not all(isinstance(item, str) and item for item in [*required, *optional])
    ):
        return _error(
            "capability_plan_selection_limit",
            f"Each selection list is limited to {MAX_PLAN_SELECTIONS} non-empty IDs.",
        )
    if (
        len(optional) > MAX_OPTIONAL_SELECTIONS
        or len(required) + len(optional) > MAX_TOTAL_SELECTIONS
    ):
        return _error(
            "capability_plan_selection_limit",
            "The bounded plan permits at most "
            f"{MAX_OPTIONAL_SELECTIONS} optional and {MAX_TOTAL_SELECTIONS} "
            "total capability selections.",
        )
    if not isinstance(unsupported, list) or len(unsupported) > MAX_UNSUPPORTED_ITEMS:
        return _error(
            "capability_plan_invalid_unsupported",
            f"unsupported must contain at most {MAX_UNSUPPORTED_ITEMS} typed items.",
        )
    clean_unsupported: list[dict[str, str]] = []
    for item in unsupported:
        if not isinstance(item, dict):
            return _error(
                "capability_plan_invalid_unsupported",
                "Every unsupported item must contain instruction and reason strings.",
            )
        instruction = item.get("instruction")
        reason = item.get("reason")
        if (
            not isinstance(instruction, str)
            or not instruction.strip()
            or len(instruction) > 500
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 1000
        ):
            return _error(
                "capability_plan_invalid_unsupported",
                "Unsupported instruction/reason must be non-empty bounded strings.",
            )
        clean_unsupported.append({
            "instruction": instruction.strip(),
            "reason": reason.strip(),
        })

    all_ids = [*required, *optional]
    if len(set(all_ids)) != len(all_ids):
        return _error(
            "capability_plan_duplicate_selection",
            "A capability ID may appear exactly once across required and optional.",
        )
    candidates = {
        str(item.get("id")): item
        for item in catalog.get("candidates") or []
        if isinstance(item, dict) and item.get("id")
    }
    unknown = [identifier for identifier in all_ids if identifier not in candidates]
    if unknown:
        return _error(
            "capability_plan_unknown_id",
            "The plan contains capability IDs that were not issued by the backend.",
            unknown_ids=unknown[:32],
        )
    required_candidate_groups: list[list[str]] = []
    for raw_group in catalog.get("required_candidate_groups") or []:
        if (
            not isinstance(raw_group, list)
            or not raw_group
            or not all(
                isinstance(identifier, str)
                and identifier in candidates
                for identifier in raw_group
            )
        ):
            return _error(
                "capability_catalog_required_group_invalid",
                "The runtime-owned required capability groups are malformed.",
            )
        canonical_group = list(dict.fromkeys(raw_group))
        if canonical_group not in required_candidate_groups:
            required_candidate_groups.append(canonical_group)
    missing_required_groups = [
        group
        for group in required_candidate_groups
        if not set(group).intersection(required)
    ]
    if missing_required_groups:
        return _error(
            "capability_plan_required_group_omitted",
            "At least one capability ID from every runtime-owned user "
            "requirement group must be selected as required.",
            missing_required_candidate_groups=missing_required_groups[:32],
        )

    workflow_flag = catalog.get("workflow_ir_required", False)
    if not isinstance(workflow_flag, bool):
        return _error(
            "capability_catalog_workflow_ir_invalid",
            "The runtime-owned workflow_ir_required flag is malformed.",
        )
    raw_instruction_documents = catalog.get("instruction_documents")
    if raw_instruction_documents is None:
        runtime_instruction_documents: tuple[InstructionDocument, ...] = ()
    elif not isinstance(raw_instruction_documents, (list, tuple)) or not all(
        isinstance(document, InstructionDocument)
        for document in raw_instruction_documents
    ):
        return _error(
            "capability_catalog_workflow_ir_invalid",
            "The runtime-owned instruction documents are malformed.",
        )
    else:
        runtime_instruction_documents = tuple(raw_instruction_documents)

    if workflow_flag and not runtime_instruction_documents:
        return _error(
            "capability_catalog_workflow_ir_invalid",
            "workflow_ir_required has no runtime-owned instruction documents.",
        )
    if runtime_instruction_documents:
        try:
            current_instruction_catalog = instruction_catalog_payload(
                runtime_instruction_documents
            )
            current_instruction_plan_catalog = (
                workflow_plan_instruction_catalog_payload(
                    runtime_instruction_documents
                )
            )
        except WorkflowIRValidationError as exc:
            return _error(
                "capability_catalog_workflow_ir_invalid",
                "The runtime-owned instruction catalog is invalid.",
                workflow_ir_error_code=exc.code,
                workflow_ir_error_path=exc.path,
            )
        if len(json.dumps(
            current_instruction_plan_catalog,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")) > MAX_WORKFLOW_INSTRUCTION_CATALOG_BYTES:
            return _error(
                "capability_catalog_workflow_ir_invalid",
                "The runtime-owned instruction catalog exceeds its bounded "
                "model-visible size.",
            )
        if catalog.get("instruction_catalog") != current_instruction_catalog:
            return _error(
                "capability_catalog_workflow_ir_invalid",
                "The model-visible instruction catalog no longer matches "
                "runtime-owned instruction documents.",
            )
        if (
            catalog.get("instruction_plan_catalog")
            != current_instruction_plan_catalog
        ):
            return _error(
                "capability_catalog_workflow_ir_invalid",
                "The compact planning index no longer matches runtime-owned "
                "instruction documents.",
            )
    elif workflow_ir is not None or workflow_plan is not None:
        return _error(
            "capability_plan_workflow_ir_not_authorized",
            "This simple catalog did not issue instruction-document authority "
            "for a model-authored workflow plan.",
        )

    if workflow_ir is not None and workflow_plan is not None:
        return _error(
            "capability_plan_workflow_payload_conflict",
            "Submit workflow_plan or legacy workflow_ir, never both.",
        )
    workflow_payload = workflow_plan if workflow_plan is not None else workflow_ir
    if workflow_flag and workflow_payload is None:
        return _error(
            "capability_plan_workflow_ir_required",
            "This content-addressed instruction catalog requires workflow_plan.",
            instruction_catalog_sha256=(
                (catalog.get("instruction_catalog") or {}).get("catalog_sha256")
            ),
        )

    validated_workflow_ir = None
    worker_plan: dict[str, Any] | None = None
    workflow_instruction_resource_bindings: list[dict[str, str]] = []
    if workflow_payload is not None:
        if not runtime_instruction_documents:
            return _error(
                "capability_plan_workflow_ir_not_authorized",
                "No runtime-owned instruction documents authorize a workflow plan.",
            )
        if not isinstance(workflow_payload, dict):
            return _error(
                "capability_plan_workflow_ir_invalid",
                "workflow plan must be one complete JSON object.",
            )
        delegate_candidate_ids = {
            identifier
            for identifier, candidate in candidates.items()
            if (
                candidate.get("kind") == "native_tool"
                and candidate.get("tool_name") == "delegate_task"
            )
        }
        try:
            if workflow_plan is not None:
                validated_workflow_ir = compile_workflow_plan(
                    workflow_payload,
                    documents=runtime_instruction_documents,
                    skill_name=expected_skill,
                    capability_catalog_sha256=expected_catalog_digest,
                    # Passing only exact current selections prevents the
                    # semantic plan from widening the capability boundary.
                    available_capability_ids=all_ids,
                    mandatory_node_capability_ids=(
                        delegate_candidate_ids.intersection(required)
                    ),
                    strict_instruction_execution=workflow_flag,
                )
            else:
                validated_workflow_ir = validate_workflow_ir(
                    workflow_payload,
                    documents=runtime_instruction_documents,
                    skill_name=expected_skill,
                    capability_catalog_sha256=expected_catalog_digest,
                    available_capability_ids=all_ids,
                    strict_instruction_execution=workflow_flag,
                )
        except WorkflowIRValidationError as exc:
            error_code = (
                "capability_plan_workflow_ir_unselected_capability"
                if exc.code == "unknown_capability_id"
                else "capability_plan_workflow_ir_invalid"
            )
            actionable_correction = (
                _actionable_workflow_plan_correction(
                    exc,
                    runtime_instruction_documents,
                    current_instruction_plan_catalog,
                )
                if workflow_plan is not None
                else {}
            )
            return _error(
                error_code,
                (
                    "The submitted compact workflow plan failed deterministic "
                    "compilation."
                    + (
                        " Apply the exact document/ordinal correction returned "
                        "in workflow_plan_correction; the internal instruction "
                        "ID is not a provider-writable selector."
                        if actionable_correction else ""
                    )
                    if workflow_plan is not None
                    else "The submitted Workflow IR failed deterministic validation."
                ),
                workflow_ir_error_code=exc.code,
                workflow_ir_error_path=exc.path,
                workflow_ir_error=str(exc)[:1_000],
                **(
                    {
                        "workflow_plan_error_code": exc.code,
                        "workflow_plan_error_path": (
                            actionable_correction.get(
                                "workflow_plan_error_path"
                            ) or exc.path
                        ),
                        **actionable_correction,
                    }
                    if workflow_plan is not None
                    else {}
                ),
            )
        if workflow_flag and not delegate_candidate_ids.intersection(required):
            return _error(
                "capability_plan_workflow_delegate_not_required",
                "Mandatory Workflow IR requires the backend-issued "
                "delegate_task candidate in the required selection.",
            )
        for node in validated_workflow_ir.nodes:
            if (
                node.required
                and not delegate_candidate_ids.intersection(
                    node.capability_ids
                )
            ):
                return _error(
                    "capability_plan_workflow_node_not_delegated",
                    "Every required child-agent Workflow IR node must bind the "
                    "backend-issued delegate_task candidate.",
                    workflow_ir_node_id=node.id,
                )
        authorized_instruction_digests = {
            "SKILL.md": expected_digest,
            **{
                str(document.get("resource_path") or ""): str(
                    document.get("sha256") or ""
                )
                for document in catalog.get("authority_documents") or []
                if isinstance(document, dict)
            },
        }
        instruction_units_by_id = {
            unit.id: unit
            for unit in validated_workflow_ir.instruction_units
        }
        seen_instruction_sources: set[str] = set()
        for node in validated_workflow_ir.nodes:
            if not node.required:
                continue
            for instruction_id in node.instruction_ids:
                unit = instruction_units_by_id[instruction_id]
                if (
                    authorized_instruction_digests.get(unit.source_path)
                    != unit.source_sha256
                ):
                    return _error(
                        "capability_plan_workflow_instruction_source_unauthorized",
                        "A required Workflow IR node instruction source is "
                        "outside the parent-frozen content-addressed authority "
                        "closure.",
                        workflow_ir_node_id=node.id,
                        workflow_ir_instruction_id=instruction_id,
                        workflow_ir_instruction_source=unit.source_path,
                    )
                if unit.source_path not in seen_instruction_sources:
                    seen_instruction_sources.add(unit.source_path)
                    workflow_instruction_resource_bindings.append({
                        "resource_path": unit.source_path,
                        "sha256": unit.source_sha256,
                    })
        try:
            worker_plan = _bind_worker_plan_capabilities(
                compile_worker_wave_plan(validated_workflow_ir),
                candidates=candidates,
                skill_name=expected_skill,
            )
        except WorkflowPlanAdapterError as exc:
            return _error(
                "capability_plan_workflow_ir_not_lowerable",
                "The validated Workflow IR cannot be represented by the "
                "bounded worker/wave runtime.",
                workflow_ir_error_code=exc.code,
                workflow_ir_node_id=exc.node_id,
                workflow_ir_error=str(exc)[:1_000],
            )
        if workflow_flag and not (
            worker_plan.get("required_workers")
            or worker_plan.get("aggregation_steps")
        ):
            return _error(
                "capability_plan_workflow_empty",
                "Mandatory Workflow IR lowered to no required execution nodes.",
            )

    selected = [candidates[identifier] for identifier in all_ids]
    required_candidates = [candidates[identifier] for identifier in required]
    tools: list[str] = ["skill_view"]
    resources: list[tuple[str, str]] = [
        (expected_skill, "SKILL.md"),
        *[
            (
                expected_skill,
                str(binding.get("resource_path") or ""),
            )
            for binding in workflow_instruction_resource_bindings
        ],
    ]
    scripts: list[tuple[str, str, str]] = []
    process_only_scripts: list[tuple[str, str, str]] = []
    script_authorities: list[
        tuple[str, str, str, str, str, str]
    ] = []
    package_digests: list[tuple[str, str]] = []
    commands: list[tuple[str, str, str, tuple[str, ...]]] = []
    http_prefixes: list[tuple[str, str]] = []
    http_post_prefixes: list[tuple[str, str]] = []
    sandbox_egress_prefixes: list[tuple[str, str]] = []
    sandbox_egress_rules: list[
        tuple[str, str, tuple[str, ...]]
    ] = []
    browser_egress_rules: list[tuple[str, tuple[str, ...]]] = []
    required_groups: list[tuple[str, ...]] = []

    for index, candidate in enumerate(selected):
        kind = candidate.get("kind")
        candidate_tools: list[str] = []
        if kind in {"native_tool", "mcp_tool"}:
            candidate_tools = [str(candidate.get("tool_name") or "")]
            raw_browser_rules = candidate.get("browser_egress_rules")
            if raw_browser_rules is not None:
                if (
                    kind != "native_tool"
                    or candidate_tools != ["browser_navigate"]
                    or not isinstance(raw_browser_rules, list)
                    or len(raw_browser_rules) > MAX_SANDBOX_EGRESS_PREFIXES
                ):
                    return _error(
                        "capability_catalog_browser_egress_rules_invalid",
                        "The backend-issued native-browser egress rules are malformed.",
                    )
                for rule in raw_browser_rules:
                    if (
                        not isinstance(rule, dict)
                        or set(rule) != {"methods", "url_prefix"}
                        or not isinstance(rule.get("url_prefix"), str)
                    ):
                        return _error(
                            "capability_catalog_browser_egress_rules_invalid",
                            "The backend-issued native-browser egress rules are malformed.",
                        )
                    try:
                        prefix = normalize_http_url_prefix(
                            rule["url_prefix"]
                        )
                        methods = normalize_session_sandbox_methods(
                            rule.get("methods")
                        )
                    except SessionSandboxPolicyError:
                        return _error(
                            "capability_catalog_browser_egress_rules_invalid",
                            "The backend-issued native-browser egress rules are malformed.",
                        )
                    if (
                        prefix != rule["url_prefix"]
                        or list(methods) != rule.get("methods")
                    ):
                        return _error(
                            "capability_catalog_browser_egress_rules_invalid",
                            "The backend-issued native-browser egress rules are noncanonical.",
                        )
                    browser_egress_rules.append((prefix, methods))
                package_sha256 = str(
                    candidate.get("package_sha256") or ""
                )
                if _valid_sha256(package_sha256):
                    package_digests.append((
                        expected_skill,
                        package_sha256,
                    ))
        elif kind == "skill_resource":
            path = str(candidate.get("resource_path") or "")
            resources.append((expected_skill, path))
            candidate_tools = ["skill_view"]
        elif kind == "skill_script":
            path = str(candidate.get("resource_path") or "")
            digest = str(candidate.get("sha256") or "")
            resources.append((expected_skill, path))
            scripts.append((expected_skill, path, digest))
            if (
                candidate.get("runtime_profile")
                == "browser-automation-v1"
                or candidate.get("required_cwd") in {"script", "skill"}
            ):
                process_only_scripts.append(
                    (expected_skill, path, digest)
                )
            chain = candidate.get("authority_chain")
            if isinstance(chain, list) and chain:
                root_row = chain[0] if isinstance(chain[0], dict) else {}
                declaring_row = (
                    chain[-1] if isinstance(chain[-1], dict) else {}
                )
                root_digest = str(root_row.get("sha256") or "")
                declaring_path = str(
                    declaring_row.get("resource_path") or ""
                )
                declaring_digest = str(
                    declaring_row.get("sha256") or ""
                )
                if (
                    _valid_sha256(root_digest)
                    and _safe_relative_path(declaring_path) is not None
                    and _valid_sha256(declaring_digest)
                ):
                    resources.append((expected_skill, declaring_path))
                    script_authorities.append((
                        expected_skill,
                        root_digest,
                        declaring_path,
                        declaring_digest,
                        path,
                        digest,
                    ))
            candidate_tools = [
                str(name) for name in candidate.get("tool_names") or [] if str(name)
            ]
            package_sha256 = str(candidate.get("package_sha256") or "")
            if "run_skill_process" in candidate_tools and not _valid_sha256(
                package_sha256
            ):
                return _error(
                    "capability_plan_invalid_package_authority",
                    "Persistent Skill execution requires a backend-issued "
                    "complete package digest.",
                )
            if _valid_sha256(package_sha256):
                package_digests.append((expected_skill, package_sha256))
            script_egress_prefixes = candidate.get(
                "sandbox_egress_url_prefixes"
            )
            if (
                not isinstance(script_egress_prefixes, list)
                or len(script_egress_prefixes)
                > MAX_SANDBOX_EGRESS_PREFIXES
                or any(
                    not isinstance(prefix, str)
                    for prefix in script_egress_prefixes
                )
                or len(set(script_egress_prefixes))
                != len(script_egress_prefixes)
                or any(
                    canonical_https_prefix(prefix) != prefix
                    for prefix in script_egress_prefixes
                )
            ):
                return _error(
                    "capability_catalog_script_egress_invalid",
                    "The backend-issued script egress closure is malformed.",
                )
            sandbox_egress_prefixes.extend(
                (expected_skill, prefix)
                for prefix in script_egress_prefixes
            )
            raw_script_egress_rules = candidate.get(
                "sandbox_egress_rules"
            )
            if raw_script_egress_rules is None:
                raw_script_egress_rules = [
                    {
                        "methods": ["GET", "HEAD"],
                        "url_prefix": normalize_http_url_prefix(prefix),
                    }
                    for prefix in script_egress_prefixes
                ]
            if (
                not isinstance(raw_script_egress_rules, list)
                or len(raw_script_egress_rules)
                > MAX_SANDBOX_EGRESS_PREFIXES
            ):
                return _error(
                    "capability_catalog_script_egress_rules_invalid",
                    "The backend-issued script egress rules are malformed.",
                )
            for rule in raw_script_egress_rules:
                if (
                    not isinstance(rule, dict)
                    or set(rule) != {"methods", "url_prefix"}
                    or not isinstance(rule.get("url_prefix"), str)
                ):
                    return _error(
                        "capability_catalog_script_egress_rules_invalid",
                        "The backend-issued script egress rules are malformed.",
                    )
                try:
                    prefix = normalize_http_url_prefix(
                        rule["url_prefix"]
                    )
                    methods = normalize_session_sandbox_methods(
                        rule.get("methods")
                    )
                except SessionSandboxPolicyError:
                    return _error(
                        "capability_catalog_script_egress_rules_invalid",
                        "The backend-issued script egress rules are malformed.",
                    )
                if prefix != rule["url_prefix"]:
                    return _error(
                        "capability_catalog_script_egress_rules_invalid",
                        "The backend-issued script egress rules are malformed.",
                    )
                sandbox_egress_rules.append((
                    expected_skill,
                    prefix,
                    methods,
                ))
        elif kind == "declared_command":
            candidate_tools = ["run_declared_command"]
            commands.append((
                expected_skill,
                str(candidate.get("command_id") or ""),
                str(candidate.get("executable") or ""),
                tuple(str(item) for item in candidate.get("fixed_argv") or []),
            ))
            command_egress_prefixes = candidate.get(
                "sandbox_egress_url_prefixes"
            )
            if (
                not isinstance(command_egress_prefixes, list)
                or len(command_egress_prefixes)
                > MAX_SANDBOX_EGRESS_PREFIXES
                or any(
                    not isinstance(prefix, str)
                    for prefix in command_egress_prefixes
                )
                or len(set(command_egress_prefixes))
                != len(command_egress_prefixes)
                or any(
                    canonical_https_prefix(prefix) != prefix
                    for prefix in command_egress_prefixes
                )
            ):
                return _error(
                    "capability_catalog_command_egress_invalid",
                    "The backend-issued command egress closure is malformed.",
                )
            sandbox_egress_prefixes.extend(
                (expected_skill, prefix)
                for prefix in command_egress_prefixes
            )
            raw_command_egress_rules = candidate.get(
                "sandbox_egress_rules"
            )
            if raw_command_egress_rules is None:
                raw_command_egress_rules = [
                    {
                        "methods": ["GET", "HEAD"],
                        "url_prefix": normalize_http_url_prefix(prefix),
                    }
                    for prefix in command_egress_prefixes
                ]
            if (
                not isinstance(raw_command_egress_rules, list)
                or len(raw_command_egress_rules)
                > MAX_SANDBOX_EGRESS_PREFIXES
            ):
                return _error(
                    "capability_catalog_command_egress_rules_invalid",
                    "The backend-issued command egress rules are malformed.",
                )
            for rule in raw_command_egress_rules:
                if (
                    not isinstance(rule, dict)
                    or set(rule) != {"methods", "url_prefix"}
                    or not isinstance(rule.get("url_prefix"), str)
                ):
                    return _error(
                        "capability_catalog_command_egress_rules_invalid",
                        "The backend-issued command egress rules are malformed.",
                    )
                try:
                    prefix = normalize_http_url_prefix(
                        rule["url_prefix"]
                    )
                    methods = normalize_session_sandbox_methods(
                        rule.get("methods")
                    )
                except SessionSandboxPolicyError:
                    return _error(
                        "capability_catalog_command_egress_rules_invalid",
                        "The backend-issued command egress rules are malformed.",
                    )
                if prefix != rule["url_prefix"]:
                    return _error(
                        "capability_catalog_command_egress_rules_invalid",
                        "The backend-issued command egress rules are malformed.",
                    )
                sandbox_egress_rules.append((
                    expected_skill,
                    prefix,
                    methods,
                ))
        elif kind == "skill_http_prefix":
            candidate_tools = [str(candidate.get("tool_name") or "")]
            target_prefixes = (
                http_post_prefixes
                if candidate_tools == ["skill_http_post_json"]
                else http_prefixes
            )
            target_prefixes.append((
                expected_skill,
                str(candidate.get("url_prefix") or ""),
            ))
        tools.extend(name for name in candidate_tools if name)
        if index < len(required) and candidate_tools:
            required_groups.append(tuple(dict.fromkeys(candidate_tools)))

    normalized = {
        "status": "accepted",
        "schema_version": CAPABILITY_PLAN_SCHEMA_VERSION,
        "skill_name": expected_skill,
        "body_sha256": expected_digest,
        "catalog_sha256": expected_catalog_digest,
        "catalog_revision": catalog_revision,
        "required": list(required),
        "optional": list(optional),
        # Preserve the exact backend-issued candidate identities.  Tool-name
        # groups alone are insufficient receipts: two resources, scripts, or
        # commands can intentionally share one bridge without being
        # interchangeable.
        "required_candidates": [dict(item) for item in required_candidates],
        "required_candidate_groups": required_candidate_groups,
        "capability_semantics": {
            "selection_lifetime": "active_standard_skill_run",
            "selected_capabilities_reusable": True,
            "required": "minimum_exact_dispatch_receipt",
            "optional": "authorized_without_receipt_requirement",
        },
        "unsupported": clean_unsupported,
        "selected_tools": list(dict.fromkeys(tools)),
        "required_tool_groups": [list(group) for group in required_groups],
        "allowed_skill_resources": [list(item) for item in dict.fromkeys(resources)],
        "allowed_skill_scripts": [list(item) for item in dict.fromkeys(scripts)],
        "process_only_skill_scripts": [
            list(item)
            for item in dict.fromkeys(process_only_scripts)
        ],
        "allowed_skill_script_authorities": [
            list(item) for item in dict.fromkeys(script_authorities)
        ],
        "allowed_skill_package_digests": [
            list(item) for item in dict.fromkeys(package_digests)
        ],
        "allowed_skill_commands": [
            [skill, command_id, executable, list(prefix)]
            for skill, command_id, executable, prefix in dict.fromkeys(commands)
        ],
        "allowed_skill_http_prefixes": [
            list(item) for item in dict.fromkeys(http_prefixes)
        ],
        "allowed_skill_http_post_prefixes": [
            list(item) for item in dict.fromkeys(http_post_prefixes)
        ],
        "allowed_skill_sandbox_egress_prefixes": [
            list(item)
            for item in dict.fromkeys(sandbox_egress_prefixes)
        ],
        "allowed_skill_sandbox_egress_rules": [
            [skill, prefix, list(methods)]
            for skill, prefix, methods in dict.fromkeys(
                sandbox_egress_rules
            )
        ],
        "allowed_browser_egress_rules": [
            [prefix, list(methods)]
            for prefix, methods in dict.fromkeys(browser_egress_rules)
        ],
        "diagnostic": (
            "unsupported Skill instructions remain and must be reported explicitly"
            if clean_unsupported else "all classified instructions use backend-issued candidates"
        ),
    }
    if validated_workflow_ir is not None and worker_plan is not None:
        canonical_workflow_ir = validated_workflow_ir.to_dict()
        normalized.update(
            {
                "workflow_ir_required": workflow_flag,
                "workflow_ir": canonical_workflow_ir,
                "worker_plan": worker_plan,
                "workflow_instruction_resource_bindings": [
                    dict(binding)
                    for binding in workflow_instruction_resource_bindings
                ],
                "instruction_coverage": [
                    item.to_dict() for item in validated_workflow_ir.coverage
                ],
            }
        )
    return CapabilityPlanResult(True, normalized)


def script_call_has_semantic_task_binding(
    tool_name: str,
    args: dict[str, Any],
    artifacts: Iterable[dict[str, Any]] = (),
) -> bool:
    """Prove that a managed script invocation is bound to the actual task.

    Merely running a package demo or an argument-free ``main`` function does
    not establish an evidence receipt. A declared callable/method, non-empty
    data/CLI arguments, or a verified artifact does.
    """

    if any(isinstance(item, dict) for item in artifacts):
        return True
    count_fields = (
        "cli_arg_count", "function_arg_count", "function_kwarg_count",
        "constructor_arg_count", "constructor_kwarg_count",
        "method_arg_count", "method_kwarg_count",
    )
    has_arguments = any(
        isinstance(args.get(field), int)
        and not isinstance(args.get(field), bool)
        and args[field] > 0
        for field in count_fields
    )
    has_arguments = has_arguments or any(
        isinstance(args.get(field), (list, dict)) and bool(args.get(field))
        for field in (
            "args", "function_args", "function_kwargs", "constructor_args",
            "constructor_kwargs", "method_args", "method_kwargs",
        )
    )
    if tool_name == "run_skill_script":
        return has_arguments
    method_name = str(args.get("method_name") or "").strip()
    class_name = str(args.get("class_name") or "").strip()
    if method_name and class_name:
        return method_name.casefold() != "main" or has_arguments
    function_name = str(args.get("function_name") or "").strip()
    if function_name:
        return function_name.casefold() != "main" or has_arguments
    return has_arguments


def capability_call_targets_candidate(
    candidate: dict[str, Any],
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    allowed_skill_scripts: Iterable[tuple[str, str, str]] = (),
    allowed_skill_commands: Iterable[
        tuple[str, str, str, tuple[str, ...]]
    ] = (),
    allowed_skill_http_prefixes: Iterable[tuple[str, str]] = (),
    allowed_skill_http_post_prefixes: Iterable[tuple[str, str]] = (),
) -> bool:
    """Match a proposed call to one exact compiler-owned coordinate.

    This is the pre-dispatch counterpart of
    :func:`capability_call_satisfies_candidate`.  It deliberately proves only
    where a call is aimed; handler-owned result identities, EOF, artifacts,
    and evidence quality remain receipt-time facts.  Shared public bridges
    such as ``skill_http_get`` therefore cannot cross a mandatory phase
    boundary merely because their *name* appears on the active frontier.
    """

    if not isinstance(candidate, dict) or not isinstance(args, dict):
        return False
    kind = str(candidate.get("kind") or "")
    skill_name = str(candidate.get("skill_name") or "")

    if kind in {"native_tool", "mcp_tool"}:
        return tool_name == str(candidate.get("tool_name") or "")

    if kind == "skill_resource":
        requested_path = args.get("file_path")
        if requested_path in {None, ""}:
            requested_path = "SKILL.md"
        return (
            tool_name == "skill_view"
            and args.get("name") == skill_name
            and requested_path == str(candidate.get("resource_path") or "")
        )

    if kind == "skill_script":
        candidate_tools = {
            str(item)
            for item in candidate.get("tool_names") or []
            if str(item)
        }
        if tool_name not in candidate_tools:
            return False
        path = str(candidate.get("resource_path") or "")
        digest = str(candidate.get("sha256") or "")
        if (skill_name, path, digest) not in set(allowed_skill_scripts):
            return False
        if tool_name == "run_skill_process":
            # Start calls carry the exact script path. Later read/sync/close
            # calls carry only a handler-issued process ID; the executor lease
            # is the authoritative coordinate for those continuations.
            operation = str(args.get("operation") or "start")
            return operation != "start" or args.get(
                "script_path"
            ) == f"skills/{skill_name}/{path}"
        return args.get("script_path") == f"skills/{skill_name}/{path}"

    if kind == "declared_command":
        command_id = str(candidate.get("command_id") or "")
        exact_grant = (
            skill_name,
            command_id,
            str(candidate.get("executable") or ""),
            tuple(str(item) for item in candidate.get("fixed_argv") or []),
        )
        if exact_grant not in set(allowed_skill_commands):
            return False
        if (
            candidate.get("additional_argv") is False
            and args.get("argv") != []
        ):
            return False
        return (
            tool_name == "run_declared_command"
            and args.get("skill_name") == skill_name
            and args.get("command_id") == command_id
        )

    if kind == "skill_http_prefix":
        candidate_tool = str(candidate.get("tool_name") or "")
        if (
            candidate_tool not in {
                "skill_http_get", "skill_http_post_json",
            }
            or tool_name != candidate_tool
        ):
            return False
        prefix = str(candidate.get("url_prefix") or "")
        allowed_prefixes = (
            allowed_skill_http_post_prefixes
            if candidate_tool == "skill_http_post_json"
            else allowed_skill_http_prefixes
        )
        if (skill_name, prefix) not in set(allowed_prefixes):
            return False
        canonical_prefix = canonical_https_prefix(prefix)
        request_url = canonical_https_request_url(args.get("url"))
        if canonical_prefix is None or request_url is None:
            return False
        request = urlsplit(request_url)
        granted = urlsplit(canonical_prefix)
        prefix_path = granted.path or "/"
        request_path = request.path or "/"
        path_matches = (
            request_path.startswith(prefix_path)
            if prefix_path.endswith("/")
            else request_path == prefix_path
        )
        return bool(
            (request.hostname or "").casefold()
            == (granted.hostname or "").casefold()
            and path_matches
        )

    return False


def capability_call_satisfies_candidate(
    candidate: dict[str, Any],
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    result_data: dict[str, Any] | None = None,
    outcome: str = "success",
    skill_resource_complete: bool | None = None,
    artifacts: Iterable[dict[str, Any]] = (),
    allowed_skill_scripts: Iterable[tuple[str, str, str]] = (),
    allowed_skill_commands: Iterable[tuple[str, str, str, tuple[str, ...]]] = (),
    allowed_skill_http_prefixes: Iterable[tuple[str, str]] = (),
    allowed_skill_http_post_prefixes: Iterable[tuple[str, str]] = (),
) -> bool:
    """Match one successful dispatch receipt to one exact required candidate.

    The caller invokes this only after a real handler/MCP dispatch.  A matched
    terminal error is a concrete degraded receipt (the capability was tried),
    not a reason to replay it indefinitely.  Successful paginated disclosure
    is the exception: it remains incomplete until the exact EOF page succeeds.
    """

    if not isinstance(candidate, dict) or not isinstance(args, dict):
        return False
    kind = str(candidate.get("kind") or "")
    skill_name = str(candidate.get("skill_name") or "")

    if kind in {"native_tool", "mcp_tool"}:
        return tool_name == str(candidate.get("tool_name") or "")

    if kind == "skill_resource":
        if tool_name != "skill_view":
            return False
        path = str(candidate.get("resource_path") or "")
        expected_digest = str(candidate.get("sha256") or "")
        requested_path = args.get("file_path")
        if requested_path in {None, ""}:
            requested_path = "SKILL.md"
        # Successful disclosure is authoritative only after HarnessRunState
        # validates a contiguous offset-0..EOF chain with stable digest/size.
        # Merely asking for a later page whose response says has_more=false is
        # not a complete receipt. A terminal handler error remains a concrete
        # degraded attempt and is matched below by its exact arguments.
        if outcome == "success":
            if skill_resource_complete is not True:
                return False
            # EOF alone does not bind the bytes to the capability plan.  The
            # whole-resource digest returned by skill_view must still equal the
            # compiler-issued candidate digest, closing same-path replacement
            # between planning and direct/root execution.
            if (
                not expected_digest
                or not isinstance(result_data, dict)
                or result_data.get("sha256") != expected_digest
            ):
                return False
        return (
            args.get("name") == skill_name
            and requested_path == path
        )

    if kind == "skill_script":
        if tool_name not in {
            str(item) for item in candidate.get("tool_names") or [] if str(item)
        }:
            return False
        path = str(candidate.get("resource_path") or "")
        digest = str(candidate.get("sha256") or "")
        exact_grant = (skill_name, path, digest)
        if tool_name == "run_skill_process":
            receipt = normalize_skill_process_evidence_receipt(
                (result_data or {}).get("process_evidence_receipt")
                if isinstance(result_data, dict)
                else None
            )
            completion_operation = {
                "structured_call": "read",
                "cli_exit": "read",
                "artifact_sync": "sync",
                "artifact_close": "close",
            }.get(
                str((receipt or {}).get("completion_kind") or "")
            )
            if (
                receipt is None
                or args.get("operation") != completion_operation
                or args.get("process_id") != receipt["process_id"]
                or receipt["skill_name"] != skill_name
                or receipt["script_resource"] != path
                or receipt["script_sha256"] != digest
                or (
                    _valid_sha256(candidate.get("package_sha256"))
                    and receipt["package_sha256"]
                    != candidate["package_sha256"]
                )
                or outcome != receipt["outcome"]
            ):
                return False
            if receipt["completion_kind"] in {
                "artifact_sync",
                "artifact_close",
            }:
                manifest = skill_process_artifact_manifest_sha256(artifacts)
                if (
                    manifest is None
                    or manifest[0] != receipt["artifact_count"]
                    or manifest[1] != receipt["artifact_manifest_sha256"]
                ):
                    return False
            return exact_grant in set(allowed_skill_scripts)
        if not script_call_has_semantic_task_binding(
            tool_name,
            args,
            artifacts,
        ):
            return False
        return (
            exact_grant in set(allowed_skill_scripts)
            and args.get("script_path") == f"skills/{skill_name}/{path}"
        )

    if kind == "declared_command":
        if tool_name != "run_declared_command":
            return False
        command_id = str(candidate.get("command_id") or "")
        exact_grant = (
            skill_name,
            command_id,
            str(candidate.get("executable") or ""),
            tuple(str(item) for item in candidate.get("fixed_argv") or []),
        )
        if (
            candidate.get("additional_argv") is False
            and args.get("argv") != []
        ):
            return False
        return (
            exact_grant in set(allowed_skill_commands)
            and args.get("skill_name") == skill_name
            and args.get("command_id") == command_id
        )

    if kind == "skill_http_prefix":
        candidate_tool = str(candidate.get("tool_name") or "")
        if (
            candidate_tool not in {
                "skill_http_get", "skill_http_post_json",
            }
            or tool_name != candidate_tool
        ):
            return False
        prefix = str(candidate.get("url_prefix") or "")
        allowed_prefixes = (
            allowed_skill_http_post_prefixes
            if candidate_tool == "skill_http_post_json"
            else allowed_skill_http_prefixes
        )
        if (skill_name, prefix) not in set(allowed_prefixes):
            return False
        canonical_prefix = canonical_https_prefix(prefix)
        if canonical_prefix is None:
            return False
        receipt = result_data or {}
        matched_skill = receipt.get("matched_skill")
        matched_prefix_sha256 = receipt.get("matched_prefix_sha256")
        safe_identity_present = (
            matched_skill is not None
            or matched_prefix_sha256 is not None
        )
        if safe_identity_present:
            expected_prefix_sha256 = hashlib.sha256(
                canonical_prefix.encode("utf-8")
            ).hexdigest()
            if (
                matched_skill != skill_name
                or matched_prefix_sha256 != expected_prefix_sha256
            ):
                return False
        else:
            # Compatibility for in-process callers that still retain the raw
            # canonical args. Delegation deliberately redacts HTTP args, so
            # its outer audit can use only the handler-owned safe identity
            # above and can never persist a URL or query string.
            request_url = canonical_https_request_url(args.get("url"))
            if request_url is None:
                return False
            request = urlsplit(request_url)
            granted = urlsplit(canonical_prefix)
            prefix_path = granted.path or "/"
            request_path = request.path or "/"
            path_matches = (
                request_path.startswith(prefix_path)
                if prefix_path.endswith("/") else request_path == prefix_path
            )
            if not (
                (request.hostname or "").casefold()
                == (granted.hostname or "").casefold()
                and path_matches
            ):
                return False
        # A handler-level invalid/boundary error means no exact granted HTTP
        # attempt occurred even though its public handler was entered.
        error_code = str(receipt.get("error_code") or "")
        if error_code in {
            "invalid_url", "missing_skill_http_grant",
            "skill_http_boundary_violation", "invalid_json_body",
            "invalid_max_chars", "invalid_timeout",
            "execution_authority_revoked",
        }:
            return False
        if receipt.get("request_sent") is not True:
            # DNS, admission, or other pre-submit infrastructure failures are
            # still concrete failed attempts at this exact authenticated
            # candidate.  Count them only as degraded receipts: require the
            # handler-owned safe grant identity, a consumed/proposed request
            # number, and a terminal error outcome.  Raw model args alone can
            # never manufacture this state.
            request_number = receipt.get("request_number")
            if not (
                outcome != "success"
                and safe_identity_present
                and isinstance(request_number, int)
                and not isinstance(request_number, bool)
                and request_number > 0
                and error_code
            ):
                return False
        return True

    return False


def capability_catalog_json(catalog: dict[str, Any]) -> str:
    """Stable JSON for prompt/debug snapshots."""

    return json.dumps(catalog_prompt_payload(catalog), ensure_ascii=False, sort_keys=True)
