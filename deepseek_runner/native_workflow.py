"""Trusted projection and receipt audit for native DSH Skill workflows."""

from __future__ import annotations

import re
from typing import Any, Iterable


MAX_PHASES = 128
MAX_WORKERS = 128
MAX_MANIFEST_ROWS = 4096
MAX_USER_TURN_BYTES = 2 * 1024 * 1024
MAX_WORKER_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_WORKER_SOURCE_BYTES = 32 * 1024 * 1024
MAX_FINDINGS = 256
MAX_ATTEMPTS_PER_WORKER = 2
HANDOFF_MAX_CHARS = 24_000
SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
SAFE_NATIVE_SESSION_ID = re.compile(r"chatds-[0-9a-f]{32}")
SAFE_SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_RUN_ID = re.compile(r"[^\x00\r\n]{1,256}")
WORKFLOW_EVENT_TYPES = frozenset({
    "tool-workflow/run-start",
    "tool-workflow/agent-start",
    "tool-workflow/agent-end",
    "tool-workflow/run-end",
})
WORKFLOW_OUTCOMES = frozenset({"completed", "failed", "cancelled"})
WORKFLOW_STOP_REASONS = frozenset({"completed", "error", "cancelled"})


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("deepseek_workflow_projection_invalid")
    normalized = value.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or "\x00" in normalized
        or len(normalized.encode("utf-8")) > 1024
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise ValueError("deepseek_workflow_projection_invalid")
    return normalized


def _validate_route_contract(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != (
        "chatds.skill-workflow-contract.v1"
    ):
        raise ValueError("deepseek_workflow_projection_invalid")
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
        or SAFE_SHA256.fullmatch(route_sha256) is None
        or not isinstance(phases, list)
        or not phases
        or len(phases) > MAX_PHASES
    ):
        raise ValueError("deepseek_workflow_projection_invalid")
    observed: set[str] = set()
    normalized_phases: list[dict[str, Any]] = []
    for phase in phases:
        if not isinstance(phase, dict):
            raise ValueError("deepseek_workflow_projection_invalid")
        mode = phase.get("mode")
        workers = phase.get("workers")
        if (
            mode not in {"parallel", "sequential"}
            or not isinstance(workers, list)
            or not workers
            or len(workers) > MAX_WORKERS
            or mode == "sequential" and len(workers) != 1
        ):
            raise ValueError("deepseek_workflow_projection_invalid")
        normalized_workers: list[dict[str, str]] = []
        for worker in workers:
            if not isinstance(worker, dict):
                raise ValueError("deepseek_workflow_projection_invalid")
            worker_id = worker.get("worker_id")
            native_agent_type = worker.get("native_agent_type")
            if (
                not isinstance(worker_id, str)
                or SAFE_NAME.fullmatch(worker_id) is None
                or native_agent_type
                != f"chatds-session-skills:{skill_name}:{worker_id}"
                or worker_id in observed
            ):
                raise ValueError("deepseek_workflow_projection_invalid")
            observed.add(worker_id)
            normalized_workers.append({
                "worker_id": worker_id,
                "native_agent_type": native_agent_type,
            })
        normalized_phases.append({"mode": mode, "workers": normalized_workers})
    return {
        "skill_name": skill_name,
        "route_id": route_id,
        "route_sha256": route_sha256,
        "phases": normalized_phases,
    }


