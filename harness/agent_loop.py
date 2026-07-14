"""Agent loop — multi-turn tool-calling with iteration budget, error retry,
and streaming think-block scrubbing.

Ported from hermes-agent/conversation_loop.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Optional

import httpx

from config import PROVIDERS, settings
from context import ContextCompressor
from error_classifier import FailoverReason, classify_api_error
from iteration_budget import IterationBudget
from prompt.builder import build_system_prompt
from retry_utils import jittered_backoff
from think_scrubber import StreamingThinkScrubber
from tools.context import ToolContext
from tools.registry import dispatch, get_schemas
from tools.tool_result_storage import wrap_result
from transports.base import build_tool_call
from workspace_context import get_workspace, load_workspace_context, SubdirectoryHintTracker

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
DEFAULT_MAX_TOKENS = 8192
MIN_COMPLEX_REPORT_BYTES = 30_000
MIN_COMPLEX_REPORT_TARGET_BYTES = 120_000
MIN_COMPLEX_REPORT_H2 = 18
MIN_COMPLEX_REPORT_H3 = 24
MIN_COMPLEX_REPORT_TABLE_ROWS = 40
MIN_COMPLEX_REPORT_CODE_FENCES_WITH_CODE = 8
MAX_VERIFIER_CONTINUATIONS = 4
MAX_ARTIFACT_ENFORCEMENT_CONTINUATIONS = 3

# Complex session-skill deliverables should cover the explicit workflow files
# declared by the skill, not just sample a few worker resources.
_MAX_REQUIRED_WORKFLOW_FILES = 32
_WORKFLOW_FILE_CATEGORIES = (
    "orchestration", "workflows", "workers", "formats", "evaluation", "scripts", "protocols",
)
_SUPPORTING_WORKFLOW_CATEGORIES = {"references", "templates", "examples"}


_LARGE_TOOL_ARGUMENT_STRING_CAP = 2_000
_DEBUG_RESULT_DEFAULT_CAP = 4_000
_CONTEXT_OVERFLOW_RE = re.compile(
    r"maximum context length is\s+(\d+)\s+tokens.*?"
    r"requested\s+(\d+)\s+output tokens.*?"
    r"prompt contains at least\s+(\d+)\s+input tokens",
    re.IGNORECASE | re.DOTALL,
)


def _estimate_serialized_tokens(value: Any) -> int:
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        serialized = str(value)
    return max(len(serialized) // 4, len(serialized.encode("utf-8")) // 4)


def _estimate_payload_tokens(messages: list[dict], tool_schemas: list[dict]) -> int:
    token_estimate = 0
    for message in messages:
        token_estimate += _estimate_serialized_tokens(message)
    for schema in tool_schemas:
        token_estimate += _estimate_serialized_tokens(schema)
    return max(1, token_estimate + len(messages) * 4 + len(tool_schemas) * 16)


def _context_safety_margin(context_limit: int) -> int:
    return max(8192, int(context_limit * 0.10))


def _clamp_max_tokens_for_context(
    requested_max_tokens: int,
    context_length: Any,
    estimated_input_tokens: int,
) -> tuple[int, dict[str, int]]:
    try:
        context_limit = int(context_length)
    except (TypeError, ValueError):
        return requested_max_tokens, {}
    if context_limit <= 0:
        return requested_max_tokens, {}
    safety_margin = _context_safety_margin(context_limit)
    available_output = context_limit - estimated_input_tokens - safety_margin
    effective_max_tokens = min(requested_max_tokens, max(512, available_output))
    return effective_max_tokens, {
        "context_length": context_limit,
        "estimated_input_tokens": estimated_input_tokens,
        "requested_max_tokens": requested_max_tokens,
        "effective_max_tokens": effective_max_tokens,
        "safety_margin": safety_margin,
        "available_output_tokens": available_output,
    }


def _retry_max_tokens_from_context_overflow(error_body: str, current_max_tokens: int) -> tuple[int | None, dict[str, int]]:
    match = _CONTEXT_OVERFLOW_RE.search(error_body or "")
    if not match:
        return None, {}
    context_limit = int(match.group(1))
    requested_output = int(match.group(2))
    prompt_tokens = int(match.group(3))
    safety_margin = _context_safety_margin(context_limit)
    available_output = context_limit - prompt_tokens - safety_margin
    if available_output < 512:
        return None, {
            "context_length": context_limit,
            "prompt_tokens": prompt_tokens,
            "requested_max_tokens": requested_output,
            "safety_margin": safety_margin,
            "available_output_tokens": available_output,
        }
    adjusted = min(current_max_tokens, requested_output, available_output)
    if adjusted >= current_max_tokens:
        return None, {}
    return adjusted, {
        "context_length": context_limit,
        "prompt_tokens": prompt_tokens,
        "requested_max_tokens": requested_output,
        "effective_max_tokens": adjusted,
        "safety_margin": safety_margin,
        "available_output_tokens": available_output,
    }


def _compact_tool_argument_value(value: Any, *, tool_name: str, key: str = "", filepath: str = "") -> Any:
    if isinstance(value, str):
        omit_written_content = (
            tool_name in {"write_file", "patch_file", "skill_manage"}
            and key in {"content", "old_text", "new_text", "file_content"}
            and len(value) > 500
        )
        if omit_written_content:
            return "__CHATDS_OMITTED_TOOL_CONTENT_REGENERATE_OR_READ_SOURCE__"
        if len(value) > _LARGE_TOOL_ARGUMENT_STRING_CAP:
            return f"__CHATDS_OMITTED_TOOL_ARGUMENT_{len(value)}_CHARS__"
        return value
    if isinstance(value, list):
        return [
            _compact_tool_argument_value(item, tool_name=tool_name, key=key, filepath=filepath)
            for item in value
        ]
    if isinstance(value, dict):
        nested_filepath = str(value.get("filepath") or value.get("file_path") or filepath or "")
        return {
            str(k): _compact_tool_argument_value(
                v, tool_name=tool_name, key=str(k), filepath=nested_filepath,
            )
            for k, v in value.items()
        }
    return value


def _compact_tool_call_arguments(tool_name: str, arguments: str) -> str:
    args = _safe_parse_args(arguments or "")
    if not isinstance(args, dict) or "__tool_arg_parse_error" in args:
        if len(arguments or "") <= _LARGE_TOOL_ARGUMENT_STRING_CAP:
            return arguments or "{}"
        return json.dumps({
            "_arguments_omitted": f"__CHATDS_OMITTED_TOOL_ARGUMENT_{len(arguments or '')}_CHARS__",
        })
    compacted = _compact_tool_argument_value(args, tool_name=tool_name)
    return json.dumps(compacted, ensure_ascii=False)


def _debug_payload(value: Any, *, cap: int | None = None) -> Any:
    cap = max(200, int(cap or getattr(settings, "agent_debug_trace_result_chars", _DEBUG_RESULT_DEFAULT_CAP)))
    if isinstance(value, str):
        if len(value) <= cap:
            return value
        return value[:cap] + f"\n... [truncated {len(value) - cap} chars]"
    if isinstance(value, list):
        return [_debug_payload(item, cap=cap) for item in value[:50]]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(marker in lowered for marker in ("token", "secret", "password", "api_key", "authorization")):
                cleaned[key_text] = "[redacted]"
            else:
                cleaned[key_text] = _debug_payload(item, cap=cap)
        return cleaned
    return value


def _workspace_debug_trace_enabled() -> bool:
    return bool(getattr(settings, "agent_debug_trace_workspace", True))


def _append_workspace_debug_event(user_id: str, session_id: str, event: dict[str, Any]) -> None:
    if not _workspace_debug_trace_enabled():
        return
    run_id = str(event.get("run_id") or "unknown")
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:128] or "unknown"
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "event_type": event.get("event_type"),
        "run_id": event.get("run_id"),
        "root_run_id": event.get("root_run_id"),
        "parent_run_id": event.get("parent_run_id"),
        "agent_kind": event.get("agent_kind"),
        "agent_name": event.get("agent_name"),
        "depth": event.get("depth"),
        "workspace_scope": event.get("workspace_scope"),
        "seq": event.get("seq"),
        "tool_name": event.get("tool_name"),
        "tool_call_id": event.get("tool_call_id"),
        "payload": event.get("payload") or {},
    }
    try:
        debug_dir = get_workspace(user_id, session_id) / "debug" / "agent_runs"
        debug_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with (debug_dir / f"{safe_run_id}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line)
        with (debug_dir / "latest.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        logger.debug(
            "Failed to append workspace debug trace user=%s session=%s run=%s",
            user_id, session_id, run_id, exc_info=True,
        )


def _tool_debug_result(raw: str) -> dict[str, Any]:
    parsed = _json_object(raw)
    payload: dict[str, Any] = {"raw_excerpt": _debug_payload(raw)}
    if isinstance(parsed, dict):
        payload["status"] = parsed.get("status")
        payload["error"] = parsed.get("error")
        payload["returncode"] = parsed.get("returncode")
        payload["script_path"] = parsed.get("script_path")
        payload["cwd"] = parsed.get("cwd")
        payload["runtime_status"] = parsed.get("runtime_status")
        payload["stdout_excerpt"] = _debug_payload(str(parsed.get("stdout") or "")) if parsed.get("stdout") is not None else None
        payload["stderr_excerpt"] = _debug_payload(str(parsed.get("stderr") or "")) if parsed.get("stderr") is not None else None
        payload = {k: v for k, v in payload.items() if v not in (None, "")}
    return payload


@dataclass
class HarnessRunState:
    user_id: str = "default"
    session_id: str = "default"
    tool_call_count: int = 0
    tool_error_count: int = 0
    parse_failure_count: int = 0
    schema_failure_count: int = 0
    successful_write_sizes: list[int] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    invalid_placeholder_write_count: int = 0
    viewed_skill_names: set[str] = field(default_factory=set)
    viewed_skill_files: dict[str, set[str]] = field(default_factory=dict)
    viewed_skill_categories: dict[str, set[str]] = field(default_factory=dict)
    skill_available_categories: dict[str, set[str]] = field(default_factory=dict)
    skill_suggested_files: dict[str, list[str]] = field(default_factory=dict)
    skill_category_files: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    skill_workflow_contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_skill_names: set[str] = field(default_factory=set)
    successful_search_count: int = 0
    successful_extract_count: int = 0
    successful_code_execution_count: int = 0
    successful_skill_python_count: int = 0
    successful_mcp_tool_count: int = 0
    continuation_reasons: list[str] = field(default_factory=list)
    last_tool_error_at: int = 0
    last_successful_artifact_at: int = 0
    last_parse_failure_at: int = 0
    last_schema_failure_at: int = 0

    def record_skill_view(
        self,
        args: dict[str, Any],
        result_data: dict[str, Any] | None = None,
    ) -> None:
        name = args.get("name")
        if not name:
            return
        skill_name = str(name)
        self.viewed_skill_names.add(skill_name)
        file_path = args.get("file_path")
        if isinstance(file_path, str) and file_path:
            self.viewed_skill_files.setdefault(skill_name, set()).add(file_path)
            category = file_path.split("/", 1)[0]
            self.viewed_skill_categories.setdefault(skill_name, set()).add(category)
        if isinstance(result_data, dict):
            linked = result_data.get("linked_files")
            if isinstance(linked, dict):
                self.skill_available_categories.setdefault(skill_name, set()).update(
                    str(category) for category in linked.keys()
                )
                declared = self.skill_category_files.setdefault(skill_name, {})
                for category, files in linked.items():
                    if isinstance(files, list) and files:
                        declared[str(category)] = [str(f) for f in files if isinstance(f, str)]
            graph = result_data.get("resource_graph")
            if isinstance(graph, dict):
                categories = graph.get("categories")
                if isinstance(categories, dict):
                    self.skill_available_categories.setdefault(skill_name, set()).update(
                        str(category) for category in categories.keys()
                    )
                    declared = self.skill_category_files.setdefault(skill_name, {})
                    for category, meta in categories.items():
                        # Prefer the fuller linked_files list; fall back to the sample.
                        if str(category) in declared:
                            continue
                        sample = meta.get("sample") if isinstance(meta, dict) else None
                        if isinstance(sample, list) and sample:
                            declared[str(category)] = [str(f) for f in sample if isinstance(f, str)]
                suggested = graph.get("suggested_files")
                if isinstance(suggested, list):
                    clean_suggested = [str(path) for path in suggested if isinstance(path, str)]
                    if clean_suggested:
                        self.skill_suggested_files[skill_name] = clean_suggested
            contract = result_data.get("workflow_contract")
            if isinstance(contract, dict) and contract:
                self.skill_workflow_contracts[skill_name] = contract

    def unviewed_session_skills(self) -> set[str]:
        return self.session_skill_names - self.viewed_skill_names

    def needs_more_skill_workflow(self) -> tuple[bool, str]:
        for skill_name in sorted(self.viewed_skill_names & self.session_skill_names):
            available_categories = self.skill_available_categories.get(skill_name, set())
            if not available_categories:
                continue
            files = self.viewed_skill_files.get(skill_name, set())
            categories = self.viewed_skill_categories.get(skill_name, set())
            if "__manifest__" not in files:
                return True, f"load the resource manifest for session skill '{skill_name}'"

            required_files = _required_workflow_files(self.skill_category_files.get(skill_name, {}))
            missing_required = [path for path in required_files if path not in files]
            if missing_required:
                return True, (
                    f"inspect explicit workflow resources for session skill '{skill_name}' "
                    f"(missing {len(missing_required)} of {len(required_files)} declared files)"
                )

            primary_workflow_categories = {"orchestration", "workers", "workflows"}
            if available_categories & primary_workflow_categories:
                viewed_primary = categories & primary_workflow_categories
                viewed_worker_file = any("/workers/" in path for path in files)
                if not viewed_primary and not viewed_worker_file:
                    return True, f"inspect orchestrator or worker resources for session skill '{skill_name}'"

            if "formats" in available_categories and "formats" not in categories:
                return True, f"inspect output format specifications for session skill '{skill_name}'"

            available_supporting = available_categories & _SUPPORTING_WORKFLOW_CATEGORIES
            if available_supporting and not (categories & available_supporting):
                return True, f"inspect supporting resources for session skill '{skill_name}'"

            contract = self.skill_workflow_contracts.get(skill_name) or {}
            contract_required = _contract_required_files(contract)
            missing_contract = [path for path in contract_required if path not in files]
            if missing_contract:
                return True, (
                    f"inspect workflow contract resources for session skill '{skill_name}' "
                    f"(missing {len(missing_contract)} of {len(contract_required)} contract files)"
                )

            if contract.get("requires_worker_outputs"):
                worker_files = [str(path) for path in contract.get("worker_files") or []]
                missing_workers = [path for path in worker_files if path not in files]
                if missing_workers:
                    return True, (
                        f"inspect all declared worker resources for session skill '{skill_name}' "
                        f"(missing {len(missing_workers)} worker files)"
                    )
                if _successful_evidence_tool_count(self) < max(1, min(3, len(worker_files))):
                    return True, (
                        f"collect worker evidence for session skill '{skill_name}' before final synthesis"
                    )

            if contract.get("requires_modular_artifacts") and not _has_modular_artifacts_for_contract(self, contract):
                return True, f"generate declared modular/checklist artifacts for session skill '{skill_name}'"

            if contract.get("requires_merge") and not _has_merged_artifact_for_contract(self, contract):
                return True, f"create the declared merged final report artifact for session skill '{skill_name}'"
        return False, ""


def _required_workflow_files(declared: dict[str, list[str]]) -> list[str]:
    required: list[str] = []
    for category in _WORKFLOW_FILE_CATEGORIES:
        for path in declared.get(category, []):
            if _is_required_workflow_file(path):
                required.append(path)
    return _dedupe_paths(required)[:_MAX_REQUIRED_WORKFLOW_FILES]


def _is_required_workflow_file(path: str) -> bool:
    normalized = str(path or "").strip()
    if not normalized or normalized.endswith("/"):
        return False
    name = normalized.rsplit("/", 1)[-1].lower()
    if name in {"readme.md", "license", "license.md"}:
        return False
    return normalized.startswith((
        "orchestration/", "workflows/", "workers/", "formats/",
        "evaluation/", "scripts/", "protocols/",
    )) or any(part in normalized for part in (
        "/orchestration/", "/workflows/", "/workers/", "/formats/",
        "/evaluation/", "/scripts/", "/protocols/",
    ))


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        normalized = str(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _contract_required_files(contract: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("orchestrator_files", "worker_files", "format_files"):
        value = contract.get(key)
        if isinstance(value, list):
            paths.extend(str(path) for path in value if isinstance(path, str))
    workflow_files = contract.get("workflow_files")
    if isinstance(workflow_files, list):
        paths.extend(str(path) for path in workflow_files if isinstance(path, str) and _is_required_workflow_file(path))
    return _dedupe_paths(paths)[:_MAX_REQUIRED_WORKFLOW_FILES]


def _artifact_path_strings(run_state: HarnessRunState) -> list[str]:
    paths: list[str] = []
    for artifact in run_state.artifacts:
        if not isinstance(artifact, dict):
            continue
        path = artifact.get("path")
        if isinstance(path, str):
            paths.append(path)
    return _dedupe_paths(paths)


def _has_modular_artifacts_for_contract(run_state: HarnessRunState, contract: dict[str, Any]) -> bool:
    paths = _artifact_path_strings(run_state)
    if not paths:
        return False
    markdown_paths = [path for path in paths if path.lower().endswith(".md")]
    if not markdown_paths:
        return False
    patterns = [str(item).lower() for item in (contract.get("artifact_patterns") or [])]
    declared_count = _declared_file_count(patterns)
    numbered_count = sum(1 for path in markdown_paths if re.search(r"(?:^|/)\d{2}_[^/]+\.md$", path))
    has_checklist = any("checklist" in path.lower() for path in markdown_paths)
    if declared_count and len(markdown_paths) >= min(declared_count, 8):
        return True
    if numbered_count >= 3:
        return True
    if has_checklist and len(markdown_paths) >= 2:
        return True
    return not patterns and bool(markdown_paths)


def _has_merged_artifact_for_contract(run_state: HarnessRunState, contract: dict[str, Any]) -> bool:
    paths = [path.lower() for path in _artifact_path_strings(run_state) if path.lower().endswith(".md")]
    if not paths:
        return False
    if any("full_report" in path or "full-report" in path or "merged" in path for path in paths):
        return True
    if contract.get("requires_merge"):
        return False
    return bool(paths)


def _declared_file_count(patterns: list[str]) -> int | None:
    for pattern in patterns:
        match = re.match(r"declared_file_count:(\d+)", pattern)
        if match:
            return int(match.group(1))
    return None


def _workflow_contract_findings(run_state: HarnessRunState) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for skill_name, contract in sorted(run_state.skill_workflow_contracts.items()):
        files = run_state.viewed_skill_files.get(skill_name, set())
        required_files = _contract_required_files(contract)
        missing = [path for path in required_files if path not in files]
        if missing:
            findings.append({
                "severity": "blocker",
                "category": "skill_workflow_contract",
                "message": f"Missing {len(missing)} declared workflow resource inspections for skill {skill_name}.",
                "missing_files": missing[:20],
            })
        worker_files = [str(path) for path in contract.get("worker_files") or []]
        if worker_files and _successful_evidence_tool_count(run_state) < max(1, min(3, len(worker_files))):
            findings.append({
                "severity": "blocker",
                "category": "skill_worker_evidence",
                "message": f"Worker workflow for skill {skill_name} needs evidence/tool execution before final synthesis.",
                "worker_count": len(worker_files),
                "evidence_tool_count": _successful_evidence_tool_count(run_state),
            })
        if contract.get("requires_modular_artifacts") and not _has_modular_artifacts_for_contract(run_state, contract):
            findings.append({
                "severity": "blocker",
                "category": "skill_artifacts",
                "message": f"Skill {skill_name} declares modular/checklist artifacts that are not present in workspace artifacts.",
                "artifact_patterns": (contract.get("artifact_patterns") or [])[:20],
            })
        if contract.get("requires_merge") and not _has_merged_artifact_for_contract(run_state, contract):
            findings.append({
                "severity": "blocker",
                "category": "skill_merge",
                "message": f"Skill {skill_name} declares a merged final report but no merged/full report artifact is present.",
                "merge_requirements": (contract.get("merge_requirements") or [])[:10],
            })
    return findings


def _successful_evidence_tool_count(run_state: HarnessRunState) -> int:
    return (
        run_state.successful_search_count
        + run_state.successful_extract_count
        + run_state.successful_code_execution_count
        + run_state.successful_skill_python_count
        + run_state.successful_mcp_tool_count
    )


def _written_markdown_artifact_paths(run_state: HarnessRunState) -> list[str]:
    paths: list[str] = []
    for artifact in run_state.artifacts:
        path = artifact.get("path") if isinstance(artifact, dict) else None
        if isinstance(path, str) and path.endswith(".md"):
            paths.append(path)
    return _dedupe_paths(paths)


def _complex_report_context(run_state: HarnessRunState, original_user_text: str) -> bool:
    return bool(
        _looks_like_complex_artifact_request(original_user_text)
        or run_state.session_skill_names
        or run_state.viewed_skill_names
        or _successful_evidence_tool_count(run_state) >= 2
    )


def _needs_complex_artifact_gate(run_state: HarnessRunState, original_user_text: str, visible_content: str = "") -> bool:
    if not _complex_report_context(run_state, original_user_text):
        return False
    largest_write = max(run_state.successful_write_sizes or [0])
    if run_state.invalid_placeholder_write_count > 0:
        return True
    if run_state.tool_error_count >= 2:
        return True
    if not largest_write and len(visible_content.strip()) < 8_000:
        return True
    if _successful_evidence_tool_count(run_state) > 0 and largest_write < MIN_COMPLEX_REPORT_TARGET_BYTES:
        return True
    if _written_markdown_artifact_paths(run_state) and largest_write < MIN_COMPLEX_REPORT_TARGET_BYTES:
        return True
    return False


def _suggested_workflow_paths_for_reason(
    run_state: HarnessRunState,
    reason: str,
) -> list[str]:
    requested_primary = "orchestrator" in reason or "worker" in reason
    requested_formats = "format" in reason
    requested_supporting = "supporting" in reason
    paths: list[str] = []
    for skill_name in sorted(run_state.viewed_skill_names & run_state.session_skill_names):
        already_viewed = run_state.viewed_skill_files.get(skill_name, set())
        missing_required = [
            path for path in _required_workflow_files(run_state.skill_category_files.get(skill_name, {}))
            if path not in already_viewed
        ]
        if missing_required:
            paths.extend(missing_required)
            continue
        for path in run_state.skill_suggested_files.get(skill_name, []):
            if path in already_viewed:
                continue
            if requested_primary:
                if path.startswith(("orchestration/", "workers/", "workflows/")) or "/workers/" in path:
                    paths.append(path)
            elif requested_formats:
                if path.startswith("formats/") or "/formats/" in path:
                    paths.append(path)
            elif requested_supporting:
                if path.startswith((
                    "references/", "scripts/", "templates/", "protocols/",
                    "evaluation/", "examples/",
                )):
                    paths.append(path)
            else:
                paths.append(path)
    return _dedupe_paths(paths)


async def run_stream(
    model_id: str,
    messages: list[dict],
    enabled_tools: list[str] | None = None,
    user_id: str = "default",
    session_id: str = "default",
    timeout: float = 600.0,
    max_iterations: int = 60,
    max_tokens: int | None = None,
    provider_override: dict[str, Any] | None = None,
    fallback_overrides: list[dict[str, Any]] | None = None,
    source: str = "chat",
    enabled_user_skills: list[str] | None = None,
    run_id: str | None = None,
    root_run_id: str | None = None,
    parent_run_id: str | None = None,
    agent_kind: str = "primary",
    agent_name: str | None = None,
    depth: int = 0,
    workspace_scope: str = "shared_session",
    event_schema: str = "flat",
    event_sink: Any | None = None,
) -> AsyncIterator[dict]:
    """Async generator yielding SSE-style dicts for a full agent conversation turn.

    Yields:
      {"type": "tool_progress", "msg": str}
      {"type": "delta", "content": str}
      {"type": "reasoning_delta", "content": str}
      {"type": "done", "finish_reason": str}
      {"type": "error", "msg": str}
    """
    provider = dict(provider_override or PROVIDERS.get(model_id) or {})
    if not provider:
        msg = f"Unknown model: {model_id}"
        event = {
            "type": "agent_event",
            "event_type": "run.failed",
            "run_id": run_id,
            "root_run_id": root_run_id or run_id,
            "parent_run_id": parent_run_id,
            "agent_kind": agent_kind,
            "agent_name": agent_name or agent_kind,
            "depth": depth,
            "workspace_scope": workspace_scope,
            "seq": 1,
            "payload": {"error": msg},
        }
        if event_sink is not None:
            maybe = event_sink(event)
            if hasattr(maybe, "__await__"):
                await maybe
        yield event
        yield {"type": "error", "msg": msg}
        return
    provider.setdefault("id", model_id)
    provider.setdefault("protocol", "openai")
    provider.setdefault("context_length", 262144)
    provider_chain = [provider]
    for fallback in fallback_overrides or []:
        if fallback and fallback.get("base_url") and fallback.get("api_model"):
            normalized = dict(fallback)
            normalized.setdefault("id", normalized.get("api_model"))
            normalized.setdefault("protocol", "openai")
            normalized.setdefault("context_length", 262144)
            provider_chain.append(normalized)
    provider_cursor = 0

    def apply_provider(selected: dict) -> tuple[str, str, str, str, dict[str, str]]:
        base = str(selected["base_url"]).rstrip("/")
        api_name = str(selected["api_model"])
        key = str(selected.get("api_key") or "")
        protocol_name = str(selected.get("protocol") or "openai").lower()
        request_headers = {
            "Content-Type": "application/json",
            **(selected.get("extra_headers") or {}),
        }
        if protocol_name == "anthropic":
            if key and key != "EMPTY":
                request_headers["x-api-key"] = key
            request_headers.setdefault("anthropic-version", "2023-06-01")
        elif key and key != "EMPTY":
            request_headers["Authorization"] = f"Bearer {key}"
        return base, api_name, key, protocol_name, request_headers

    base_url, api_model, api_key, protocol, headers = apply_provider(provider)

    tools = list(enabled_tools or [])
    tool_context = ToolContext(
        user_id=user_id,
        session_id=session_id,
        model_id=model_id,
        provider_config=provider,
        fallback_configs=tuple(provider_chain[1:]),
        enabled_tools=tuple(tools),
        source=source,
        enabled_user_skills=tuple(enabled_user_skills or []),
        run_id=run_id,
        root_run_id=root_run_id or run_id,
        parent_run_id=parent_run_id,
        agent_kind=agent_kind,
        agent_name=agent_name,
        depth=depth,
        workspace_scope=workspace_scope,
        event_sink=event_sink,
    )
    hint_tracker = SubdirectoryHintTracker(user_id, session_id)
    run_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    event_seq = 0
    debug_trace_enabled = bool(getattr(settings, "agent_debug_trace", False))

    async def emit_agent_event(event_type: str, payload: dict[str, Any] | None = None, **extra: Any) -> dict:
        nonlocal event_seq
        event_seq += 1
        event = {
            "type": "agent_event",
            "event_type": event_type,
            "run_id": run_id,
            "root_run_id": root_run_id or run_id,
            "parent_run_id": parent_run_id,
            "agent_kind": agent_kind,
            "agent_name": agent_name or agent_kind,
            "depth": depth,
            "workspace_scope": workspace_scope,
            "seq": event_seq,
            "payload": payload or {},
        }
        event.update(extra)
        if event_sink is not None:
            maybe = event_sink(event)
            if hasattr(maybe, "__await__"):
                await maybe
        return event

    async def emit_debug_event(event_type: str, payload: dict[str, Any] | None = None, **extra: Any) -> dict | None:
        if not debug_trace_enabled:
            return None
        event = await emit_agent_event(
            f"debug.{event_type}",
            _debug_payload(payload or {}),
            **extra,
        )
        _append_workspace_debug_event(user_id, session_id, event)
        return event

    async def debug_stream_event(event_type: str, payload: dict[str, Any] | None = None, **extra: Any) -> list[dict]:
        event = await emit_debug_event(event_type, payload, **extra)
        return [event] if event is not None else []

    goal_continuations = 0
    goal_parse_failures = 0
    run_state = HarnessRunState(user_id=user_id, session_id=session_id)
    artifact_enforcement_continuations = 0
    verifier_continuations = 0
    skill_enforcement_continuations = 0
    skill_workflow_continuations = 0
    action_promise_continuations = 0
    tool_failure_continuations = 0
    max_skill_workflow_continuations = 12
    max_artifact_enforcement_continuations = MAX_ARTIFACT_ENFORCEMENT_CONTINUATIONS
    max_verifier_continuations = MAX_VERIFIER_CONTINUATIONS

    def workflow_reason_retryable(reason: str) -> bool:
        return run_state.continuation_reasons.count(reason) < 2

    def queue_skill_workflow_continuation(reason: str) -> None:
        run_state.continuation_reasons.append(reason)
        requires_manifest = "manifest" in reason
        suggested_paths = [] if requires_manifest else _suggested_workflow_paths_for_reason(run_state, reason)
        suggested_line = (
            "\nSuggested skill resource paths to inspect now: " + ", ".join(suggested_paths[:20])
            if suggested_paths else ""
        )
        next_action = (
            "Your next tool call MUST be skill_view(name, file_path='__manifest__') for the relevant session skill. "
            if requires_manifest else
            "Your next tool calls MUST inspect the suggested skill resource paths below with "
            "skill_view(name, file_path=that_path). You may issue multiple independent skill_view "
            "calls in the same assistant turn to cover the missing workflow files. "
        )
        if "collect worker evidence" in reason:
            next_action = (
                "Your next steps MUST execute the declared worker workflow: use the skill's declared MCP/database/search/script resources, "
                "collect source evidence for each worker, and preserve worker outputs as workspace artifacts before final synthesis. "
            )
        elif "modular" in reason or "checklist" in reason:
            next_action = (
                "Your next steps MUST write the declared modular artifact/checklist files into the current session workspace. "
                "Do not stop with a single condensed report when the skill contract declares multiple artifacts. "
            )
        elif "merged final report" in reason or "merged" in reason:
            next_action = (
                "Your next steps MUST create the declared merged/full report artifact from the modular files and verify its sanity checks. "
            )
        conversation.append({
            "role": "user",
            "content": (
                "The task uses a session-level skill for a complex deliverable, but the skill workflow "
                f"is not sufficiently inspected yet: {reason}. "
                "Before doing web search, code execution, workspace file reads, or final writing, call "
                "skill_view for the missing skill resources. "
                f"{next_action}"
                "Use skill_view for skill resources; read_file/search_files are only for workspace files."
                f"{suggested_line}\n"
                "After reading those skill resources, continue the task."
            ),
        })
    if "skill_view" in tools:
        try:
            from skills.scanner import find_all_skills
            run_state.session_skill_names = {
                str(skill.get("name"))
                for skill in find_all_skills(
                    user_id,
                    session_id,
                    enabled_user_skills=enabled_user_skills,
                )
                if skill.get("scope") == "session" and skill.get("name")
            }
        except Exception:
            logger.debug(
                "Failed to inspect session skills for user=%s session=%s",
                user_id, session_id, exc_info=True,
            )

    original_user_text_for_budget = _latest_user_text(messages)
    if _looks_like_complex_artifact_request(original_user_text_for_budget) or run_state.session_skill_names:
        complex_max_iterations = max(
            max_iterations,
            int(getattr(settings, "complex_report_max_iterations", max_iterations) or max_iterations),
        )
        max_iterations = min(complex_max_iterations, 240)

    yield await emit_agent_event("run.started", {
        "model_id": model_id,
        "source": source,
        "enabled_tools": tools,
        "max_iterations": max_iterations,
    })

    # ── Auto-connect MCP servers for exactly this user+session ──────────
    mcp_tool_names: list[str] = []
    try:
        from tools.mcp_client import (
            connect_all_for_user,
            get_session_tool_names,
        )
        mcp_results = await connect_all_for_user(
            user_id, session_id,
            enabled_user_skills=enabled_user_skills,
        )
        connected = [n for n, ok in mcp_results.items() if ok]
        if connected:
            logger.info(
                "Auto-connected MCP servers for user=%s session=%s: %s",
                user_id, session_id, connected,
            )
        mcp_tool_names = get_session_tool_names(user_id, session_id)
    except Exception:
        logger.exception(
            "Failed to initialize session MCP catalog for user=%s session=%s",
            user_id, session_id,
        )

    prompt_tools = list(dict.fromkeys(tools + mcp_tool_names))

    budget = IterationBudget(max_iterations)
    scrubber = StreamingThinkScrubber()

    # ── Initialize context compressor ───────────────────────────────────
    # Uses planner model (qwen3_5) as the auxiliary summarization model.
    compressor_cfg = PROVIDERS.get(settings.compressor_model, PROVIDERS.get("qwen3_5", provider))
    compressor = ContextCompressor(
        base_url=compressor_cfg["base_url"],
        api_model=compressor_cfg["api_model"],
        api_key=compressor_cfg["api_key"],
    )

    # Work on a copy so we don't mutate the caller's list.
    conversation: list[dict] = list(messages)
    original_user_text = _latest_user_text(conversation)

    # ── Image routing: pre-analyze images for text-only models ─────────
    # When the active model is text-only (deepseek_v4_pro),
    # pre-analyze any images in the last user message with qwen3_5 and
    # replace the image content with text descriptions.  The agent still
    # has vision_analyze as a tool for deeper inspection.
    # This mirrors hermes-agent's _enrich_message_with_vision().
    is_multimodal = provider.get("is_multimodal", False)
    if not is_multimodal and conversation:
        last_msg = conversation[-1]
        if last_msg.get("role") == "user":
            content = last_msg.get("content")
            # Check for image_url parts in the content
            if isinstance(content, list):
                image_urls: list[str] = []
                text_parts: list[str] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        img_data = part.get("image_url", {})
                        url = img_data.get("url", "") if isinstance(img_data, dict) else str(img_data or "")
                        if url:
                            image_urls.append(url)
                    elif isinstance(part, dict) and part.get("type") == "text":
                        t = str(part.get("text", "")).strip()
                        if t:
                            text_parts.append(t)
                    elif isinstance(part, str) and part.strip():
                        text_parts.append(part.strip())

                if image_urls:
                    from agent.image_routing import enrich_message_with_vision
                    user_text = "\n".join(text_parts)
                    enriched = await enrich_message_with_vision(
                        user_text, image_urls,
                        session_id=session_id, user_id=user_id,
                    )
                    # Replace multimodal content with plain text
                    conversation[-1] = {**last_msg, "content": enriched}
                    logger.info(
                        "Image routing: enriched %d image(s) with vision descriptions "
                        "(model %s is text-only)", len(image_urls), model_id,
                    )

    # ── Build and inject system prompt ────────────────────────────────
    # Extract caller-supplied system_message if present, then replace with
    # our full three-tier system prompt (stable / context / volatile).
    caller_system_message = None
    if conversation and conversation[0].get("role") == "system":
        caller_system_message = conversation[0].get("content", "")
        # Remove the caller's bare system message — we replace it below
        conversation.pop(0)

    goal = await _fetch_goal(user_id, session_id)
    system_prompt = build_system_prompt(
        user_id=user_id,
        session_id=session_id,
        system_message=caller_system_message,
        enabled_tools=prompt_tools,
        model_id=model_id,
        provider=provider.get("provider", ""),
        workspace_context=load_workspace_context(user_id, session_id),
        goal=goal,
        enabled_user_skills=enabled_user_skills,
    )
    conversation.insert(0, {"role": "system", "content": system_prompt})

    while budget.remaining > 0:
        if not budget.consume():
            msg = "Agent iteration budget exhausted."
            yield await emit_agent_event("run.failed", {"error": msg})
            yield {"type": "error", "msg": msg}
            return
        for debug_evt in await debug_stream_event("iteration.started", {
            "iteration": budget.used,
            "remaining_after_consume": budget.remaining,
            "max_iterations": budget.max_total,
            "conversation_messages": len(conversation),
            "tool_calls_so_far": run_state.tool_call_count,
            "tool_errors_so_far": run_state.tool_error_count,
            "skill_workflow_continuations": skill_workflow_continuations,
            "artifact_enforcement_continuations": artifact_enforcement_continuations,
            "action_promise_continuations": action_promise_continuations,
            "goal_continuations": goal_continuations,
        }):
            yield debug_evt

        # ── Sanitize messages before sending ──────────────────────────
        sanitized = _sanitize_messages(
            conversation,
            strip_images=not bool(provider.get("is_multimodal", False)),
        )

        # ── Build request body ────────────────────────────────────────
        tool_schemas = get_schemas(tools) if tools else []
        deferred_catalog = None
        try:
            from tools.mcp_client import get_session_tool_definitions
            mcp_definitions = get_session_tool_definitions(user_id, session_id)
            from tools.tool_search import (
                DeferredCatalog, bridge_schemas, should_defer,
            )
            if should_defer(
                mcp_definitions,
                int(provider.get("context_length") or 262144),
            ):
                deferred_catalog = DeferredCatalog.from_definitions(mcp_definitions)
                tool_schemas.extend(bridge_schemas(len(deferred_catalog.definitions)))
            else:
                tool_schemas.extend(mcp_definitions)
        except Exception:
            logger.exception(
                "Failed to build session MCP schemas for user=%s session=%s",
                user_id, session_id,
            )

        requested_max_tokens = int(max_tokens or DEFAULT_MAX_TOKENS)
        estimated_input_tokens = _estimate_payload_tokens(sanitized, tool_schemas)
        effective_max_tokens, max_token_budget = _clamp_max_tokens_for_context(
            requested_max_tokens,
            provider.get("context_length"),
            estimated_input_tokens,
        )
        if effective_max_tokens < requested_max_tokens:
            log_fn = logger.warning if max_token_budget.get("available_output_tokens", 0) < 512 else logger.info
            log_fn(
                "Clamped max_tokens for model=%s context_length=%s estimated_input=%s requested=%s effective=%s safety_margin=%s",
                provider.get("id") or api_model,
                max_token_budget.get("context_length"),
                max_token_budget.get("estimated_input_tokens"),
                max_token_budget.get("requested_max_tokens"),
                max_token_budget.get("effective_max_tokens"),
                max_token_budget.get("safety_margin"),
            )

        body: dict = {
            "model": api_model,
            "messages": sanitized,
            "max_tokens": effective_max_tokens,
            "temperature": 0.7,
            "stream": True,
        }
        if tool_schemas:
            body["tools"] = tool_schemas
        if protocol != "anthropic":
            body["stream_options"] = {"include_usage": True}
        for debug_evt in await debug_stream_event("llm.request", {
            "iteration": budget.used,
            "provider_model": provider.get("id") or model_id,
            "api_model": api_model,
            "protocol": protocol,
            "message_count": len(sanitized),
            "tool_schema_count": len(tool_schemas),
            "requested_max_tokens": requested_max_tokens,
            "effective_max_tokens": effective_max_tokens,
            "estimated_input_tokens": estimated_input_tokens,
            "context_budget": max_token_budget,
        }):
            yield debug_evt

        # Qwen-specific: disable thinking by default (it's mainly a tool model)
        if (provider.get("id") or model_id) == "qwen3_5":
            body["chat_template_kwargs"] = {"enable_thinking": False}

        # ── Call LLM with retry ───────────────────────────────────────
        full_content = ""
        full_reasoning = ""
        finish_reason: Optional[str] = None
        tool_call_fragments: dict[int, dict] = {}  # index -> {id, name, arguments}
        api_usage: dict[str, int] = {}  # captured from streaming chunks

        fallback_requested = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                finish_reason = None
                tool_call_fragments.clear()
                scrubber.reset()

                request_url, request_body = _build_provider_request(
                    base_url=base_url,
                    protocol=protocol,
                    body=body,
                )
                stream_timeout = httpx.Timeout(
                    connect=max(1.0, float(settings.llm_stream_connect_timeout_seconds)),
                    read=max(5.0, float(settings.llm_stream_read_timeout_seconds)),
                    write=30.0,
                    pool=30.0,
                )
                total_stream_timeout = max(
                    30.0,
                    float(settings.llm_stream_total_timeout_seconds),
                )

                async def consume_provider_stream() -> None:
                    nonlocal full_content, full_reasoning, finish_reason
                    async with httpx.AsyncClient(timeout=stream_timeout) as client:
                        async with client.stream(
                            "POST",
                            request_url,
                            headers=headers,
                            json=request_body,
                        ) as resp:
                            if resp.status_code != 200:
                                body_bytes = await resp.aread()
                                err_text = body_bytes.decode(errors="replace")[:1000]
                                raise _http_error(resp.status_code, err_text)

                            async for normalized in _iter_provider_stream(resp, protocol):
                                reasoning = normalized.get("reasoning", "")
                                if reasoning:
                                    scrubbed_reasoning = scrubber.feed(reasoning)
                                    if scrubbed_reasoning:
                                        full_reasoning += scrubbed_reasoning
                                        yield await emit_agent_event("agent.reasoning_delta", {"content": scrubbed_reasoning})
                                        yield {
                                            "type": "reasoning_delta",
                                            "content": scrubbed_reasoning,
                                        }

                                content = normalized.get("content", "")
                                if content:
                                    scrubbed_content = scrubber.feed(content)
                                    if scrubbed_content:
                                        full_content += scrubbed_content
                                        yield await emit_agent_event("agent.delta", {"content": scrubbed_content})
                                        yield {
                                            "type": "delta",
                                            "content": scrubbed_content,
                                        }

                                for tc_delta in normalized.get("tool_calls", []):
                                    idx = tc_delta.get("index", 0)
                                    if idx not in tool_call_fragments:
                                        tool_call_fragments[idx] = {
                                            "id": None,
                                            "name": "",
                                            "arguments": "",
                                        }
                                    frag = tool_call_fragments[idx]
                                    if tc_delta.get("id"):
                                        frag["id"] = tc_delta["id"]
                                    fn = tc_delta.get("function", {})
                                    if fn.get("name"):
                                        frag["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        frag["arguments"] += fn["arguments"]

                                fr = normalized.get("finish_reason")
                                if fr:
                                    finish_reason = fr

                                usage = normalized.get("usage")
                                if usage:
                                    api_usage["prompt_tokens"] = max(
                                        api_usage.get("prompt_tokens", 0),
                                        int(usage.get("prompt_tokens", 0) or 0),
                                    )
                                    api_usage["completion_tokens"] = max(
                                        api_usage.get("completion_tokens", 0),
                                        int(usage.get("completion_tokens", 0) or 0),
                                    )
                                    api_usage["total_tokens"] = max(
                                        api_usage.get("total_tokens", 0),
                                        int(usage.get("total_tokens", 0) or 0),
                                        api_usage["prompt_tokens"] + api_usage["completion_tokens"],
                                    )

                async for stream_event in _aiter_with_timeout(
                    consume_provider_stream(),
                    timeout_seconds=total_stream_timeout,
                ):
                    yield stream_event

                # Flush scrubber tail
                tail = scrubber.flush()
                if tail:
                    full_content += tail
                    yield {"type": "delta", "content": tail}

                # Success — break out of retry loop
                break

            except _HTTPError as e:
                # Some OpenAI-compatible servers reject stream_options even
                # though they otherwise support streaming chat completions.
                if (
                    e.status_code == 400
                    and "stream_options" in e.body.lower()
                    and "stream_options" in body
                ):
                    body.pop("stream_options", None)
                    logger.info(
                        "Provider %s rejected stream_options; retrying without it",
                        provider.get("id") or api_model,
                    )
                    continue

                # ── Image-rejection recovery ──────────────────────────────
                # If the text-only model rejects messages containing image
                # content (shouldn't happen since we pre-enrich, but safety
                # net for edge cases), strip images and retry.
                # Ported from hermes-agent/agent/conversation_loop.py.
                from agent.image_routing import is_image_rejection_error, strip_images_from_messages
                if is_image_rejection_error(e.body, e.status_code):
                    stripped = strip_images_from_messages(conversation)
                    if stripped:
                        sanitized = _sanitize_messages(
                            conversation, strip_images=True
                        )
                        body["messages"] = sanitized
                        logger.warning(
                            "Model %s rejected image content — stripped images, retrying",
                            model_id,
                        )
                        continue

                adjusted_max_tokens, overflow_budget = _retry_max_tokens_from_context_overflow(
                    e.body,
                    int(body.get("max_tokens") or 0),
                )
                if adjusted_max_tokens is not None:
                    body["max_tokens"] = adjusted_max_tokens
                    logger.warning(
                        "Provider reported context overflow; retrying model=%s context_length=%s prompt_tokens=%s requested=%s effective=%s safety_margin=%s",
                        provider.get("id") or api_model,
                        overflow_budget.get("context_length"),
                        overflow_budget.get("prompt_tokens"),
                        overflow_budget.get("requested_max_tokens"),
                        overflow_budget.get("effective_max_tokens"),
                        overflow_budget.get("safety_margin"),
                    )
                    continue

                classified = classify_api_error(e, provider=model_id, model=api_model)
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s — %s",
                    attempt, MAX_RETRIES, classified.reason.value, classified.message[:200],
                )
                if (
                    (not classified.retryable or attempt >= MAX_RETRIES)
                    and not full_content
                    and provider_cursor + 1 < len(provider_chain)
                ):
                    previous = provider_chain[provider_cursor]
                    provider_cursor += 1
                    provider = provider_chain[provider_cursor]
                    base_url, api_model, api_key, protocol, headers = apply_provider(provider)
                    tool_context = ToolContext(
                        user_id=user_id,
                        session_id=session_id,
                        model_id=str(provider.get("id") or api_model),
                        provider_config=provider,
                        fallback_configs=tuple(provider_chain[provider_cursor + 1:]),
                        enabled_tools=tuple(tools),
                        source=source,
                        enabled_user_skills=tuple(enabled_user_skills or []),
                        run_id=run_id,
                        root_run_id=root_run_id or run_id,
                        parent_run_id=parent_run_id,
                        agent_kind=agent_kind,
                        agent_name=agent_name,
                        depth=depth,
                        workspace_scope=workspace_scope,
                        event_sink=event_sink,
                    )
                    yield await emit_agent_event("model.switch", {
                        "from_model": previous.get("id") or model_id,
                        "to_model": provider.get("id") or api_model,
                        "reason": classified.reason.value,
                    })
                    yield {
                        "type": "model_switch",
                        "from_model": previous.get("id") or model_id,
                        "to_model": provider.get("id") or api_model,
                        "reason": classified.reason.value,
                    }
                    fallback_requested = True
                    break
                if not classified.retryable or attempt >= MAX_RETRIES:
                    msg = f"LLM error: {classified.reason.value} — {classified.message[:300]}"
                    yield await emit_agent_event("run.failed", {"error": msg})
                    yield {
                        "type": "error",
                        "msg": msg,
                    }
                    return
                delay = jittered_backoff(attempt)
                await asyncio.sleep(delay)

            except (asyncio.TimeoutError, httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                logger.warning(
                    "LLM transport error (attempt %d/%d): %s", attempt, MAX_RETRIES, e,
                )
                if (
                    attempt >= MAX_RETRIES
                    and not full_content
                    and provider_cursor + 1 < len(provider_chain)
                ):
                    previous = provider_chain[provider_cursor]
                    provider_cursor += 1
                    provider = provider_chain[provider_cursor]
                    base_url, api_model, api_key, protocol, headers = apply_provider(provider)
                    tool_context = ToolContext(
                        user_id=user_id,
                        session_id=session_id,
                        model_id=str(provider.get("id") or api_model),
                        provider_config=provider,
                        fallback_configs=tuple(provider_chain[provider_cursor + 1:]),
                        enabled_tools=tuple(tools),
                        source=source,
                        enabled_user_skills=tuple(enabled_user_skills or []),
                        run_id=run_id,
                        root_run_id=root_run_id or run_id,
                        parent_run_id=parent_run_id,
                        agent_kind=agent_kind,
                        agent_name=agent_name,
                        depth=depth,
                        workspace_scope=workspace_scope,
                        event_sink=event_sink,
                    )
                    yield await emit_agent_event("model.switch", {
                        "from_model": previous.get("id") or model_id,
                        "to_model": provider.get("id") or api_model,
                        "reason": "transport_error",
                    })
                    yield {
                        "type": "model_switch",
                        "from_model": previous.get("id") or model_id,
                        "to_model": provider.get("id") or api_model,
                        "reason": "transport_error",
                    }
                    fallback_requested = True
                    break
                if attempt >= MAX_RETRIES:
                    msg = f"LLM transport error after {MAX_RETRIES} attempts: {type(e).__name__}: {e}"
                    yield await emit_agent_event("run.failed", {"error": msg})
                    yield {
                        "type": "error",
                        "msg": msg,
                    }
                    return
                delay = jittered_backoff(attempt)
                await asyncio.sleep(delay)

            except Exception as e:
                logger.exception("Unexpected error streaming LLM (attempt %d): %s", attempt, e)
                if attempt >= MAX_RETRIES:
                    msg = f"LLM error: {type(e).__name__}: {e}"
                    yield await emit_agent_event("run.failed", {"error": msg})
                    yield {"type": "error", "msg": msg}
                    return
                delay = jittered_backoff(attempt)
                await asyncio.sleep(delay)

        if fallback_requested:
            continue

        # ── Determine next action based on finish_reason ──────────────
        if finish_reason is None:
            finish_reason = "stop"
        for debug_evt in await debug_stream_event("llm.finish", {
            "iteration": budget.used,
            "finish_reason": finish_reason,
            "content_chars": len(full_content),
            "reasoning_chars": len(full_reasoning),
            "tool_call_fragment_count": len(tool_call_fragments),
            "api_usage": api_usage,
            "provider_model": provider.get("id") or model_id,
        }):
            yield debug_evt

        # Feed token usage to context compressor
        if api_usage:
            compressor.update_from_response(api_usage)
            run_usage["input_tokens"] += int(api_usage.get("prompt_tokens", 0) or 0)
            run_usage["output_tokens"] += int(api_usage.get("completion_tokens", 0) or 0)
            run_usage["total_tokens"] += int(api_usage.get("total_tokens", 0) or 0)
        elif full_content or full_reasoning:
            estimated_input = max(1, len(json.dumps(sanitized, ensure_ascii=False)) // 4)
            estimated_output = max(1, (len(full_content) + len(full_reasoning)) // 4)
            run_usage["input_tokens"] += estimated_input
            run_usage["output_tokens"] += estimated_output
            run_usage["total_tokens"] += estimated_input + estimated_output

        if finish_reason == "stop":
            unviewed_session_skills = run_state.unviewed_session_skills()
            if (
                unviewed_session_skills
                and skill_enforcement_continuations < 1
                and (
                    _looks_like_complex_artifact_request(original_user_text)
                    or run_state.tool_call_count > 0
                    or len(full_content.strip()) < 200
                )
            ):
                skill_enforcement_continuations += 1
                skill_list = ", ".join(sorted(unviewed_session_skills))
                for debug_evt in await debug_stream_event("gate.continuation", {
                    "gate": "skill_enforcement",
                    "reason": "session skill available but not loaded",
                    "skills": sorted(unviewed_session_skills),
                    "continuation_count": skill_enforcement_continuations,
                }):
                    yield debug_evt
                conversation.append({
                    "role": "assistant",
                    "content": full_content or "(No visible response.)",
                })
                conversation.append({
                    "role": "user",
                    "content": (
                        "A session-level skill is available for this conversation but has not been loaded: "
                        f"{skill_list}. If any listed skill is relevant to the user's task, call "
                        "skill_view(name) before continuing, then follow the skill workflow. "
                        "If none is relevant, state the concrete reason and continue."
                    ),
                })
                yield {
                    "type": "tool_progress",
                    "msg": f"↻ Loading relevant session skill before continuing — {skill_list}",
                }
                yield {"type": "delta", "content": "\n\n"}
                continue

            needs_artifact_gate = _needs_complex_artifact_gate(run_state, original_user_text, full_content)
            needs_workflow_gate, workflow_reason = run_state.needs_more_skill_workflow()
            if (
                needs_workflow_gate
                and workflow_reason_retryable(workflow_reason)
                and skill_workflow_continuations < max_skill_workflow_continuations
            ):
                skill_workflow_continuations += 1
                for debug_evt in await debug_stream_event("gate.continuation", {
                    "gate": "skill_workflow_before_finish",
                    "reason": workflow_reason,
                    "continuation_count": skill_workflow_continuations,
                    "max_continuations": max_skill_workflow_continuations,
                    "largest_write_size": max(run_state.successful_write_sizes or [0]),
                }):
                    yield debug_evt
                conversation.append({
                    "role": "assistant",
                    "content": full_content or "(No visible response.)",
                })
                queue_skill_workflow_continuation(workflow_reason)
                yield {
                    "type": "tool_progress",
                    "msg": f"↻ Inspecting session skill workflow before finishing — {workflow_reason}",
                }
                yield {"type": "delta", "content": "\n\n"}
                continue
            if needs_artifact_gate and artifact_enforcement_continuations < max_artifact_enforcement_continuations:
                largest_write = max(run_state.successful_write_sizes or [0])
                invalid_placeholder_write = run_state.invalid_placeholder_write_count > 0
                if True:
                    artifact_enforcement_continuations += 1
                    for debug_evt in await debug_stream_event("gate.continuation", {
                        "gate": "artifact_enforcement",
                        "reason": "complex artifact incomplete or tool failures require follow-up",
                        "continuation_count": artifact_enforcement_continuations,
                        "largest_write_size": largest_write,
                        "tool_error_count": run_state.tool_error_count,
                        "invalid_placeholder_write": invalid_placeholder_write,
                        "visible_content_chars": len(full_content.strip()),
                    }):
                        yield debug_evt
                    conversation.append({
                        "role": "assistant",
                        "content": full_content or "(No visible response.)",
                    })
                    continuation_reason = (
                        "A file write was rejected because it contained a compacted conversation-history placeholder. "
                        if invalid_placeholder_write else ""
                    )
                    conversation.append({
                        "role": "user",
                        "content": (
                            continuation_reason +
                            "The task uses a session-level skill and asks for a complex deliverable. "
                            "Continue to complete the artifact using the loaded skill workflow and any "
                            "relevant linked files. If tool calls failed validation, retry with valid schema "
                            "arguments and real generated content. Produce one coherent final artifact with the "
                            "required major sections; do not stop at a short summary, placeholder, or incomplete file."
                        ),
                    })
                    yield {
                        "type": "tool_progress",
                        "msg": "↻ Completing session-skill artifact before finishing",
                    }
                    yield {"type": "delta", "content": "\n\n"}
                    continue

            if (
                action_promise_continuations < 1
                and _looks_like_unfulfilled_action_promise(full_content)
                and (
                    _looks_like_complex_artifact_request(original_user_text)
                    or run_state.tool_error_count > 0
                    or run_state.viewed_skill_names
                    or run_state.tool_call_count > 0
                )
            ):
                action_promise_continuations += 1
                for debug_evt in await debug_stream_event("gate.continuation", {
                    "gate": "action_promise",
                    "reason": "assistant promised a concrete next action without completing it",
                    "continuation_count": action_promise_continuations,
                    "visible_content_tail": full_content[-700:],
                    "tool_error_count": run_state.tool_error_count,
                    "tool_call_count": run_state.tool_call_count,
                }):
                    yield debug_evt
                conversation.append({
                    "role": "assistant",
                    "content": full_content or "(No visible response.)",
                })
                conversation.append({
                    "role": "user",
                    "content": (
                        "Your last response ended by promising a concrete next action, but no tool call or final deliverable followed. "
                        "Do not stop after describing the next step. Continue now: use the needed tool call, write the requested artifact, "
                        "or provide the completed final answer with concrete evidence."
                    ),
                })
                yield {
                    "type": "tool_progress",
                    "msg": "↻ Continuing after an unfinished action promise",
                }
                yield {"type": "delta", "content": "\n\n"}
                continue

            current_goal = await _fetch_goal(user_id, session_id)
            if current_goal and current_goal.get("status") == "active":
                token_budget = int(current_goal.get("token_budget") or 0)
                tokens_used = int(current_goal.get("tokens_used") or 0)
                if token_budget and tokens_used + run_usage["total_tokens"] >= token_budget:
                    await _set_goal_status(
                        user_id,
                        session_id,
                        "budget_limited",
                        "Goal token budget reached.",
                    )
                    yield {
                        "type": "tool_progress",
                        "msg": "⏸ Goal paused — token budget reached.",
                    }
                    yield await emit_agent_event("usage.updated", {
                        **run_usage,
                        "model": provider.get("id") or api_model,
                    })
                    yield {
                        "type": "usage",
                        **run_usage,
                        "model": provider.get("id") or api_model,
                    }
                    yield await emit_agent_event("run.completed", {
                        "finish_reason": "stop",
                        "usage": run_usage,
                    })
                    yield {"type": "done", "finish_reason": "stop"}
                    return
                verdict, reason, parse_failed = await _judge_goal(
                    str(current_goal.get("objective") or ""),
                    full_content,
                )
                if parse_failed:
                    goal_parse_failures += 1
                else:
                    goal_parse_failures = 0
                if verdict == "complete":
                    await _set_goal_status(
                        user_id, session_id, "complete", reason,
                    )
                    yield {
                        "type": "tool_progress",
                        "msg": f"🎯 Goal complete — {reason}",
                    }
                elif verdict == "blocked":
                    await _set_goal_status(
                        user_id, session_id, "blocked", reason,
                    )
                    yield {
                        "type": "tool_progress",
                        "msg": f"⛔ Goal blocked — {reason}",
                    }
                elif goal_parse_failures >= settings.goal_max_parse_failures:
                    await _set_goal_status(
                        user_id,
                        session_id,
                        "pause",
                        "Goal judge repeatedly returned invalid output.",
                    )
                    yield {
                        "type": "tool_progress",
                        "msg": "⏸ Goal paused — judge output could not be parsed.",
                    }
                elif goal_continuations < max(0, settings.goal_max_continuations):
                    goal_continuations += 1
                    conversation.append({
                        "role": "assistant",
                        "content": full_content or "(No visible response.)",
                    })
                    conversation.append({
                        "role": "user",
                        "content": (
                            "[Continuing toward your standing goal]\n"
                            f"Goal: {current_goal['objective']}\n\n"
                            f"Judge feedback: {reason}\n\n"
                            "Continue working toward this goal. Take the next "
                            "concrete step, use tools when needed, and verify the "
                            "result. If complete, state concrete evidence. If "
                            "blocked and user input is required, say so clearly."
                        ),
                    })
                    yield {
                        "type": "tool_progress",
                        "msg": (
                            f"↻ Continuing toward goal "
                            f"({goal_continuations}/{settings.goal_max_continuations})"
                            f" — {reason}"
                        ),
                    }
                    yield {"type": "delta", "content": "\n\n"}
                    continue
                else:
                    await _set_goal_status(
                        user_id,
                        session_id,
                        "pause",
                        "Automatic continuation budget reached.",
                    )
                    yield {
                        "type": "tool_progress",
                        "msg": (
                            "⏸ Goal paused — automatic continuation budget reached."
                        ),
                    }
            if (
                run_state.tool_error_count > 0
                and run_state.last_tool_error_at > run_state.last_successful_artifact_at
                and tool_failure_continuations < 2
                and (
                    _looks_like_complex_artifact_request(original_user_text)
                    or _looks_like_file_artifact_request(original_user_text)
                    or run_state.viewed_skill_names
                )
            ):
                tool_failure_continuations += 1
                for debug_evt in await debug_stream_event("gate.continuation", {
                    "gate": "tool_failure_recovery",
                    "reason": "tool failures remain before completing a complex or artifact workflow",
                    "continuation_count": tool_failure_continuations,
                    "tool_error_count": run_state.tool_error_count,
                    "parse_failure_count": run_state.parse_failure_count,
                    "schema_failure_count": run_state.schema_failure_count,
                    "invalid_placeholder_write_count": run_state.invalid_placeholder_write_count,
                }):
                    yield debug_evt
                conversation.append({
                    "role": "assistant",
                    "content": full_content or "(No visible response.)",
                })
                conversation.append({
                    "role": "user",
                    "content": (
                        "One or more required tool calls failed before this complex/artifact workflow was complete. "
                        "Do not finish yet. Read the JSON tool results already in context, fix the concrete cause, "
                        "and retry with the appropriate tool. If execute_code was rerouted to managed runtime, inspect "
                        "stdout/stderr and continue from that result. If run_skill_python failed, fix the script path, "
                        "cwd, imports, args, or output location. If write_file failed, regenerate real content rather "
                        "than copying any omitted-history marker. Only stop when the failed step is resolved or you can "
                        "state a concrete external blocker."
                    ),
                })
                yield {
                    "type": "tool_progress",
                    "msg": "↻ Recovering failed tool step before finishing",
                }
                yield {"type": "delta", "content": "\n\n"}
                continue

            verifier_payload = _deterministic_verifier_payload(
                run_state,
                requested_artifact=_looks_like_file_artifact_request(original_user_text),
                complex_report=_complex_report_context(run_state, original_user_text),
            )
            if verifier_payload is not None:
                yield await emit_agent_event("verifier.requested", {
                    "verifier_kind": verifier_payload["verifier_kind"],
                    "target_run_id": run_id,
                    "target_artifacts": verifier_payload.get("target_artifacts", []),
                })
                yield await emit_agent_event("verifier.completed", {
                    **verifier_payload,
                    "target_run_id": run_id,
                })
                if verifier_payload["needs_more_work"] and verifier_continuations < max_verifier_continuations:
                    verifier_continuations += 1
                    conversation.append({
                        "role": "assistant",
                        "content": full_content or "(No visible response.)",
                    })
                    conversation.append({
                        "role": "user",
                        "content": (
                            f"[Verifier requested follow-up {verifier_continuations}/{max_verifier_continuations}]\n"
                            f"Verifier kind: {verifier_payload['verifier_kind']}\n"
                            f"Verdict: {verifier_payload['verdict']}\n"
                            f"Reason: {verifier_payload['reason']}\n\n"
                            "Continue to resolve every concrete verifier finding. If a file write failed, retry with valid schema and real content. If an artifact is empty, rewrite it with substantive content. If a README/checklist link or pending marker is wrong, patch the affected Markdown artifact. If a Markdown report is structurally thin, expand it by preserving worker outputs, adding appendices, mapping executed tool/script/search evidence to report sections, and citing sources; do not pad with generic prose. For long Markdown files, prefer appending well-labeled sections or replacing exact short anchors over fragile large exact patches. If the finding is already resolved, explain the evidence briefly."
                        ),
                    })
                    yield {
                        "type": "tool_progress",
                        "msg": f"↻ Verifier requested follow-up — {verifier_payload['reason']}",
                    }
                    yield {"type": "delta", "content": "\n\n"}
                    continue
            yield await emit_agent_event("usage.updated", {
                **run_usage,
                "model": provider.get("id") or api_model,
            })
            yield {
                "type": "usage",
                **run_usage,
                "model": provider.get("id") or api_model,
            }
            yield await emit_agent_event("run.completed", {
                "finish_reason": "stop",
                "usage": run_usage,
            })
            yield {"type": "done", "finish_reason": "stop"}
            return

        if finish_reason == "tool_calls" and tool_call_fragments:
            # Assemble tool calls from accumulated fragments
            sorted_frags = sorted(tool_call_fragments.items())
            assembled_calls = []
            for _, frag in sorted_frags:
                assembled_calls.append(
                    build_tool_call(
                        id=frag["id"],
                        name=frag["name"],
                        arguments=_safe_parse_args(frag["arguments"]),
                    )
                )

            # Build the assistant message with tool_calls
            assistant_msg = {
                "role": "assistant",
                "content": full_content or None,
                "tool_calls": [
                    {
                        "id": tc.id or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": _compact_tool_call_arguments(tc.name, tc.arguments),
                        },
                    }
                    for i, tc in enumerate(assembled_calls)
                ],
            }
            conversation.append(assistant_msg)

            # Execute each tool and append results
            for tc in assembled_calls:
                run_state.tool_call_count += 1
                args_summary = tc.arguments[:80] if tc.arguments else "{}"
                display_tool_name = tc.name
                yield await emit_agent_event("tool.started", {
                    "tool_name": tc.name,
                    "tool_call_id": tc.id or "",
                    "args_compacted": _compact_tool_call_arguments(tc.name, tc.arguments),
                }, tool_name=tc.name, tool_call_id=tc.id or "")
                yield {
                    "type": "tool_progress",
                    "msg": f"🔧 {tc.name}({args_summary})",
                }

                args = _safe_parse_args(tc.arguments or "")
                if isinstance(args, dict) and "__tool_arg_parse_error" in args:
                    run_state.parse_failure_count += 1
                    run_state.last_parse_failure_at = run_state.tool_call_count
                executed_args = args
                for debug_evt in await debug_stream_event("tool.call", {
                    "tool_name": tc.name,
                    "tool_call_id": tc.id or "",
                    "arguments": _compact_tool_call_arguments(tc.name, tc.arguments),
                    "parse_error": args.get("__tool_arg_parse_error") if isinstance(args, dict) else None,
                }, tool_name=tc.name, tool_call_id=tc.id or ""):
                    yield debug_evt

                if tc.name == "tool_search" and deferred_catalog is not None:
                    result = json.dumps(
                        deferred_catalog.search(
                            str(args.get("query", "")),
                            int(args.get("limit", 5)),
                        ),
                        ensure_ascii=False,
                    )
                elif tc.name == "tool_describe" and deferred_catalog is not None:
                    result = json.dumps(
                        deferred_catalog.describe(str(args.get("name", ""))),
                        ensure_ascii=False,
                    )
                elif tc.name == "tool_call" and deferred_catalog is not None:
                    display_tool_name = str(args.get("name", ""))
                    actual_args = args.get("arguments")
                    if not isinstance(actual_args, dict):
                        actual_args = {}
                    executed_args = actual_args
                    if display_tool_name not in deferred_catalog.definitions:
                        result = json.dumps({
                            "error": f"Deferred tool not found: {display_tool_name}"
                        })
                    elif display_tool_name in ("mcp_server_list", "mcp_server_status"):
                        result = await dispatch(
                            display_tool_name,
                            actual_args,
                            context=tool_context,
                        )
                    elif display_tool_name.startswith("mcp_"):
                        from tools.mcp_client import dispatch_mcp_tool
                        result = await dispatch_mcp_tool(
                            display_tool_name,
                            actual_args,
                            user_id=user_id,
                            session_id=session_id,
                            enabled_user_skills=list(tool_context.enabled_user_skills),
                        )
                    else:
                        result = await dispatch(
                            display_tool_name,
                            actual_args,
                            context=tool_context,
                        )
                elif tc.name in ("mcp_server_list", "mcp_server_status"):
                    result = await dispatch(
                        tc.name,
                        args,
                        context=tool_context,
                    )
                elif tc.name.startswith("mcp_"):
                    from tools.mcp_client import dispatch_mcp_tool
                    result = await dispatch_mcp_tool(
                        tc.name,
                        args,
                        user_id=user_id,
                        session_id=session_id,
                        enabled_user_skills=list(tool_context.enabled_user_skills),
                    )
                else:
                    result = await dispatch(
                        tc.name,
                        args,
                        context=tool_context,
                    )
                hint = hint_tracker.check(args)
                if hint:
                    result = str(result) + "\n\n" + hint
                outcome, outcome_detail = _tool_outcome_summary(str(result))
                if outcome != "success":
                    run_state.tool_error_count += 1
                    run_state.last_tool_error_at = run_state.tool_call_count
                    result_data = _json_object(str(result))
                    if isinstance(result_data, dict) and result_data.get("reason") == "invalid_placeholder_content":
                        run_state.invalid_placeholder_write_count += 1
                    if isinstance(executed_args, dict) and "__tool_arg_parse_error" in executed_args:
                        run_state.parse_failure_count += 1
                        run_state.last_parse_failure_at = run_state.tool_call_count
                    if "schema" in outcome_detail.lower() or "required field" in outcome_detail.lower():
                        run_state.schema_failure_count += 1
                        run_state.last_schema_failure_at = run_state.tool_call_count
                elif display_tool_name == "write_file":
                    written_size = _tool_result_size(str(result))
                    if written_size is not None:
                        run_state.successful_write_sizes.append(written_size)
                if outcome == "success":
                    if display_tool_name == "web_search":
                        run_state.successful_search_count += 1
                    elif display_tool_name == "web_extract":
                        run_state.successful_extract_count += 1
                    elif display_tool_name == "execute_code":
                        run_state.successful_code_execution_count += 1
                    elif display_tool_name == "run_skill_python":
                        run_state.successful_skill_python_count += 1
                    elif display_tool_name.startswith("mcp_"):
                        run_state.successful_mcp_tool_count += 1
                artifact_payloads = _artifact_payloads_from_tool_result(display_tool_name, str(result)) if outcome == "success" else []
                if artifact_payloads:
                    run_state.artifacts.extend(artifact_payloads)
                    run_state.last_successful_artifact_at = run_state.tool_call_count
                    for artifact_payload in artifact_payloads:
                        size = artifact_payload.get("size_bytes")
                        if isinstance(size, int) and size >= 0:
                            run_state.successful_write_sizes.append(size)
                if display_tool_name == "skill_view" and outcome == "success" and isinstance(executed_args, dict):
                    run_state.record_skill_view(executed_args, _json_object(str(result)))
                logger.info(
                    "Tool completed user=%s session=%s tool=%s outcome=%s detail=%s",
                    user_id, session_id, display_tool_name, outcome, outcome_detail[:300],
                )
                yield await emit_agent_event(
                    "tool.completed" if outcome == "success" else "tool.failed",
                    {
                        "tool_name": display_tool_name,
                        "tool_call_id": tc.id or "",
                        "outcome": outcome,
                        "detail": outcome_detail[:1000],
                    },
                    tool_name=display_tool_name,
                    tool_call_id=tc.id or "",
                )
                for debug_evt in await debug_stream_event(
                    "tool.result",
                    {
                        "tool_name": display_tool_name,
                        "original_tool_name": tc.name,
                        "tool_call_id": tc.id or "",
                        "outcome": outcome,
                        "detail": outcome_detail[:1000],
                        "result": _tool_debug_result(str(result)),
                    },
                    tool_name=display_tool_name,
                    tool_call_id=tc.id or "",
                ):
                    yield debug_evt
                for artifact_payload in artifact_payloads:
                    yield await emit_agent_event(
                        "artifact.created",
                        {
                            **artifact_payload,
                            "source_tool_name": display_tool_name,
                            "source_tool_call_id": tc.id or "",
                        },
                        tool_name=display_tool_name,
                        tool_call_id=tc.id or "",
                    )
                marker = "✅" if outcome == "success" else "⚠️"
                progress = f"{marker} {display_tool_name}: {outcome}"
                if outcome_detail:
                    progress += f" — {outcome_detail[:240]}"
                yield {"type": "tool_progress", "msg": progress}
                wrapped = wrap_result(str(result), display_tool_name, user_id=user_id, session_id=session_id)
                conversation.append({
                    "role": "tool",
                    "tool_call_id": tc.id or f"call_unknown",
                    "content": wrapped,
                })

            needs_workflow_gate_after_tools, workflow_reason_after_tools = run_state.needs_more_skill_workflow()
            if (
                needs_workflow_gate_after_tools
                and workflow_reason_retryable(workflow_reason_after_tools)
                and skill_workflow_continuations < max_skill_workflow_continuations
            ):
                skill_workflow_continuations += 1
                queue_skill_workflow_continuation(workflow_reason_after_tools)
                for debug_evt in await debug_stream_event("gate.continuation", {
                    "gate": "skill_workflow_after_tools",
                    "reason": workflow_reason_after_tools,
                    "continuation_count": skill_workflow_continuations,
                    "max_continuations": max_skill_workflow_continuations,
                    "largest_write_size": max(run_state.successful_write_sizes or [0]),
                }):
                    yield debug_evt
                yield {
                    "type": "tool_progress",
                    "msg": f"↻ Inspecting session skill workflow before continuing — {workflow_reason_after_tools}",
                }

            # ── Context compression check ────────────────────────────
            if compressor.should_compress():
                logger.info("Context compression triggered (prompt_tokens=%d threshold=%d)",
                           compressor.last_prompt_tokens, compressor.threshold_tokens)
                conversation = await compressor.compress(conversation)

            # Continue the loop — LLM will process tool results
            continue

        if finish_reason == "length":
            for debug_evt in await debug_stream_event("gate.continuation", {
                "gate": "length_continuation",
                "reason": "model hit max output tokens",
                "iteration": budget.used,
                "effective_max_tokens": effective_max_tokens,
                "content_chars": len(full_content),
                "reasoning_chars": len(full_reasoning),
            }):
                yield debug_evt
            # Inject continuation prompt and loop
            conversation.append({
                "role": "user",
                "content": "Please continue from where you left off. Complete your response.",
            })

            # ── Context compression check ────────────────────────────
            if compressor.should_compress():
                logger.info("Context compression triggered (prompt_tokens=%d threshold=%d)",
                           compressor.last_prompt_tokens, compressor.threshold_tokens)
                conversation = await compressor.compress(conversation)

            continue

        # content_filter or other — treat as stop
        yield await emit_agent_event("usage.updated", {
            **run_usage,
            "model": provider.get("id") or api_model,
        })
        yield {
            "type": "usage",
            **run_usage,
            "model": provider.get("id") or api_model,
        }
        yield await emit_agent_event("run.completed", {
            "finish_reason": finish_reason,
            "usage": run_usage,
        })
        yield {"type": "done", "finish_reason": finish_reason}
        return

    msg = f"Agent iteration budget exhausted after {budget.used} iterations."
    exhausted_verifier_payload = _deterministic_verifier_payload(
        run_state,
        requested_artifact=_looks_like_file_artifact_request(original_user_text),
        complex_report=_complex_report_context(run_state, original_user_text),
    )
    for debug_evt in await debug_stream_event("budget.exhausted", {
        "used": budget.used,
        "max_iterations": budget.max_total,
        "remaining": budget.remaining,
        "tool_call_count": run_state.tool_call_count,
        "tool_error_count": run_state.tool_error_count,
        "parse_failure_count": run_state.parse_failure_count,
        "schema_failure_count": run_state.schema_failure_count,
        "successful_write_sizes": run_state.successful_write_sizes[-10:],
        "viewed_skill_names": sorted(run_state.viewed_skill_names),
        "continuation_reasons_tail": run_state.continuation_reasons[-10:],
        "verifier": exhausted_verifier_payload,
    }):
        yield debug_evt
    if exhausted_verifier_payload is not None:
        yield await emit_agent_event("verifier.completed", {
            **exhausted_verifier_payload,
            "target_run_id": run_id,
            "terminal_reason": "iteration_budget_exhausted",
        })
    yield await emit_agent_event("run.failed", {"error": msg, "usage": run_usage, "verifier": exhausted_verifier_payload})
    yield {"type": "error", "msg": msg}


async def _fetch_goal(user_id: str, session_id: str) -> dict | None:
    if not user_id or not session_id or session_id == "default":
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{settings.backend_internal_url.rstrip('/')}/internal/sessions/{session_id}/goal",
                params={"user_id": user_id},
                headers={"X-Internal-Token": settings.internal_api_token},
            )
        if response.status_code == 200:
            data = response.json()
            return data if data.get("objective") else None
    except Exception:
        logger.debug("Goal lookup failed for session=%s", session_id, exc_info=True)
    return None


async def _set_goal_status(
    user_id: str,
    session_id: str,
    action: str,
    note: str,
) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{settings.backend_internal_url.rstrip('/')}/internal/sessions/{session_id}/goal",
                params={"user_id": user_id},
                headers={"X-Internal-Token": settings.internal_api_token},
                json={"action": action, "note": note[:1000]},
            )
    except Exception:
        logger.debug(
            "Goal status update failed session=%s action=%s",
            session_id, action, exc_info=True,
        )


async def _judge_goal(
    objective: str,
    last_response: str,
) -> tuple[str, str, bool]:
    """Return (complete|blocked|continue, reason, parse_failed)."""
    if not objective.strip():
        return "continue", "empty goal", False
    if not last_response.strip():
        return "continue", "the agent produced no substantive response", False
    judge_cfg = PROVIDERS.get(
        settings.compressor_model,
        PROVIDERS.get("qwen3_5"),
    )
    if not judge_cfg:
        return "continue", "goal judge model is unavailable", False
    system_prompt = (
        "You are a strict completion judge for an autonomous agent. Decide "
        "whether the latest response proves the user's durable goal is complete, "
        "is genuinely blocked on user/external input, or still needs work. "
        "Generic claims are not evidence. Reply with one JSON object only: "
        '{"status":"complete|blocked|continue","reason":"one sentence"}'
    )
    user_prompt = (
        f"Goal:\n{objective[:2000]}\n\n"
        f"Latest agent response:\n{last_response[-4000:]}\n\n"
        "Judge the current state."
    )
    headers = {"Content-Type": "application/json"}
    key = judge_cfg.get("api_key")
    if key and key != "EMPTY":
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                f"{str(judge_cfg['base_url']).rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": judge_cfg["api_model"],
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 180,
                    "stream": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
        response.raise_for_status()
        raw = ""
        resp_data = response.json()
        choices = resp_data.get("choices")
        if choices:
            raw = choices[0].get("message", {}).get("content", "")
    except Exception as exc:
        logger.info("Goal judge failed: %s", exc)
        return "continue", f"goal judge unavailable: {type(exc).__name__}", False
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start:end + 1]) if start >= 0 and end > start else None
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        return "continue", "goal judge reply was not valid JSON", True
    status = str(data.get("status") or "").strip().lower()
    if not status and "done" in data:
        status = "complete" if bool(data.get("done")) else "continue"
    if status not in {"complete", "blocked", "continue"}:
        return "continue", "goal judge returned an unknown status", True
    reason = str(data.get("reason") or "no reason provided").strip()
    return status, reason[:500], False


def _build_provider_request(
    *,
    base_url: str,
    protocol: str,
    body: dict,
) -> tuple[str, dict]:
    if protocol != "anthropic":
        return f"{base_url.rstrip('/')}/chat/completions", body

    system_parts: list[str] = []
    converted: list[dict] = []

    def append_message(role: str, content: Any) -> None:
        if converted and converted[-1]["role"] == role:
            existing = converted[-1]["content"]
            if not isinstance(existing, list):
                existing = [{"type": "text", "text": str(existing)}]
            incoming = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
            converted[-1]["content"] = existing + incoming
        else:
            converted.append({"role": role, "content": content})

    for message in body.get("messages", []):
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if content:
                system_parts.append(str(content))
            continue
        if role == "assistant":
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for tool_call in message.get("tool_calls") or []:
                fn = tool_call.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    parsed_args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tool_call.get("id") or "tool_call",
                    "name": fn.get("name") or "unknown",
                    "input": parsed_args if isinstance(parsed_args, dict) else {},
                })
            append_message("assistant", blocks or [{"type": "text", "text": ""}])
            continue
        if role == "tool":
            append_message("user", [{
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id") or "tool_call",
                "content": str(content or ""),
            }])
            continue
        if isinstance(content, list):
            blocks = []
            for part in content:
                if not isinstance(part, dict):
                    blocks.append({"type": "text", "text": str(part)})
                elif part.get("type") == "text":
                    blocks.append({"type": "text", "text": str(part.get("text", ""))})
                elif part.get("type") == "image_url":
                    image = part.get("image_url") or {}
                    url = image.get("url", "") if isinstance(image, dict) else str(image)
                    if url.startswith("data:") and ";base64," in url:
                        header, data = url.split(",", 1)
                        media_type = header[5:].split(";", 1)[0] or "image/png"
                        blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        })
                    elif url:
                        blocks.append({
                            "type": "image",
                            "source": {"type": "url", "url": url},
                        })
            append_message("user", blocks or [{"type": "text", "text": ""}])
        else:
            append_message("user", str(content or ""))

    anthropic_tools = []
    for tool in body.get("tools") or []:
        fn = tool.get("function") or {}
        anthropic_tools.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    request_body = {
        "model": body["model"],
        "messages": converted,
        "max_tokens": body.get("max_tokens", DEFAULT_MAX_TOKENS),
        "temperature": body.get("temperature", 0.7),
        "stream": True,
    }
    if system_parts:
        request_body["system"] = "\n\n".join(system_parts)
    if anthropic_tools:
        request_body["tools"] = anthropic_tools
    return f"{base_url.rstrip('/')}/messages", request_body


async def _iter_provider_stream(
    response: httpx.Response,
    protocol: str,
) -> AsyncIterator[dict]:
    if protocol != "anthropic":
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            chunk = line[6:]
            if chunk == "[DONE]":
                break
            try:
                data = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            choices = data.get("choices")
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta", {})
            yield {
                "reasoning": delta.get("reasoning") or delta.get("reasoning_content") or "",
                "content": delta.get("content") or "",
                "tool_calls": delta.get("tool_calls") or [],
                "finish_reason": choice.get("finish_reason"),
                "usage": data.get("usage"),
            }
        return

    input_tokens = 0
    output_tokens = 0
    tool_blocks: dict[int, dict] = {}
    async for line in response.aiter_lines():
        if not line.startswith("data: "):
            continue
        raw = line[6:]
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "message_start":
            input_tokens = int((event.get("message", {}).get("usage") or {}).get("input_tokens", 0) or 0)
            yield {
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": 0,
                    "total_tokens": input_tokens,
                }
            }
        elif event_type == "content_block_start":
            index = int(event.get("index", 0))
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                tool_blocks[index] = {
                    "id": block.get("id"),
                    "name": block.get("name", ""),
                }
                yield {
                    "tool_calls": [{
                        "index": index,
                        "id": block.get("id"),
                        "function": {"name": block.get("name", ""), "arguments": ""},
                    }]
                }
            elif block.get("type") == "text" and block.get("text"):
                yield {"content": block.get("text", "")}
        elif event_type == "content_block_delta":
            index = int(event.get("index", 0))
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                yield {"content": delta.get("text", "")}
            elif delta_type == "thinking_delta":
                yield {"reasoning": delta.get("thinking", "")}
            elif delta_type == "input_json_delta":
                block = tool_blocks.get(index, {})
                yield {
                    "tool_calls": [{
                        "index": index,
                        "id": block.get("id"),
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": delta.get("partial_json", ""),
                        },
                    }]
                }
        elif event_type == "message_delta":
            output_tokens = int((event.get("usage") or {}).get("output_tokens", output_tokens) or 0)
            stop_reason = (event.get("delta") or {}).get("stop_reason")
            reason_map = {
                "end_turn": "stop",
                "stop_sequence": "stop",
                "tool_use": "tool_calls",
                "max_tokens": "length",
                "refusal": "content_filter",
            }
            yield {
                "finish_reason": reason_map.get(stop_reason, stop_reason),
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            }


async def _aiter_with_timeout(iterator: AsyncIterator[dict], *, timeout_seconds: float) -> AsyncIterator[dict]:
    queue: asyncio.Queue[tuple[bool, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, float(timeout_seconds))

    async def pump() -> None:
        try:
            async for item in iterator:
                await queue.put((True, item))
        except BaseException as exc:
            await queue.put((False, exc))
        else:
            await queue.put((False, None))

    task = asyncio.create_task(pump())
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            ok, value = await asyncio.wait_for(queue.get(), timeout=remaining)
            if ok:
                yield value
                continue
            if value is None:
                break
            raise value
    except asyncio.TimeoutError:
        task.cancel()
        raise
    finally:
        if not task.done():
            task.cancel()


def _tool_outcome_summary(raw: str) -> tuple[str, str]:
    """Return an auditable status line without exposing a large tool payload."""
    raw_text = str(raw or "").strip()
    raw_lower = raw_text.lower()
    if raw_lower.startswith("(web search timed out") or raw_lower.startswith("(web search failed"):
        return "error", raw_text.strip("()")
    if raw_lower.startswith("(failed to fetch "):
        return "error", raw_text.strip("()")
    data = _json_object(raw)
    if data is None:
        return "success", ""
    status = str(data.get("status", "")).lower()
    if status in {"error", "blocked", "timeout", "failed"}:
        detail = str(data.get("error") or data.get("message") or data.get("detail") or "")
        if not detail:
            stderr = str(data.get("stderr") or "").strip()
            stdout = str(data.get("stdout") or "").strip()
            if stderr:
                detail = stderr[-1000:]
            elif stdout:
                detail = stdout[-1000:]
            elif data.get("runtime_status"):
                detail = f"runtime_status={data.get('runtime_status')}"
        return status, detail
    if data.get("error"):
        return "error", str(data["error"])
    if data.get("success") is False:
        return "error", str(data.get("message") or data.get("detail") or "")
    return "success", ""


def _tool_result_size(raw: str) -> int | None:
    data = _json_object(raw)
    if data is None:
        return None
    size = data.get("size")
    return size if isinstance(size, int) and size >= 0 else None


def _artifact_payloads_from_tool_result(tool_name: str, raw: str) -> list[dict[str, Any]]:
    data = _json_object(raw)
    if data is None:
        return []
    if tool_name in {"write_file", "patch_file"}:
        if data.get("status") not in {"written", "patched"}:
            return []
        path = _safe_workspace_artifact_path(data.get("path"))
        if not path:
            return []
        payload = {
            "kind": "file",
            "title": Path(path).name,
            "path": path,
            "source_tool": tool_name,
        }
        size = data.get("size")
        if isinstance(size, int) and size >= 0:
            payload["size_bytes"] = size
        return [payload]
    if tool_name not in {"execute_code", "run_skill_python"}:
        return []
    if data.get("status") != "success":
        return []
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        path = _safe_workspace_artifact_path(item.get("path"))
        if not path or path in seen:
            continue
        seen.add(path)
        payload = {
            "kind": "file",
            "title": str(item.get("title") or Path(path).name),
            "path": path,
            "source_tool": tool_name,
        }
        size = item.get("size_bytes")
        if isinstance(size, int) and size >= 0:
            payload["size_bytes"] = size
        payloads.append(payload)
    return payloads


def _safe_workspace_artifact_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("workspace/"):
        text = text[len("workspace/"):]
    rel = PurePosixPath(text)
    if not text or rel.is_absolute() or ".." in rel.parts or any(part in {"", "."} for part in rel.parts):
        return None
    return str(rel)



def _make_workspace_artifact_readable(path: Path) -> None:
    try:
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.parent.chmod(0o755)
            path.chmod(0o644)
    except OSError:
        pass


def _markdown_artifact_findings(run_state: HarnessRunState, target_artifacts: list[Any]) -> list[str]:
    workspace = get_workspace(run_state.user_id, run_state.session_id)
    artifact_paths = [str(path) for path in target_artifacts if isinstance(path, str) and path]
    artifact_set = set(artifact_paths)
    findings: list[str] = []
    for rel_path in artifact_paths:
        if not rel_path.endswith(".md"):
            continue
        try:
            path = (workspace / rel_path).resolve()
            path.relative_to(workspace)
        except (OSError, ValueError):
            continue
        if not path.is_file() or path.is_symlink():
            findings.append(f"Artifact path was reported but is not readable in the workspace: {rel_path}.")
            continue
        try:
            _make_workspace_artifact_readable(path)
            content = path.read_text(encoding="utf-8", errors="replace")
        except PermissionError:
            findings.append(f"Artifact path was reported but is not readable by the harness/backend: {rel_path}.")
            continue
        except OSError:
            findings.append(f"Artifact path was reported but could not be read: {rel_path}.")
            continue
        if "__CHATDS_OMITTED" in content or "REGENERATE_OR_READ_SOURCE" in content:
            findings.append(f"Artifact contains an omitted-history placeholder: {rel_path}.")
        pending_markers = ("⏳", "待执行", "TODO", "TBD")
        if path.name.lower() in {"readme.md", "_checklist.md", "checklist.md"} and any(marker in content for marker in pending_markers):
            findings.append(f"Artifact still contains pending checklist/index markers: {rel_path}.")
        for match in re.finditer(r"\[[^\]]+\]\(([^)]+\.md)\)", content):
            linked = match.group(1).split("#", 1)[0].strip()
            if not linked or re.match(r"^[a-z][a-z0-9+.-]*:", linked, re.I):
                continue
            linked_rel = PurePosixPath(linked)
            if linked_rel.is_absolute() or ".." in linked_rel.parts:
                findings.append(f"Artifact has an unsafe Markdown file link: {rel_path} -> {linked}.")
                break
            linked_path = str(PurePosixPath(rel_path).parent.joinpath(linked_rel)) if "/" in rel_path else str(linked_rel)
            if linked_path not in artifact_set and not (workspace / linked_path).is_file():
                findings.append(f"Artifact links to a missing Markdown file: {rel_path} -> {linked}.")
                break
    return findings


def _markdown_quality_findings(
    run_state: HarnessRunState,
    target_artifacts: list[Any],
    *,
    complex_report: bool = False,
) -> list[str]:
    workspace = get_workspace(run_state.user_id, run_state.session_id)
    findings: list[str] = []
    evidence_tool_count = _successful_evidence_tool_count(run_state)
    markdown_paths = [
        str(path) for path in target_artifacts
        if isinstance(path, str) and str(path).endswith(".md")
    ]
    complex_workflow = bool(
        complex_report
        or run_state.session_skill_names
        or run_state.viewed_skill_names
        or evidence_tool_count > 0
    )
    for rel_path, path, stat in _likely_report_artifacts(workspace, markdown_paths):
        try:
            _make_workspace_artifact_readable(path)
            content = path.read_text(encoding="utf-8", errors="replace")
        except PermissionError:
            findings.append(f"Markdown report is not readable by the harness/backend: {rel_path}.")
            continue
        except OSError:
            continue
        metrics = _markdown_metrics(content)
        is_large_report = (
            stat.st_size >= MIN_COMPLEX_REPORT_BYTES
            or metrics["h1"] + metrics["h2"] >= 10
            or evidence_tool_count > 0
        )
        if not is_large_report and not complex_workflow:
            continue
        missing: list[str] = []
        if complex_workflow and stat.st_size < MIN_COMPLEX_REPORT_TARGET_BYTES:
            missing.append(f"{stat.st_size} bytes, below {MIN_COMPLEX_REPORT_TARGET_BYTES} target bytes")
        if metrics["h2"] < MIN_COMPLEX_REPORT_H2:
            missing.append(f"only {metrics['h2']} H2 sections")
        if metrics["h3"] < MIN_COMPLEX_REPORT_H3:
            missing.append(f"only {metrics['h3']} H3 subsections")
        if metrics["tables"] < MIN_COMPLEX_REPORT_TABLE_ROWS:
            missing.append(f"only {metrics['tables']} table rows")
        if evidence_tool_count and metrics["code_fences"] < MIN_COMPLEX_REPORT_CODE_FENCES_WITH_CODE:
            missing.append(f"only {metrics['code_fences']} code/result blocks")
        if evidence_tool_count and metrics["evidence_terms"] < 5:
            missing.append("few explicit evidence/source/appendix/trace/result sections")
        if metrics["links"] < 5 and evidence_tool_count:
            missing.append(f"only {metrics['links']} Markdown citations/links")
        if missing:
            findings.append(
                "Markdown report lacks expected structural/evidence density for a complex skill deliverable: "
                f"{rel_path} has {', '.join(missing)}. Preserve worker/script outputs, source evidence, "
                "trace-step mapping, appendices, and tables instead of condensing them into prose."
            )
    return findings



def _likely_report_artifacts(workspace: Path, markdown_paths: list[str]) -> list[tuple[str, Path, Any]]:
    candidates: list[tuple[str, Path, Any, bool]] = []
    report_terms = ("report", "final", "full", "comprehensive", "dossier", "development_plan", "plan")
    for rel_path in _dedupe_paths(markdown_paths):
        try:
            path = (workspace / rel_path).resolve()
            path.relative_to(workspace)
            stat = path.stat()
        except (OSError, ValueError):
            continue
        if not path.is_file() or path.is_symlink() or path.name.lower() in {"readme.md", "_checklist.md", "checklist.md"}:
            continue
        lower_name = path.name.lower()
        candidates.append((rel_path, path, stat, any(term in lower_name for term in report_terms)))
    if not candidates:
        return []
    candidates = sorted(
        candidates,
        key=lambda item: (not item[3], -int(item[2].st_size or 0), item[0]),
    )
    return [(rel, path, stat) for rel, path, stat, _ in candidates]


def _markdown_metrics(content: str) -> dict[str, int]:
    lines = content.splitlines()
    lower = content.lower()
    return {
        "h1": sum(1 for line in lines if line.startswith("# ")),
        "h2": sum(1 for line in lines if line.startswith("## ")),
        "h3": sum(1 for line in lines if line.startswith("### ")),
        "tables": sum(1 for line in lines if line.strip().startswith("|") and line.strip().endswith("|")),
        "code_fences": sum(1 for line in lines if line.strip().startswith("```")) // 2,
        "links": len(re.findall(r"\[[^\]]+\]\([^)]+\)", content)),
        "evidence_terms": sum(1 for term in ("evidence", "source", "appendix", "trace", "artifact", "reference", "citation", "output", "result") if term in lower),
    }

def _deterministic_verifier_payload(
    run_state: HarnessRunState,
    *,
    requested_artifact: bool = False,
    complex_report: bool = False,
) -> dict[str, Any] | None:
    target_artifacts = [artifact.get("path") for artifact in run_state.artifacts if artifact.get("path")]
    if not target_artifacts and run_state.tool_error_count == 0 and run_state.invalid_placeholder_write_count == 0 and run_state.schema_failure_count == 0 and not requested_artifact and not complex_report:
        return None
    findings: list[str] = []
    needs_more_work = False
    verdict = "pass"
    if requested_artifact and not target_artifacts:
        findings.append("The user requested a file or durable deliverable, but no artifact was produced.")
        needs_more_work = True
        verdict = "fail"
    if complex_report and not any(isinstance(path, str) and path.endswith(".md") for path in target_artifacts):
        findings.append("The complex report workflow did not produce a Markdown report artifact.")
        needs_more_work = True
        verdict = "fail"
    if run_state.invalid_placeholder_write_count > 0:
        findings.append("A write was rejected because it contained compacted conversation-history placeholder content.")
        needs_more_work = True
        verdict = "fail"
    empty_artifacts = [artifact.get("path") for artifact in run_state.artifacts if int(artifact.get("size_bytes") or 0) <= 0]
    if empty_artifacts:
        findings.append(f"Artifact is empty: {', '.join(str(path) for path in empty_artifacts)}.")
        needs_more_work = True
        verdict = "fail"
    markdown_findings = _markdown_artifact_findings(run_state, target_artifacts)
    if markdown_findings:
        findings.extend(markdown_findings)
        needs_more_work = True
        verdict = "fail"
    markdown_quality_findings = _markdown_quality_findings(
        run_state,
        target_artifacts,
        complex_report=complex_report,
    )
    if markdown_quality_findings:
        findings.extend(markdown_quality_findings)
        needs_more_work = True
        verdict = "fail" if requested_artifact or complex_report else "inconclusive"
    workflow_findings = _workflow_contract_findings(run_state)
    if workflow_findings:
        findings.extend(str(item.get("message") or item) for item in workflow_findings)
        needs_more_work = True
        verdict = "fail"
    last_validation_failure_at = max(run_state.last_schema_failure_at, run_state.last_parse_failure_at)
    if (run_state.schema_failure_count > 0 or run_state.parse_failure_count > 0) and last_validation_failure_at > run_state.last_successful_artifact_at:
        findings.append("One or more tool calls failed schema or argument parsing validation after the last successful artifact.")
        needs_more_work = True
        verdict = "fail"
    elif run_state.schema_failure_count > 0 or run_state.parse_failure_count > 0:
        findings.append("Earlier schema or argument parsing failures were followed by a successful artifact write/patch.")
    if run_state.tool_error_count > 0 and not findings:
        if run_state.last_tool_error_at > run_state.last_successful_artifact_at:
            findings.append("One or more tool calls failed during a requested artifact workflow and must be resolved before completion.")
            needs_more_work = True
            verdict = "fail" if requested_artifact or complex_report else "inconclusive"
        else:
            findings.append("Earlier tool failures were followed by a successful artifact write/patch.")
    if not findings:
        findings.append("Artifacts referenced by successful write/patch tool results are non-empty.")
    return {
        "verifier_kind": "artifact_integrity",
        "target_artifacts": target_artifacts,
        "verdict": verdict,
        "reason": findings[0],
        "findings": findings,
        "needs_more_work": needs_more_work,
    }


def _json_object(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _latest_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
    return ""


def _looks_like_complex_artifact_request(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "完整", "综合", "详细", "全面", "计划", "方案", "设计", "报告",
        "development plan", "clinical", "trial", "phase i", "phase ii", "phase iii",
        "regulatory", "simulation", "分析", "策略", "白皮书", "proposal",
    )
    return sum(1 for marker in markers if marker in lowered) >= 2


def _looks_like_file_artifact_request(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "write", "save", "create a file", "生成文件", "写入文件", "保存", "导出",
        "artifact", "deliverable", "report", "报告", "文档", "markdown", ".md",
        ".txt", ".csv", ".json", ".pdf", "workspace", "工作区",
    )
    return any(marker in lowered for marker in markers)


def _looks_like_unfulfilled_action_promise(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 40:
        return False
    tail = stripped[-700:].lower()
    promise_markers = (
        "let me ", "i will ", "i'll ", "i am going to ", "i’m going to ",
        "next, i", "now i will", "i have the full content", "continue by",
        "让我", "接下来", "下一步", "现在我", "我将", "我会",
    )
    action_markers = (
        "run", "execute", "write", "save", "create", "call", "use the tool",
        "workspace", "simulation", "analysis", "script", "生成", "写入", "运行", "执行", "调用", "保存",
        "撰写", "编写", "创建", "输出", "整理", "文档", "报告",
    )
    completion_markers = (
        "completed", "done", "已完成", "完成如下", "最终", "final", "结果如下",
    )
    if any(marker in tail for marker in completion_markers):
        return False
    return any(marker in tail for marker in promise_markers) and any(marker in tail for marker in action_markers)


# ── Internal helpers ────────────────────────────────────────────────────


class _HTTPError(Exception):
    """Wrapper for non-200 HTTP responses from the LLM endpoint."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:200]}")


