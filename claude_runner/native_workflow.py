"""Machine receipts and native hook decisions for Skill-declared workflows."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import stat
import uuid
from pathlib import Path
from typing import Any, Iterable


MAX_PHASES = 128
MAX_WORKERS = 128
MAX_FINDINGS = 256
MAX_SUBAGENT_TRANSCRIPT_BYTES = 64 * 1024 * 1024
SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
SAFE_AGENT_TYPE = re.compile(r"[A-Za-z0-9._:-]{1,512}")
SAFE_TASK_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}")
SAFE_TOOL_ID = re.compile(r"[A-Za-z0-9._:-]{1,256}")


def validate_workflow_contract(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != (
        "chatds.skill-workflow-contract.v1"
    ):
        raise ValueError("workflow_contract_invalid")
    skill_name = value.get("skill_name")
    route_id = value.get("route_id")
    route_sha256 = value.get("route_sha256")
    phases = value.get("phases")
    if (
        not isinstance(skill_name, str)
        or SAFE_NAME.fullmatch(skill_name) is None
        or not isinstance(route_id, str)
        or SAFE_NAME.fullmatch(route_id) is None
        or not isinstance(route_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", route_sha256) is None
        or not isinstance(phases, list)
        or not phases
        or len(phases) > MAX_PHASES
    ):
        raise ValueError("workflow_contract_invalid")
    observed: set[str] = set()
    normalized_phases: list[dict[str, Any]] = []
    for phase in phases:
        if not isinstance(phase, dict):
            raise ValueError("workflow_contract_invalid")
        mode = phase.get("mode")
        workers = phase.get("workers")
        if (
            mode not in {"parallel", "sequential"}
            or not isinstance(workers, list)
            or not workers
            or len(workers) > MAX_WORKERS
            or mode == "sequential" and len(workers) != 1
        ):
            raise ValueError("workflow_contract_invalid")
        normalized_workers: list[dict[str, str]] = []
        for worker in workers:
            if not isinstance(worker, dict):
                raise ValueError("workflow_contract_invalid")
            worker_id = worker.get("worker_id")
            native_agent_type = worker.get("native_agent_type")
            if (
                not isinstance(worker_id, str)
                or SAFE_NAME.fullmatch(worker_id) is None
                or not isinstance(native_agent_type, str)
                or SAFE_AGENT_TYPE.fullmatch(native_agent_type) is None
                or native_agent_type
                != f"chatds-session-skills:{skill_name}:{worker_id}"
                or native_agent_type in observed
            ):
                raise ValueError("workflow_contract_invalid")
            observed.add(native_agent_type)
            normalized_workers.append({
                "worker_id": worker_id,
                "native_agent_type": native_agent_type,
            })
        normalized_phases.append({
            "mode": mode,
            "workers": normalized_workers,
        })
    return {**value, "phases": normalized_phases}


def build_workflow_receipt(
    contract: object,
    tasks: Iterable[dict[str, Any]],
    violations: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    normalized = validate_workflow_contract(contract)
    task_rows = [dict(row) for row in tasks]
    phase_rows: list[dict[str, Any]] = []
    frontier_index = len(normalized["phases"])
    for phase_index, phase in enumerate(normalized["phases"]):
        worker_rows: list[dict[str, Any]] = []
        phase_passed = True
        for worker in phase["workers"]:
            attempts = [
                row for row in task_rows
                if row.get("native_agent_type")
                == worker["native_agent_type"]
            ]
            terminal_by_status: dict[str, int] = {}
            for attempt in attempts:
                status = str(attempt.get("status") or "unknown")
                if status != "running":
                    terminal_by_status[status] = (
                        terminal_by_status.get(status, 0) + 1
                    )
            if any(row.get("status") == "completed" for row in attempts):
                status = "succeeded"
            elif any(row.get("status") == "running" for row in attempts):
                status = "running"
            elif attempts:
                status = "failed"
            else:
                status = "missing"
            if status != "succeeded":
                phase_passed = False
            worker_rows.append({
                **worker,
                "status": status,
                "attempt_count": len(attempts),
                "terminal_by_status": dict(sorted(terminal_by_status.items())),
            })
        if not phase_passed and frontier_index == len(normalized["phases"]):
            frontier_index = phase_index
        phase_rows.append({
            "mode": phase["mode"],
            "status": "passed" if phase_passed else "pending",
            "workers": worker_rows,
        })
    normalized_violations = _normalize_violations(violations)
    return {
        "schema": "chatds.native-workflow-receipt.v1",
        "skill_name": normalized["skill_name"],
        "route_id": normalized["route_id"],
        "route_sha256": normalized["route_sha256"],
        "status": (
            "passed"
            if frontier_index == len(phase_rows) and not normalized_violations
            else "pending"
        ),
        "frontier_index": frontier_index,
        "phases": phase_rows,
        "violations": normalized_violations,
    }


def terminal_workflow_receipt(
    contract: object,
    receipt: object,
) -> dict[str, Any]:
    normalized = validate_workflow_contract(contract)
    current = _validate_receipt_identity(normalized, receipt)
    findings: list[dict[str, Any]] = []
    for phase_index, phase in enumerate(current["phases"]):
        for worker in phase["workers"]:
            status = worker["status"]
            if status != "succeeded" and len(findings) < MAX_FINDINGS:
                findings.append({
                    "code": f"workflow_worker_{status}",
                    "phase_index": phase_index,
                    "worker_id": worker["worker_id"],
                    "native_agent_type": worker["native_agent_type"],
                    "attempt_count": worker["attempt_count"],
                })
    for violation in current["violations"]:
        if len(findings) < MAX_FINDINGS:
            findings.append(dict(violation))
    return {
        "status": "failed" if findings else "passed",
        "skill_name": normalized["skill_name"],
        "route_id": normalized["route_id"],
        "route_sha256": normalized["route_sha256"],
        "finding_count": len(findings),
        "findings": findings,
        "frontier_index": current["frontier_index"],
    }


def workflow_start_violation(
    contract: object,
    receipt: object,
    *,
    native_agent_type: str,
) -> dict[str, Any] | None:
    normalized = validate_workflow_contract(contract)
    current = _validate_receipt_identity(normalized, receipt)
    phase_index, worker = _required_worker(normalized, native_agent_type)
    if worker is None:
        return None
    frontier = current["frontier_index"]
    worker_receipt = current["phases"][phase_index]["workers"][
        next(
            index
            for index, row in enumerate(normalized["phases"][phase_index]["workers"])
            if row["native_agent_type"] == native_agent_type
        )
    ]
    code: str | None = None
    if phase_index > frontier:
        code = "workflow_phase_order_violation"
    elif phase_index < frontier or worker_receipt["status"] == "succeeded":
        code = "workflow_worker_repeated_after_success"
    elif worker_receipt["status"] == "running":
        code = "workflow_worker_duplicate_running"
    if code is None:
        return None
    return {
        "code": code,
        "phase_index": phase_index,
        "frontier_index": frontier,
        "worker_id": worker["worker_id"],
        "native_agent_type": native_agent_type,
    }


def classify_native_subagent_outcome(
    *,
    projects_root: Path,
    native_session_id: str,
    task: dict[str, Any],
) -> tuple[str, str]:
    task_id = str(task.get("task_id") or "")
    tool_use_id = str(task.get("tool_use_id") or "")
    native_agent_type = str(task.get("native_agent_type") or "")
    if (
        SAFE_TASK_ID.fullmatch(task_id) is None
        or SAFE_TOOL_ID.fullmatch(tool_use_id) is None
        or SAFE_AGENT_TYPE.fullmatch(native_agent_type) is None
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            native_session_id,
        ) is None
    ):
        return "failed", "native_subagent_identity_invalid"
    main_transcripts = list(projects_root.glob(f"*/{native_session_id}.jsonl"))
    if len(main_transcripts) != 1:
        return "failed", "native_subagent_checkpoint_missing"
    project = main_transcripts[0].parent
    subagents = project / native_session_id / "subagents"
    transcript = subagents / f"agent-{task_id}.jsonl"
    metadata = subagents / f"agent-{task_id}.meta.json"
    try:
        transcript_info = os.lstat(transcript)
        metadata_info = os.lstat(metadata)
        if (
            transcript.is_symlink()
            or metadata.is_symlink()
            or not stat.S_ISREG(transcript_info.st_mode)
            or not stat.S_ISREG(metadata_info.st_mode)
            or transcript_info.st_size <= 0
            or transcript_info.st_size > MAX_SUBAGENT_TRANSCRIPT_BYTES
            or metadata_info.st_size <= 0
            or metadata_info.st_size > 64 * 1024
        ):
            return "failed", "native_subagent_checkpoint_invalid"
        meta = json.loads(metadata.read_text(encoding="utf-8"))
        lines = transcript.read_bytes().splitlines()
        record = json.loads(lines[-1]) if lines else None
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return "failed", "native_subagent_checkpoint_invalid"
    if (
        not isinstance(meta, dict)
        or meta.get("agentType") != native_agent_type
        or meta.get("toolUseId") != tool_use_id
        or not isinstance(record, dict)
        or record.get("type") != "assistant"
        or record.get("agentId") != task_id
        or record.get("sessionId") != native_session_id
    ):
        return "failed", "native_subagent_checkpoint_invalid"
    if record.get("isApiErrorMessage") is True or record.get("error") is not None:
        return "failed", "native_subagent_api_error"
    message = record.get("message")
    if not isinstance(message, dict) or message.get("stop_reason") != "end_turn":
        return "failed", "native_subagent_result_incomplete"
    content = message.get("content")
    if (
        not isinstance(content, list)
        or not any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and bool(block["text"].strip())
            for block in content
        )
    ):
        return "failed", "native_subagent_result_incomplete"
    return "completed", "native_subagent_end_turn"


def write_workflow_receipt(
    path: Path,
    receipt: dict[str, Any],
    *,
    owner_uid: int | None = None,
    group_gid: int | None = None,
) -> None:
    if not path.is_absolute():
        raise ValueError("workflow_receipt_path_invalid")
    payload = json.dumps(
        receipt,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o440,
    )
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("workflow_receipt_short_write")
            offset += written
        if owner_uid is not None or group_gid is not None:
            os.fchown(
                descriptor,
                -1 if owner_uid is None else owner_uid,
                -1 if group_gid is None else group_gid,
            )
        os.fchmod(descriptor, 0o440)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def evaluate_workflow_hook(
    *,
    hook_input: object,
    contract: object,
    receipt: object,
    artifact_contracts: object,
) -> dict[str, Any]:
    try:
        normalized = validate_workflow_contract(contract)
        current = _validate_receipt_identity(normalized, receipt)
    except ValueError:
        return _block_hook_input(hook_input, "Machine workflow state is invalid.")
    if not isinstance(hook_input, dict):
        return {"decision": "block", "reason": "Machine workflow hook input is invalid."}
    event_name = hook_input.get("hook_event_name")
    if event_name == "Stop":
        if hook_input.get("stop_hook_active") is True:
            return {}
        terminal = terminal_workflow_receipt(normalized, current)
        if terminal["status"] == "passed":
            return {}
        rendered = json.dumps(
            terminal["findings"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )[:32_000]
        return {
            "decision": "block",
            "reason": (
                "Machine workflow receipts are incomplete. Retry failed "
                "mandatory workers, wait for the current phase barrier, then "
                "run every sequential phase before synthesizing artifacts. "
                f"Findings: {rendered}"
            ),
        }
    if event_name != "PreToolUse":
        return {}
    if hook_input.get("agent_id") is not None:
        return {}
    tool_name = str(hook_input.get("tool_name") or "")
    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        return _deny("Machine workflow could not validate tool input.")
    if tool_name in {"Agent", "Task"}:
        native_agent_type = str(tool_input.get("subagent_type") or "")
        phase_index, worker = _required_worker(normalized, native_agent_type)
        if worker is None:
            if current["status"] == "passed":
                return {}
            return _deny(_frontier_feedback(
                current,
                "Optional root subagents cannot start before the mandatory "
                "workflow frontier passes.",
            ))
        frontier = current["frontier_index"]
        worker_receipt = next(
            row
            for row in current["phases"][phase_index]["workers"]
            if row["native_agent_type"] == native_agent_type
        )
        if phase_index > frontier:
            return _deny(
                "A later mandatory phase cannot start before the current "
                f"phase barrier passes (frontier={frontier})."
            )
        if phase_index < frontier or worker_receipt["status"] == "succeeded":
            return _deny("This mandatory worker already has a successful receipt.")
        if worker_receipt["status"] == "running":
            return _deny("This mandatory worker is already running; wait for its receipt.")
        return {}
    if current["status"] == "passed":
        return {}
    if tool_name in {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"}:
        if _tool_targets_declared_artifact(tool_name, tool_input, artifact_contracts):
            return _deny(_frontier_feedback(
                current,
                "Declared artifact synthesis is gated until every mandatory "
                "workflow phase passes.",
            ))
    return {}


def _frontier_feedback(receipt: dict[str, Any], prefix: str) -> str:
    frontier = int(receipt["frontier_index"])
    phases = receipt["phases"]
    workers: list[dict[str, str]] = []
    if frontier < len(phases):
        workers = [
            {
                "native_agent_type": str(worker["native_agent_type"]),
                "status": str(worker["status"]),
            }
            for worker in phases[frontier]["workers"]
        ]
    rendered = json.dumps(
        workers,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(rendered) > 24_000:
        rendered = rendered[:24_000]
    return (
        f"{prefix} frontier={frontier}. Do not retry the blocked tool. "
        "For each missing or failed entry, call the native Agent tool with "
        "subagent_type exactly equal to native_agent_type; await entries "
        f"already running. Current phase: {rendered}"
    )


def _required_worker(
    contract: dict[str, Any], native_agent_type: str
) -> tuple[int, dict[str, str] | None]:
    for phase_index, phase in enumerate(contract["phases"]):
        for worker in phase["workers"]:
            if worker["native_agent_type"] == native_agent_type:
                return phase_index, worker
    return -1, None


def _normalize_violations(
    violations: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for violation in violations:
        if not isinstance(violation, dict) or not isinstance(
            violation.get("code"), str
        ):
            raise ValueError("workflow_receipt_invalid")
        if len(rows) >= MAX_FINDINGS:
            raise ValueError("workflow_receipt_invalid")
        rows.append(dict(violation))
    return rows


def _validate_receipt_identity(
    contract: dict[str, Any], receipt: object
) -> dict[str, Any]:
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "chatds.native-workflow-receipt.v1"
        or receipt.get("skill_name") != contract["skill_name"]
        or receipt.get("route_id") != contract["route_id"]
        or receipt.get("route_sha256") != contract["route_sha256"]
        or receipt.get("status") not in {"pending", "passed"}
        or not isinstance(receipt.get("frontier_index"), int)
        or not 0 <= receipt["frontier_index"] <= len(contract["phases"])
        or not isinstance(receipt.get("phases"), list)
        or len(receipt["phases"]) != len(contract["phases"])
        or not isinstance(receipt.get("violations"), list)
    ):
        raise ValueError("workflow_receipt_invalid")
    for expected_phase, actual_phase in zip(
        contract["phases"], receipt["phases"], strict=True
    ):
        if (
            not isinstance(actual_phase, dict)
            or actual_phase.get("mode") != expected_phase["mode"]
            or actual_phase.get("status") not in {"pending", "passed"}
            or not isinstance(actual_phase.get("workers"), list)
            or len(actual_phase["workers"]) != len(expected_phase["workers"])
        ):
            raise ValueError("workflow_receipt_invalid")
        for expected_worker, actual_worker in zip(
            expected_phase["workers"], actual_phase["workers"], strict=True
        ):
            if (
                not isinstance(actual_worker, dict)
                or actual_worker.get("worker_id") != expected_worker["worker_id"]
                or actual_worker.get("native_agent_type")
                != expected_worker["native_agent_type"]
                or actual_worker.get("status")
                not in {"missing", "running", "failed", "succeeded"}
                or isinstance(actual_worker.get("attempt_count"), bool)
                or not isinstance(actual_worker.get("attempt_count"), int)
                or actual_worker["attempt_count"] < 0
                or not isinstance(actual_worker.get("terminal_by_status"), dict)
            ):
                raise ValueError("workflow_receipt_invalid")
    _normalize_violations(receipt["violations"])
    return dict(receipt)


def _tool_targets_declared_artifact(
    tool_name: str,
    tool_input: dict[str, Any],
    artifact_contracts: object,
) -> bool:
    if not isinstance(artifact_contracts, list):
        return True
    patterns: list[str] = []
    for contract in artifact_contracts:
        if not isinstance(contract, dict):
            return True
        final = contract.get("declared_final_artifact")
        if isinstance(final, str):
            patterns.append(_artifact_pattern(final))
        for field in ("declared_modular_files", "declared_ancillary_files"):
            values = contract.get(field, [])
            if not isinstance(values, list):
                return True
            for value in values:
                if not isinstance(value, str):
                    return True
                patterns.append(_artifact_pattern(value))
    if not patterns:
        return False
    candidates: list[str] = []
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return True
        try:
            candidates.extend(shlex.split(command))
        except ValueError:
            return True
    else:
        for key in ("file_path", "filepath", "path", "notebook_path"):
            value = tool_input.get(key)
            if isinstance(value, str):
                candidates.append(value)
    for candidate in candidates:
        normalized = candidate.strip("'\";,()[]{}<>").replace("\\", "/")
        if normalized.startswith("/workspace/"):
            normalized = normalized[len("/workspace/"):]
        basename = normalized.rsplit("/", 1)[-1]
        if any(
            fnmatch.fnmatchcase(normalized, pattern)
            or fnmatch.fnmatchcase(basename, pattern.rsplit("/", 1)[-1])
            for pattern in patterns
        ):
            return True
    return False


def _artifact_pattern(value: str) -> str:
    pattern = value.strip().replace("\\", "/")
    if (
        not pattern
        or pattern.startswith("/")
        or any(part in {"", ".", ".."} for part in pattern.split("/"))
    ):
        raise ValueError("workflow_artifact_contract_invalid")
    pattern = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]{0,63}\}", "*", pattern)
    if "{" in pattern or "}" in pattern:
        raise ValueError("workflow_artifact_contract_invalid")
    return pattern


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def _block_hook_input(hook_input: object, reason: str) -> dict[str, Any]:
    if isinstance(hook_input, dict) and hook_input.get(
        "hook_event_name"
    ) == "PreToolUse":
        return _deny(reason)
    return {"decision": "block", "reason": reason}