def compile_deepseek_workflow_projection(
    *,
    manifest: dict[str, Any],
    workflow_contract: object,
    user_turn_text: str,
    native_session_id: str,
) -> dict[str, Any] | None:
    """Bind an engine-neutral route to immutable DSH worker resources."""

    if workflow_contract is None:
        return None
    route = _validate_route_contract(workflow_contract)
    if (
        not isinstance(manifest, dict)
        or not isinstance(user_turn_text, str)
        or not user_turn_text.strip()
        or len(user_turn_text.encode("utf-8")) > MAX_USER_TURN_BYTES
        or SAFE_NATIVE_SESSION_ID.fullmatch(native_session_id) is None
    ):
        raise ValueError("deepseek_workflow_projection_invalid")
    raw_workers = manifest.get("worker_agents")
    raw_skills = manifest.get("skills")
    if (
        not isinstance(raw_workers, list)
        or len(raw_workers) > MAX_MANIFEST_ROWS
        or not isinstance(raw_skills, list)
        or len(raw_skills) > MAX_MANIFEST_ROWS
    ):
        raise ValueError("deepseek_workflow_projection_invalid")

    skill_rows = [
        row for row in raw_skills
        if isinstance(row, dict) and row.get("name") == route["skill_name"]
    ]
    if len(skill_rows) != 1 or not isinstance(skill_rows[0].get("files"), list):
        raise ValueError("deepseek_workflow_projection_invalid")
    files: dict[str, tuple[str, int]] = {}
    for row in skill_rows[0]["files"]:
        if not isinstance(row, dict):
            raise ValueError("deepseek_workflow_projection_invalid")
        path = _safe_relative_path(row.get("path"))
        digest = row.get("sha256")
        size = row.get("size")
        if (
            not isinstance(digest, str)
            or SAFE_SHA256.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= MAX_WORKER_SOURCE_BYTES
            or path in files
        ):
            raise ValueError("deepseek_workflow_projection_invalid")
        files[path] = (digest, size)

    agents: dict[str, tuple[str, str]] = {}
    for row in raw_workers:
        if not isinstance(row, dict) or row.get("skill_name") != route["skill_name"]:
            continue
        worker_id = row.get("worker_id")
        source_path = _safe_relative_path(row.get("source_path"))
        native_agent_type = row.get("native_agent_type")
        if (
            not isinstance(worker_id, str)
            or SAFE_NAME.fullmatch(worker_id) is None
            or native_agent_type
            != f"chatds-session-skills:{route['skill_name']}:{worker_id}"
            or worker_id in agents
        ):
            raise ValueError("deepseek_workflow_projection_invalid")
        agents[worker_id] = (source_path, native_agent_type)

    total_source_bytes = 0
    projected_phases: list[dict[str, Any]] = []
    for phase_index, phase in enumerate(route["phases"]):
        projected_workers: list[dict[str, Any]] = []
        for worker in phase["workers"]:
            worker_id = worker["worker_id"]
            agent = agents.get(worker_id)
            if agent is None or agent[1] != worker["native_agent_type"]:
                raise ValueError("deepseek_workflow_projection_invalid")
            source_path = agent[0]
            source = files.get(source_path)
            if source is None:
                raise ValueError("deepseek_workflow_projection_invalid")
            total_source_bytes += source[1]
            if total_source_bytes > MAX_TOTAL_WORKER_SOURCE_BYTES:
                raise ValueError("deepseek_workflow_projection_invalid")
            projected_workers.append({
                "worker_id": worker_id,
                "source_path": source_path,
                "source_sha256": source[0],
                "source_size": source[1],
            })
        projected_phases.append({
            "mode": phase["mode"],
            "phase_id": f"phase-{phase_index}",
            "workers": projected_workers,
        })
    projection = {
        "schema": "chatds.deepseek-skill-workflow.v1",
        "native_session_id": native_session_id,
        "skill_name": route["skill_name"],
        "route_id": route["route_id"],
        "route_sha256": route["route_sha256"],
        "run_name": f"skill-workflow-{route['route_sha256'][:16]}",
        "user_turn_text": user_turn_text,
        "max_attempts_per_worker": MAX_ATTEMPTS_PER_WORKER,
        "handoff_max_chars": HANDOFF_MAX_CHARS,
        "phases": projected_phases,
    }
    return validate_deepseek_workflow_projection(projection)