def _http_error(status_code: int, body: str) -> _HTTPError:
    return _HTTPError(status_code, body)


def _sanitize_messages(
    messages: list[dict],
    *,
    strip_images: bool = True,
) -> list[dict]:
    """Strip internal ``_``-prefixed keys, ``tool_name``, and image_url parts.

    Image stripping is a safety net for text-only models — mirrors
    hermes-agent's ``_prepare_messages_for_non_vision_model``.

    Returns a shallow copy with stripped keys so the original is not mutated.
    """
    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue
        # Strip internal marker keys
        entry = {
            k: v
            for k, v in msg.items()
            if not (isinstance(k, str) and k.startswith("_"))
        }
        # Remove tool_name from tool messages
        entry.pop("tool_name", None)
        # Clean tool_calls entries
        tcs = entry.get("tool_calls")
        if isinstance(tcs, list):
            clean_tcs = []
            for tc in tcs:
                if isinstance(tc, dict):
                    tc = {k: v for k, v in tc.items() if k not in ("call_id", "response_item_id")}
                clean_tcs.append(tc)
            entry["tool_calls"] = clean_tcs
        # ── Safety net: strip image_url parts from content ──────────
        # Text-only models (deepseek_v4_pro) reject messages
        # containing image_url content parts. The pre-enrichment in
        # run_stream() should have already converted images to text, but
        # this catches any edge cases (e.g. tool results with images).
        content = entry.get("content")
        if strip_images and isinstance(content, list):
            text_only = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    continue
                text_only.append(part)
            if len(text_only) != len(content):
                if text_only:
                    entry["content"] = text_only
                elif entry.get("role") == "tool":
                    entry["content"] = "[image content removed — server does not support images]"
                else:
                    entry["content"] = ""
        cleaned.append(entry)
    return cleaned


def _safe_parse_args(raw: str):
    """Parse tool call arguments JSON without letting malformed args reach handlers."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        repaired = _extract_json_object(raw)
        if repaired is not None:
            return repaired
        return {
            "__tool_arg_parse_error": f"{exc.msg} at char {exc.pos}",
            "__raw_args_preview": raw[:500],
        }
    if isinstance(parsed, dict):
        return parsed
    return {
        "__tool_arg_parse_error": f"tool arguments must be a JSON object, got {type(parsed).__name__}",
        "__raw_args_preview": str(parsed)[:500],
    }


def _extract_json_object(raw: str) -> dict | None:
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