def validate_deepseek_workflow_projection(value: object) -> dict[str, Any]:
    """Validate the exact controller-to-native workflow projection schema."""

    required = {
        "schema", "native_session_id", "skill_name", "route_id",
        "route_sha256", "run_name", "user_turn_text",
        "max_attempts_per_worker", "handoff_max_chars", "phases",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("deepseek_workflow_projection_invalid")
    if (
        value.get("schema") != "chatds.deepseek-skill-workflow.v1"
        or not isinstance(value.get("native_session_id"), str)
        or SAFE_NATIVE_SESSION_ID.fullmatch(value["native_session_id"]) is None
        or not isinstance(value.get("skill_name"), str)
        or SAFE_NAME.fullmatch(value["skill_name"]) is None
        or not isinstance(value.get("route_id"), str)
        or SAFE_NAME.fullmatch(value["route_id"]) is None
        or not isinstance(value.get("route_sha256"), str)
        or SAFE_SHA256.fullmatch(value["route_sha256"]) is None
        or value.get("run_name")
        != f"skill-workflow-{value.get('route_sha256', '')[:16]}"
        or not isinstance(value.get("user_turn_text"), str)
        or not value["user_turn_text"].strip()
        or len(value["user_turn_text"].encode("utf-8")) > MAX_USER_TURN_BYTES
        or value.get("max_attempts_per_worker") != MAX_ATTEMPTS_PER_WORKER
        or value.get("handoff_max_chars") != HANDOFF_MAX_CHARS
        or not isinstance(value.get("phases"), list)
        or not value["phases"]
        or len(value["phases"]) > MAX_PHASES
    ):
        raise ValueError("deepseek_workflow_projection_invalid")
    observed: set[str] = set()
    total_source_bytes = 0
    phases: list[dict[str, Any]] = []
    for phase_index, phase in enumerate(value["phases"]):
        if not isinstance(phase, dict) or set(phase) != {
            "mode", "phase_id", "workers"
        }:
            raise ValueError("deepseek_workflow_projection_invalid")
        workers = phase.get("workers")
        if (
            phase.get("mode") not in {"parallel", "sequential"}
            or phase.get("phase_id") != f"phase-{phase_index}"
            or not isinstance(workers, list)
            or not workers
            or len(workers) > MAX_WORKERS
            or phase.get("mode") == "sequential" and len(workers) != 1
        ):
            raise ValueError("deepseek_workflow_projection_invalid")
        normalized_workers: list[dict[str, Any]] = []
        for worker in workers:
            if not isinstance(worker, dict) or set(worker) != {
                "worker_id", "source_path", "source_sha256", "source_size"
            }:
                raise ValueError("deepseek_workflow_projection_invalid")
            worker_id = worker.get("worker_id")
            digest = worker.get("source_sha256")
            size = worker.get("source_size")
            if (
                not isinstance(worker_id, str)
                or SAFE_NAME.fullmatch(worker_id) is None
                or worker_id in observed
                or not isinstance(digest, str)
                or SAFE_SHA256.fullmatch(digest) is None
                or isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 <= size <= MAX_WORKER_SOURCE_BYTES
            ):
                raise ValueError("deepseek_workflow_projection_invalid")
            observed.add(worker_id)
            total_source_bytes += size
            if total_source_bytes > MAX_TOTAL_WORKER_SOURCE_BYTES:
                raise ValueError("deepseek_workflow_projection_invalid")
            normalized_workers.append({
                "worker_id": worker_id,
                "source_path": _safe_relative_path(worker.get("source_path")),
                "source_sha256": digest,
                "source_size": size,
            })
        phases.append({
            "mode": phase["mode"],
            "phase_id": phase["phase_id"],
            "workers": normalized_workers,
        })
    return {**value, "phases": phases}


def build_deepseek_workflow_receipt(
    projection: object,
    envelopes: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Audit native workflow events into one controller-owned terminal receipt."""

    contract = validate_deepseek_workflow_projection(projection)
    expected: dict[str, tuple[int, str]] = {}
    states: dict[str, dict[str, Any]] = {}
    for phase_index, phase in enumerate(contract["phases"]):
        for worker in phase["workers"]:
            worker_id = worker["worker_id"]
            expected[worker_id] = (phase_index, phase["phase_id"])
            states[worker_id] = {
                "attempt_count": 0,
                "outcomes": {},
                "succeeded": False,
            }

    findings: list[dict[str, Any]] = []

    def finding(code: str, **values: Any) -> None:
        if len(findings) < MAX_FINDINGS:
            findings.append({"code": code, **values})

    target_run_id: str | None = None
    matching_run_count = 0
    run_ended = False
    live: dict[int, str] = {}
    frontier = 0

    def advance_frontier() -> None:
        nonlocal frontier
        while frontier < len(contract["phases"]):
            if not all(
                states[row["worker_id"]]["succeeded"]
                for row in contract["phases"][frontier]["workers"]
            ):
                return
            frontier += 1

    for envelope in envelopes:
        if not isinstance(envelope, dict):
            continue
        event = envelope.get("event")
        if (
            not isinstance(event, dict)
            or event.get("type") != "deepseek.session.event"
            or event.get("session_id") != contract["native_session_id"]
            or event.get("delegation_depth") != 0
        ):
            continue
        native = event.get("session_event")
        if not isinstance(native, dict) or native.get("type") not in WORKFLOW_EVENT_TYPES:
            continue
        native_type = native["type"]
        data = native.get("data")
        if not isinstance(data, dict):
            finding("workflow_event_invalid", event_type=native_type)
            continue
        run_id = data.get("runId")
        if not isinstance(run_id, str) or SAFE_RUN_ID.fullmatch(run_id) is None:
            finding("workflow_run_identity_invalid", event_type=native_type)
            continue
        if native_type == "tool-workflow/run-start":
            if data.get("name") != contract["run_name"]:
                continue
            matching_run_count += 1
            if target_run_id is None:
                target_run_id = run_id
            else:
                finding("workflow_run_repeated")
            continue
        if target_run_id is None or run_id != target_run_id:
            continue
        if run_ended:
            finding("workflow_event_after_run_end", event_type=native_type)
            continue
        if native_type == "tool-workflow/agent-start":
            member_seq = data.get("seq")
            label = data.get("label")
            phase_id = data.get("phase")
            if (
                isinstance(member_seq, bool)
                or not isinstance(member_seq, int)
                or member_seq < 1
                or not isinstance(label, str)
                or label not in expected
            ):
                finding("workflow_agent_start_invalid")
                continue
            phase_index, expected_phase_id = expected[label]
            state = states[label]
            if phase_id != expected_phase_id:
                finding(
                    "workflow_phase_identity_invalid",
                    worker_id=label,
                    expected=expected_phase_id,
                )
                continue
            if member_seq in live:
                finding("workflow_member_sequence_repeated", worker_id=label)
                continue
            if label in live.values():
                finding("workflow_worker_duplicate_running", worker_id=label)
                continue
            if phase_index > frontier:
                finding(
                    "workflow_phase_order_violation",
                    worker_id=label,
                    phase_index=phase_index,
                    frontier_index=frontier,
                )
            elif phase_index < frontier or state["succeeded"]:
                finding("workflow_worker_repeated_after_success", worker_id=label)
            state["attempt_count"] += 1
            live[member_seq] = label
            continue
        if native_type == "tool-workflow/agent-end":
            member_seq = data.get("seq")
            outcome = data.get("outcome")
            if (
                isinstance(member_seq, bool)
                or not isinstance(member_seq, int)
                or member_seq not in live
                or outcome not in WORKFLOW_OUTCOMES
            ):
                finding("workflow_agent_end_invalid")
                continue
            label = live.pop(member_seq)
            state = states[label]
            state["outcomes"][outcome] = state["outcomes"].get(outcome, 0) + 1
            if outcome == "completed":
                state["succeeded"] = True
            advance_frontier()
            continue
        if native_type == "tool-workflow/run-end":
            stop_reason = data.get("stopReason")
            if stop_reason not in WORKFLOW_STOP_REASONS:
                finding("workflow_run_end_invalid")
            elif stop_reason != "completed":
                finding("workflow_run_failed", stop_reason=stop_reason)
            if live:
                finding("workflow_run_left_members_open", actual=len(live))
            run_ended = True

    if matching_run_count == 0:
        finding("workflow_run_missing")
    if target_run_id is not None and not run_ended:
        finding("workflow_run_end_missing")
    phase_receipts: list[dict[str, Any]] = []
    for phase in contract["phases"]:
        workers: list[dict[str, Any]] = []
        for worker in phase["workers"]:
            worker_id = worker["worker_id"]
            state = states[worker_id]
            if not state["succeeded"]:
                finding(
                    "workflow_worker_incomplete",
                    worker_id=worker_id,
                    attempt_count=state["attempt_count"],
                )
            workers.append({
                "worker_id": worker_id,
                "status": "succeeded" if state["succeeded"] else (
                    "failed" if state["attempt_count"] else "missing"
                ),
                "attempt_count": state["attempt_count"],
                "outcomes": dict(sorted(state["outcomes"].items())),
            })
        phase_receipts.append({
            "mode": phase["mode"],
            "phase_id": phase["phase_id"],
            "status": "passed" if all(
                states[row["worker_id"]]["succeeded"]
                for row in phase["workers"]
            ) else "failed",
            "workers": workers,
        })
    return {
        "schema": "chatds.deepseek-workflow-receipt.v1",
        "skill_name": contract["skill_name"],
        "route_id": contract["route_id"],
        "route_sha256": contract["route_sha256"],
        "run_name": contract["run_name"],
        "status": "passed" if not findings else "failed",
        "frontier_index": frontier,
        "finding_count": len(findings),
        "findings": findings,
        "phases": phase_receipts,
    }
