"""PID 1 controller inside one networkless, per-Turn Claude container."""

from __future__ import annotations

import base64
import hashlib
import fnmatch
import fcntl
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from chatds_browser_runtime.proxy_bridge import (
    BridgeConfigurationError,
    EXPECTED_BRIDGE_GID,
    EXPECTED_PROXY_UID,
    PROXY_CA_CERTIFICATE_PATH,
    PROXY_LEAF_SPKI_PATH,
    PROXY_SOCKET_PATH,
    LoopbackProxyBridge,
    ProxySocketAuthority,
    ProxyTrustAuthority,
)
from claude_runner.runtime_capabilities import (
    validate_runtime_capability_contract,
)
from claude_runner.native_control import (
    is_native_user_interaction,
    native_user_interaction_kind,
)
from claude_runner.input_attachments import verify_input_attachments
from claude_runner.mcp_schedule_control import (
    normalize_schedule_create,
    normalize_schedule_capability_aliases,
)
from claude_runner.native_workflow import (
    build_workflow_receipt,
    classify_native_subagent_outcome,
    terminal_workflow_receipt,
    validate_workflow_contract,
    workflow_start_violation,
    write_workflow_receipt,
)


MAX_NATIVE_LINE_BYTES = 64 * 1024 * 1024
MAX_NATIVE_INPUT_BYTES = 96 * 1024 * 1024
MAX_NATIVE_RECEIPT_DELTA_BYTES = 512 * 1024 * 1024
MAX_NATIVE_RECEIPT_COUNT = 4_096
SYNC_EVERY_EVENTS = 20
MAX_WORKSPACE_SNAPSHOT_FILES = 65_536
MAX_WORKSPACE_ARTIFACTS = 8_192
MAX_WORKSPACE_ARTIFACT_FILE_BYTES = 1024 * 1024 * 1024
MAX_WORKSPACE_ARTIFACT_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_WORKSPACE_RELATIVE_PATH_BYTES = 1024
MAX_ARTIFACT_CONTRACTS = 64
MAX_ARTIFACT_CONTRACT_FINDINGS = 128
MAX_WORKFLOW_SYNTHESIS_BASELINE_BYTES = 96 * 1024 * 1024
WORKSPACE_LOCK_IDENTITY_DOMAIN = b"chatds-workspace-mutation-lock-v1\0"
SAFE_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
SAFE_NATIVE_TASK_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
SAFE_NATIVE_TOOL_USE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
TRANSIENT_NATIVE_SUBAGENT_OUTCOMES = frozenset({
    "native_subagent_checkpoint_missing",
    "native_subagent_checkpoint_invalid",
    "native_subagent_result_incomplete",
})
NATIVE_TASK_NOTIFICATION_PREFIX = re.compile(
    r"\A<task-notification>\r?\n"
    r"<task-id>([A-Za-z0-9._:-]{1,160})</task-id>\r?\n"
    r"<tool-use-id>([A-Za-z0-9._:-]{1,256})</tool-use-id>\r?\n"
    r"<output-file>[^<\r\n]{1,4096}</output-file>\r?\n"
    r"<status>(completed|failed|killed)</status>(?:\r?\n|\Z)"
)
SAFE_NATIVE_TASK_TYPES = frozenset({
    "local_bash",
    "local_agent",
    "remote_agent",
    "in_process_teammate",
    "local_workflow",
    "monitor_mcp",
    "dream",
})
SAFE_CONTROLLER_RUNTIME_CODES = frozenset({
    "native_task_session_invalid",
    "native_task_state_invalid",
    "workspace_artifact_root_invalid",
    "workspace_artifact_path_invalid",
    "workspace_artifact_symlink_invalid",
    "workspace_artifact_type_invalid",
    "workspace_artifact_file_limit",
    "workspace_artifact_change_limit",
    "workspace_artifact_size_limit",
    "workspace_artifact_total_size_limit",
    "workspace_artifact_changed_during_audit",
    "artifact_contract_invalid",
    "artifact_contract_audit_failed",
    "artifact_contract_runtime_root_invalid",
    "workflow_contract_invalid",
    "workflow_synthesis_baseline_invalid",
    "native_cron_state_invalid",
    "egress_bridge_did_not_stop",
    "input_attachment_count_invalid",
    "input_attachment_workspace_invalid",
    "input_attachment_receipt_invalid",
    "input_attachment_path_invalid",
    "input_attachment_digest_invalid",
    "input_attachment_manifest_unreferenced",
    "input_attachment_transport_unlowered",
    "input_attachment_message_invalid",
    "turn_skill_binding_invalid",
})
SAFE_RUNNER_FATAL_CODES = frozenset({
    "runner_controller_not_root",
    "runner_event_ledger_unavailable",
    "runner_bootstrap_failed",
})
_child: subprocess.Popen[bytes] | None = None
_termination_reason: str | None = None


def main() -> int:
    controller_stage = "bootstrap"
    exit_code: int | None = None
    receipt: dict[str, Any] | None = None
    checkpoint_ready: bool | None = None
    pending_plan_task_count: int | None = None
    artifact_contract_receipt: dict[str, Any] | None = None
    workflow_terminal_receipt: dict[str, Any] | None = None
    workflow_artifact_baseline_required = False
    active_workflow_artifact_contracts: list[dict[str, Any]] = []
    if os.geteuid() != 0:
        return _fatal("runner_controller_not_root")
    try:
        # Establish the durable diagnostic boundary before parsing the larger
        # request. The dependency-free outer bootstrap covers failures that
        # occur before this installed module can load at all.
        controller_stage = "event_ledger_init"
        ledger = EventLedger(Path(os.environ["CHATDS_EVENT_LEDGER"]))
        controller_stage = "load_config"
        config = _load_config(Path(os.environ["CHATDS_RUN_CONFIG"]))
        turn_skill_binding = _turn_skill_binding(config)
        ledger.bind_schedule_capability_aliases(
            config.get("schedule_capability_aliases")
        )
        ledger.append_event({
            "type": "chatds.runtime.config",
            "context_window_tokens": int(config["context_window_tokens"]),
            "max_output_tokens": int(config["max_output_tokens"]),
            "extended_context_marker": int(config["context_window_tokens"]) > 200_000,
            "runtime_capability_contract": config.get(
                "runtime_capability_contract"
            ),
            "input_attachment_count": len(
                config.get("input_attachments") or []
            ),
            "turn_skill_binding": (
                {
                    "skill_name": turn_skill_binding[0],
                    "source": turn_skill_binding[1],
                }
                if turn_skill_binding is not None
                else None
            ),
        }, channel="controller")
        if turn_skill_binding is not None:
            ledger.append_event({
                "type": "chatds.skill.binding",
                "skill_name": turn_skill_binding[0],
                "source": turn_skill_binding[1],
                "transport": "native_slash_command",
                "required": True,
            }, channel="controller")
        for diagnostic in config.get("skill_diagnostics") or ():
            if not isinstance(diagnostic, dict):
                continue
            code = str(diagnostic.get("code") or "")
            skill_name = str(diagnostic.get("skill_name") or "")
            if (
                re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code)
                and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", skill_name)
            ):
                ledger.append_event({
                    "type": "chatds.skill.diagnostic",
                    "code": code,
                    "skill_name": skill_name,
                    "severity": "warning",
                }, channel="controller")
        worker_uid = int(os.environ.get("CLAUDE_RUNNER_WORKER_UID", "65529"))
        worker_gid = int(os.environ.get("CLAUDE_RUNNER_WORKER_GID", "65529"))
        runtime_root = Path("/runtime/worker")
        runtime_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chown(runtime_root, worker_uid, worker_gid)
        native_lifecycle_contract_path: Path | None = None
        if config.get("workflow_contract") is not None:
            controller_stage = "workflow_contract_bind"
            try:
                normalized_workflow = validate_workflow_contract(
                    config["workflow_contract"]
                )
            except ValueError as exc:
                raise RuntimeError("workflow_contract_invalid") from exc
            active_workflow_artifact_contracts = (
                _active_artifact_contract_rows(
                    config=config,
                    skill_name=str(normalized_workflow["skill_name"]),
                )
            )
            workflow_artifact_baseline_required = bool(
                active_workflow_artifact_contracts
            )
            ledger.bind_native_task_state(
                projects_root=Path("/state/home/.claude/projects"),
                native_session_id=str(config["native_session_id"]),
            )
            ledger.bind_workflow_contract(
                contract=normalized_workflow,
                receipt_path=Path("/runtime/workflow-receipt.json"),
                worker_gid=worker_gid,
                synthesis_baseline_path=(
                    Path("/runtime/workflow-synthesis-baseline.json")
                    if workflow_artifact_baseline_required
                    else None
                ),
                workspace_root=(
                    Path("/workspace")
                    if workflow_artifact_baseline_required
                    else None
                ),
                synthesis_artifact_contracts=(
                    active_workflow_artifact_contracts
                    if workflow_artifact_baseline_required
                    else None
                ),
            )
        worker_tmp = runtime_root / "tmp"
        worker_tmp.mkdir(mode=0o700)
        os.chown(worker_tmp, worker_uid, worker_gid)
        controller_stage = "workspace_lock"
        with _session_workspace_lock(config):
            controller_stage = "input_attachment_audit"
            verify_input_attachments(
                attachments=config.get("input_attachments") or [],
                workspace=Path("/workspace"),
            )
            controller_stage = "native_cron_quarantine"
            if _quarantine_native_cron_state(
                Path("/state/home/.claude"),
                str(config["run_id"]),
            ):
                ledger.append_event({
                    "type": "chatds.native-cron.quarantined",
                    "reason": "per_turn_runtime_has_no_scheduler_authority",
                }, channel="controller")
            controller_stage = "workspace_snapshot_before"
            workspace_before = _workspace_snapshot(Path("/workspace"))
            native_lifecycle_contract_path = (
                _materialize_bound_native_lifecycle_contract(
                    config=config,
                    turn_skill_binding=turn_skill_binding,
                    workspace_before=workspace_before,
                    runtime_root=Path("/runtime"),
                    controller_uid=0,
                    worker_gid=worker_gid,
                )
            )
            controller_stage = "egress_bridge_start"
            authority = ProxySocketAuthority(
                PROXY_SOCKET_PATH,
                expected_uid=EXPECTED_PROXY_UID,
                expected_gid=EXPECTED_BRIDGE_GID,
            )
            authority.validate()
            trust = ProxyTrustAuthority(
                PROXY_CA_CERTIFICATE_PATH,
                PROXY_LEAF_SPKI_PATH,
                expected_uid=EXPECTED_PROXY_UID,
                expected_gid=EXPECTED_BRIDGE_GID,
            ).materialize(runtime_root, worker_uid=worker_uid, worker_gid=worker_gid)
            policy = config["egress_policy"]
            bridge = LoopbackProxyBridge(
                authority,
                ("127.0.0.1", 0),
                origin_allowlist=tuple(policy["origin_allowlist"]),
                egress_rules=tuple(policy["egress_rules"]),
                private_origins=tuple(policy["private_origins"]),
                public_read=policy.get("public_read"),
                policy_token=os.environ.get("SKILL_EGRESS_POLICY_TOKEN"),
                trust_generation=trust["SKILL_EGRESS_TRUST_GENERATION"],
                budget_scope_sha256=policy["budget_scope_sha256"],
                call_id_sha256=policy["call_id_sha256"],
                limits=policy["limits"],
            )
            bridge_thread = threading.Thread(
                target=bridge.serve_forever,
                kwargs={"poll_interval": 0.1},
                daemon=True,
                name="claude-egress-bridge",
            )
            bridge_thread.start()
            proxy_url = f"http://127.0.0.1:{int(bridge.server_address[1])}"
            environment = _worker_environment(
                config,
                trust=trust,
                proxy_url=proxy_url,
                worker_tmp=worker_tmp,
            )
            compositor, environment, compositor_paths = _start_worker_compositor(
                environment,
                worker_uid=worker_uid,
                worker_gid=worker_gid,
            )
            try:
                controller_stage = "native_execution"
                native_transcript_watermark = _native_transcript_watermark(
                    Path("/state/home/.claude/projects"),
                    str(config["native_session_id"]),
                )
                command, prompt = _claude_command(
                    config,
                    native_lifecycle_contract_path=(
                        native_lifecycle_contract_path
                    ),
                )
                _install_signal_handlers()
                exit_code = _run_child(
                    command,
                    prompt,
                    environment,
                    worker_uid=worker_uid,
                    worker_gid=worker_gid,
                    ledger=ledger,
                    run_id=str(config["run_id"]),
                    permission_preset=str(config.get("permission_preset") or "workspace_write"),
                )
                controller_stage = "native_task_receipt_recovery"
                ledger.reconcile_native_transcript_queue(
                    projects_root=Path("/state/home/.claude/projects"),
                    native_session_id=str(config["native_session_id"]),
                    watermark=native_transcript_watermark,
                )
                ledger.reconcile_workflow_subagent_checkpoints(final=True)
            finally:
                _stop_process_group(compositor)
                for path in compositor_paths:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            controller_stage = "egress_bridge_seal"
            receipt = bridge.shutdown_and_seal()
            bridge_thread.join(timeout=5.0)
            if bridge_thread.is_alive():
                raise RuntimeError("egress_bridge_did_not_stop")
            controller_stage = "workspace_snapshot_after"
            workspace_after = _workspace_snapshot(Path("/workspace"))
            controller_stage = "workspace_artifact_audit"
            _emit_workspace_artifacts(
                ledger=ledger,
                run_id=str(config["run_id"]),
                before=workspace_before,
                after=workspace_after,
                workspace_root=Path("/workspace"),
            )
            controller_stage = "workflow_contract_audit"
            workflow_terminal_receipt = ledger.workflow_terminal_receipt
            workflow_contract_passed = (
                workflow_terminal_receipt is None
                or workflow_terminal_receipt.get("status") == "passed"
            )
            ledger.append_event({
                "type": "chatds.workflow.contract",
                "status": (
                    "not_applicable"
                    if workflow_terminal_receipt is None
                    else workflow_terminal_receipt.get("status")
                ),
                "receipt": workflow_terminal_receipt,
            }, channel="controller")
            if workflow_contract_passed:
                controller_stage = "artifact_contract_audit"
                artifact_audit_before = workspace_before
                artifact_content_before: dict[str, str] | None = None
                if workflow_artifact_baseline_required:
                    synthesis_baseline = ledger.workflow_synthesis_baseline
                    synthesis_content = (
                        ledger.workflow_synthesis_content_sha256
                    )
                    if (
                        synthesis_baseline is None
                        or synthesis_content is None
                    ):
                        raise RuntimeError(
                            "workflow_synthesis_baseline_invalid"
                        )
                    artifact_audit_before = synthesis_baseline
                    artifact_content_before = synthesis_content
                artifact_contract_receipt = _validate_artifact_contracts(
                    contracts=config.get("artifact_contracts"),
                    invoked_skill_names=ledger.invoked_skill_names,
                    bound_skill_name=(
                        turn_skill_binding[0]
                        if turn_skill_binding is not None
                        else None
                    ),
                    before=artifact_audit_before,
                    before_content_sha256=artifact_content_before,
                    after=workspace_after,
                    workspace_root=Path("/workspace"),
                )
                ledger.append_event({
                    "type": "chatds.artifact.contract",
                    **artifact_contract_receipt,
                }, channel="controller")
            else:
                artifact_contract_receipt = {
                    "status": "deferred",
                    "reason": "workflow_contract_failed",
                }
                ledger.append_event({
                    "type": "chatds.artifact.contract",
                    **artifact_contract_receipt,
                }, channel="controller")
        controller_stage = "native_checkpoint_audit"
        checkpoint_ready = _native_checkpoint_exists(
            Path("/state/home/.claude/projects"),
            str(config["native_session_id"]),
        )
        controller_stage = "native_plan_task_audit"
        pending_plan_task_count = _pending_plan_task_count(
            Path("/state/home/.claude/tasks"),
            str(config["native_session_id"]),
        )
        pending_native_task_count = ledger.active_native_task_count
        artifact_contract_passed = (
            artifact_contract_receipt is not None
            and artifact_contract_receipt.get("status") not in {
                "failed", "deferred"
            }
        )
        workflow_contract_passed = (
            workflow_terminal_receipt is None
            or workflow_terminal_receipt.get("status") == "passed"
        )
        controller_stage = "terminal_commit"
        status = (
            "cancelled"
            if _termination_reason == "cancelled"
            else "failed"
            if _termination_reason == "hard_timeout"
            else "succeeded"
            if (
                exit_code == 0
                and ledger.native_result_succeeded
                and checkpoint_ready
                and pending_native_task_count == 0
                and workflow_contract_passed
                and artifact_contract_passed
            )
            else "failed"
        )
        terminal_error = _terminal_error(
            termination_reason=_termination_reason,
            exit_code=exit_code,
            ledger=ledger,
            checkpoint_ready=checkpoint_ready,
            egress_receipt=receipt,
            pending_plan_task_count=pending_plan_task_count,
            pending_native_task_count=pending_native_task_count,
            workflow_contract_passed=workflow_contract_passed,
            artifact_contract_passed=artifact_contract_passed,
        )
        ledger.append_event({
            "type": "chatds.supervisor.terminal",
            "status": status,
            "exit_code": exit_code,
            "result_observed": ledger.saw_native_result,
            "result_succeeded": ledger.native_result_succeeded,
            "result_count": ledger.native_result_count,
            "checkpoint_observed": checkpoint_ready,
            "pending_plan_task_count": pending_plan_task_count,
            "pending_native_task_count": pending_native_task_count,
            "native_task_summary": ledger.native_task_summary,
            "pending_control_writes": list(ledger.pending_control_writes),
            "workflow_contract": workflow_terminal_receipt,
            "artifact_contract": artifact_contract_receipt,
            "error": terminal_error,
            "error_code": terminal_error,
            "error_stage": _terminal_error_stage(terminal_error),
            "egress_receipt": receipt,
        }, channel="controller", terminal=True)
        ledger.close()
        # Docker's process exit is diagnostic rather than the Turn authority,
        # but a logical native-result failure must not masquerade as a clean
        # container completion to operators or recovery tooling.
        return exit_code if status in {"succeeded", "cancelled"} or exit_code else 1
    except BaseException as exc:
        try:
            ledger
        except UnboundLocalError:
            return _fatal(
                "runner_event_ledger_unavailable"
                if controller_stage == "event_ledger_init"
                else "runner_bootstrap_failed"
            )
        terminal_event = {
            "type": "chatds.supervisor.terminal",
            "status": "failed",
            "error": type(exc).__name__,
            "error_stage": controller_stage,
            "exit_code": exit_code,
            "result_observed": ledger.saw_native_result,
            "result_succeeded": ledger.native_result_succeeded,
            "result_count": ledger.native_result_count,
            "checkpoint_observed": checkpoint_ready,
            "pending_plan_task_count": pending_plan_task_count,
            "pending_native_task_count": ledger.active_native_task_count,
            "native_task_summary": ledger.native_task_summary,
            "pending_control_writes": list(ledger.pending_control_writes),
            "workflow_contract": workflow_terminal_receipt,
            "artifact_contract": artifact_contract_receipt,
            "egress_receipt": receipt,
        }
        safe_code = _safe_controller_exception_code(exc)
        if safe_code is not None:
            terminal_event["error_code"] = safe_code
        ledger.append_event(
            terminal_event,
            channel="controller",
            terminal=True,
        )
        ledger.close()
        return 1


def _safe_controller_exception_code(exc: BaseException) -> str | None:
    """Expose only static, implementation-owned egress diagnostics.

    Arbitrary exception messages can contain paths, URLs, headers, or provider
    material, so they never enter the durable event ledger.  Bridge errors are
    emitted exclusively from fixed Harness strings and are normalized to a
    bounded code that operators can diagnose after the ephemeral container is
    removed.
    """

    if isinstance(exc, BridgeConfigurationError):
        message = str(exc)
        if re.fullmatch(r"[a-z][a-z0-9 -]{0,127}", message) is None:
            return None
        return "egress_" + re.sub(r"[ -]+", "_", message)
    if (
        isinstance(exc, RuntimeError)
        and len(exc.args) == 1
        and exc.args[0] in SAFE_CONTROLLER_RUNTIME_CODES
    ):
        return str(exc.args[0])
    return None


def _terminal_error(
    *,
    termination_reason: str | None,
    exit_code: int,
    ledger: "EventLedger",
    checkpoint_ready: bool,
    egress_receipt: dict[str, Any],
    pending_plan_task_count: int,
    pending_native_task_count: int,
    workflow_contract_passed: bool = True,
    artifact_contract_passed: bool = True,
) -> str | None:
    """Choose the most specific trusted failure signal for one Turn."""

    if termination_reason == "hard_timeout":
        return "run_hard_timeout"
    if ledger.native_result_count > 1:
        return "native_result_duplicated"
    if bool(egress_receipt.get("exhausted")):
        return "egress_budget_exhausted"
    if pending_native_task_count:
        return "native_subtasks_pending"
    # Claude's TaskCreate/TaskUpdate files are model-owned planning/UI state.
    # They are useful diagnostics, but they are not machine-owned receipts for
    # work completion.  Native process/sub-agent receipts and compiled
    # artifact contracts remain authoritative terminal gates.
    if not workflow_contract_passed:
        return "workflow_contract_failed"
    if not artifact_contract_passed:
        return "artifact_contract_failed"
    if ledger.native_api_error_status is not None:
        return f"provider_http_{ledger.native_api_error_status}"
    if exit_code == 0 and not ledger.saw_native_result:
        return "runner_exited_without_result"
    if exit_code == 0 and not ledger.native_result_succeeded:
        return "native_result_failed"
    if exit_code == 0 and not checkpoint_ready:
        return "native_checkpoint_missing"
    if exit_code != 0 and termination_reason != "cancelled":
        return "runner_exit_nonzero"
    return None


def _terminal_error_stage(error_code: str | None) -> str | None:
    if error_code is None:
        return None
    if error_code.startswith("provider_http_") or error_code in {
        "runner_exited_without_result",
        "native_result_failed",
        "native_result_duplicated",
        "runner_exit_nonzero",
    }:
        return "native_execution"
    if error_code == "native_checkpoint_missing":
        return "native_checkpoint_audit"
    if error_code == "native_subtasks_pending":
        return "native_task_audit"
    if error_code == "workflow_contract_failed":
        return "workflow_contract_audit"
    if error_code == "artifact_contract_failed":
        return "artifact_contract_audit"
    if error_code == "egress_budget_exhausted":
        return "egress_bridge_seal"
    if error_code == "run_hard_timeout":
        return "native_execution"
    return "terminal_commit"


class EventLedger:
    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise RuntimeError("event_ledger_path_invalid")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._path = path
        self._seq = 0
        self._saw_native_result = False
        self._native_result_succeeded = False
        self._native_result_count = 0
        self._native_api_error_status: int | None = None
        self._native_tasks: dict[str, dict[str, Any]] = {}
        self._native_agent_tool_types: dict[str, str] = {}
        self._native_projects_root: Path | None = None
        self._native_projects_anchor: tuple[Path, int, int] | None = None
        self._bound_native_session_id: str | None = None
        self._workflow_contract: dict[str, Any] | None = None
        self._workflow_receipt_path: Path | None = None
        self._workflow_receipt_gid: int | None = None
        self._workflow_synthesis_baseline_path: Path | None = None
        self._workflow_synthesis_workspace_root: Path | None = None
        self._workflow_synthesis_artifact_contracts: (
            list[dict[str, Any]] | None
        ) = None
        self._workflow_synthesis_baseline: (
            dict[str, tuple[int, ...]] | None
        ) = None
        self._workflow_synthesis_content_sha256: dict[str, str] | None = None
        self._workflow_violations: list[dict[str, Any]] = []
        self._task_output_calls: dict[str, str] = {}
        self._schedule_control_calls: dict[str, dict[str, Any]] = {}
        self._schedule_capability_aliases: dict[str, str] = {}
        self._pending_control_writes: list[dict[str, Any]] = []
        self._settled_control_tool_calls: set[str] = set()
        self._invoked_skill_names: set[str] = set()
        self._native_task_reconciliations = {
            "native_notification": 0,
            "task_updated": 0,
            "task_output": 0,
            "native_transcript_queue": 0,
            "controller_process_reap": 0,
        }
        if path.exists():
            raise RuntimeError("event_ledger_already_exists")
        self._stream = path.open("xb", buffering=0)
        os.chmod(path, 0o600, follow_symlinks=False)

    def append_line(self, line: bytes, *, channel: str) -> int:
        if len(line) > MAX_NATIVE_LINE_BYTES:
            return self.append_event({
                "type": "chatds.runner.diagnostic",
                "code": "native_line_too_large",
                "size_bytes": len(line),
            }, channel="controller")
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        try:
            native = json.loads(text)
        except json.JSONDecodeError:
            return self._append({"channel": channel, "text": text})
        if channel == "stdout" and isinstance(native, dict):
            self.reconcile_workflow_subagent_checkpoints(final=False)
        # Only Claude's stdout stream is part of the native stream-json
        # protocol. Stderr is untrusted diagnostic text and must never be able
        # to forge the commit candidate by printing result-shaped JSON.
        if (
            channel == "stdout"
            and isinstance(native, dict)
            and native.get("type") == "result"
        ):
            self._saw_native_result = True
            self._native_result_count += 1
            self._native_result_succeeded = (
                self._native_result_count == 1
                and native.get("subtype") == "success"
                and not bool(native.get("is_error"))
            )
            api_error_status = native.get("api_error_status")
            if (
                bool(native.get("is_error"))
                and type(api_error_status) is int
                and 400 <= api_error_status <= 599
            ):
                self._native_api_error_status = api_error_status
        if (
            channel == "stdout"
            and isinstance(native, dict)
            and native.get("type") == "system"
        ):
            subtype = str(native.get("subtype") or "")
            task_id = str(native.get("task_id") or native.get("id") or "")
            if subtype == "task_started" and task_id:
                task_type = str(native.get("task_type") or "unknown")
                if task_type not in SAFE_NATIVE_TASK_TYPES:
                    task_type = "unknown"
                if SAFE_NATIVE_TASK_ID.fullmatch(task_id):
                    tool_use_id = str(native.get("tool_use_id") or "")
                    native_session_id = str(native.get("session_id") or "")
                    native_agent_type = str(
                        native.get("subagent_type")
                        or self._native_agent_tool_types.get(tool_use_id)
                        or ""
                    )
                    if re.fullmatch(
                        r"[A-Za-z0-9._:-]{1,512}", native_agent_type
                    ) is None:
                        native_agent_type = ""
                    existing = self._native_tasks.get(task_id)
                    if existing is None:
                        if native_agent_type:
                            self._record_workflow_start_violation(
                                native_agent_type=native_agent_type
                            )
                        self._native_tasks[task_id] = {
                            "task_id": task_id,
                            "task_type": task_type,
                            "tool_use_id": (
                                tool_use_id
                                if SAFE_NATIVE_TOOL_USE_ID.fullmatch(tool_use_id)
                                else None
                            ),
                            "native_session_id": (
                                native_session_id
                                if _canonical_native_session_id(
                                    native_session_id
                                )
                                else None
                            ),
                            "native_agent_type": native_agent_type or None,
                            "status": "running",
                            "terminal_source": None,
                        }
                    elif existing.get("status") != "running":
                        existing["status"] = "running"
                        existing["terminal_source"] = None
                    self._publish_workflow_receipt()
            elif subtype == "task_updated" and task_id:
                patch = native.get("patch")
                raw_status = (
                    str(patch.get("status") or "")
                    if isinstance(patch, dict)
                    else ""
                )
                if raw_status in {"completed", "failed", "killed"}:
                    self._settle_native_task(
                        task_id,
                        status=raw_status,
                        source="task_updated",
                    )
            elif subtype in {
                "task_notification",
                "task_completed",
                "task_failed",
            } and task_id:
                status = str(native.get("status") or "")
                if subtype == "task_failed" or status == "failed":
                    status = "failed"
                elif status in {"stopped", "killed"}:
                    status = "killed"
                else:
                    status = "completed"
                self._settle_native_task(
                    task_id,
                    status=status,
                    source="native_notification",
                )
        if channel == "stdout" and isinstance(native, dict):
            if native.get("type") == "assistant":
                self._observe_assistant_tool_calls(native)
            elif native.get("type") == "user":
                self._observe_task_output_results(native)
        return self._append({"channel": channel, "event": native})

    @property
    def saw_native_result(self) -> bool:
        return self._saw_native_result

    @property
    def native_result_succeeded(self) -> bool:
        return self._native_result_succeeded

    @property
    def native_result_count(self) -> int:
        return self._native_result_count

    @property
    def native_api_error_status(self) -> int | None:
        return self._native_api_error_status

    @property
    def active_native_task_count(self) -> int:
        return sum(
            row.get("status") == "running"
            for row in self._native_tasks.values()
        )

    @property
    def invoked_skill_names(self) -> frozenset[str]:
        return frozenset(self._invoked_skill_names)

    @property
    def native_task_summary(self) -> dict[str, Any]:
        active_by_type: dict[str, int] = {}
        terminal_by_status: dict[str, int] = {}
        active_ids: list[str] = []
        for task_id in sorted(self._native_tasks):
            row = self._native_tasks[task_id]
            task_type = str(row.get("task_type") or "unknown")
            status = str(row.get("status") or "unknown")
            if status == "running":
                active_by_type[task_type] = active_by_type.get(task_type, 0) + 1
                if len(active_ids) < 64:
                    active_ids.append(task_id)
            else:
                terminal_by_status[status] = terminal_by_status.get(status, 0) + 1
        return {
            "task_count": len(self._native_tasks),
            "active_count": sum(active_by_type.values()),
            "active_by_type": dict(sorted(active_by_type.items())),
            "active_task_ids": active_ids,
            "terminal_by_status": dict(sorted(terminal_by_status.items())),
            "reconciled_by": dict(self._native_task_reconciliations),
        }

    @property
    def pending_control_writes(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(row) for row in self._pending_control_writes)

    @property
    def workflow_receipt(self) -> dict[str, Any] | None:
        if self._workflow_contract is None:
            return None
        return build_workflow_receipt(
            self._workflow_contract,
            self._native_tasks.values(),
            self._workflow_violations,
        )

    @property
    def workflow_terminal_receipt(self) -> dict[str, Any] | None:
        receipt = self.workflow_receipt
        if self._workflow_contract is None or receipt is None:
            return None
        return terminal_workflow_receipt(self._workflow_contract, receipt)

    @property
    def workflow_synthesis_baseline(
        self,
    ) -> dict[str, tuple[int, ...]] | None:
        if self._workflow_synthesis_baseline is None:
            return None
        return dict(self._workflow_synthesis_baseline)

    @property
    def workflow_synthesis_content_sha256(
        self,
    ) -> dict[str, str] | None:
        if self._workflow_synthesis_content_sha256 is None:
            return None
        return dict(self._workflow_synthesis_content_sha256)

    def bind_native_task_state(
        self,
        *,
        projects_root: Path,
        native_session_id: str,
    ) -> None:
        """Bind mandatory-worker evidence to one native Session checkpoint."""

        anchor = _native_state_directory_anchor(projects_root)
        if (
            self._native_projects_root is not None
            or not _canonical_native_session_id(native_session_id)
            or anchor is None
        ):
            raise RuntimeError("native_task_state_invalid")
        self._native_projects_root = projects_root
        self._native_projects_anchor = anchor
        self._bound_native_session_id = native_session_id

    def bind_workflow_contract(
        self,
        *,
        contract: object,
        receipt_path: Path,
        worker_gid: int | None = None,
        synthesis_baseline_path: Path | None = None,
        workspace_root: Path | None = None,
        synthesis_artifact_contracts: list[dict[str, Any]] | None = None,
    ) -> None:
        """Publish the root-owned receipt consumed by native Claude hooks."""

        if (
            self._workflow_contract is not None
            or self._native_projects_root is None
            or self._bound_native_session_id is None
            or not receipt_path.is_absolute()
            or receipt_path.exists()
            or not receipt_path.parent.is_dir()
            or receipt_path.parent.is_symlink()
            or (
                (synthesis_baseline_path is None)
                != (workspace_root is None)
            )
            or (
                (synthesis_baseline_path is None)
                != (synthesis_artifact_contracts is None)
            )
            or (
                synthesis_baseline_path is not None
                and (
                    not synthesis_baseline_path.is_absolute()
                    or synthesis_baseline_path == receipt_path
                    or synthesis_baseline_path.exists()
                    or not synthesis_baseline_path.parent.is_dir()
                    or synthesis_baseline_path.parent.is_symlink()
                )
            )
            or (
                workspace_root is not None
                and (
                    not workspace_root.is_absolute()
                    or not workspace_root.is_dir()
                    or workspace_root.is_symlink()
                )
            )
            or (
                synthesis_artifact_contracts is not None
                and (
                    not synthesis_artifact_contracts
                    or len(synthesis_artifact_contracts)
                    > MAX_ARTIFACT_CONTRACTS
                    or any(
                        not isinstance(row, dict)
                        for row in synthesis_artifact_contracts
                    )
                )
            )
            or (
                worker_gid is not None
                and (type(worker_gid) is not int or worker_gid < 0)
            )
        ):
            raise RuntimeError("workflow_contract_invalid")
        try:
            self._workflow_contract = validate_workflow_contract(contract)
        except ValueError as exc:
            raise RuntimeError("workflow_contract_invalid") from exc
        self._workflow_receipt_path = receipt_path
        self._workflow_receipt_gid = worker_gid
        self._workflow_synthesis_baseline_path = synthesis_baseline_path
        self._workflow_synthesis_workspace_root = workspace_root
        self._workflow_synthesis_artifact_contracts = (
            [dict(row) for row in synthesis_artifact_contracts]
            if synthesis_artifact_contracts is not None
            else None
        )
        self._publish_workflow_receipt()

    def bind_schedule_capability_aliases(self, value: object) -> None:
        """Bind the immutable platform I/O vocabulary before native output."""

        self._schedule_capability_aliases = (
            normalize_schedule_capability_aliases(value)
        )

    def reconcile_worker_process_exit(self) -> int:
        """Close local shell tasks after the controller reaps the Turn group.

        Claude's SDK notification is advisory and may be skipped when the
        model consumes TaskOutput directly or exits while a background shell
        is still alive.  Once PID 1 has synchronously reaped the disposable
        per-Turn process group, local Bash tasks are authoritatively killed;
        sub-agent tasks remain pending so incomplete delegated work still
        fails closed.
        """

        settled = 0
        for task_id, row in self._native_tasks.items():
            if (
                row.get("status") == "running"
                and row.get("task_type") == "local_bash"
            ):
                self._settle_native_task(
                    task_id,
                    status="killed",
                    source="controller_process_reap",
                )
                settled += 1
        if settled:
            self.append_event({
                "type": "chatds.native-task.reconciled",
                "source": "controller_process_reap",
                "task_type": "local_bash",
                "count": settled,
            }, channel="controller")
        return settled

    def reconcile_native_transcript_queue(
        self,
        *,
        projects_root: Path,
        native_session_id: str,
        watermark: dict[str, Any],
    ) -> int:
        """Recover exact native terminal envelopes omitted from SDK stdout.

        Claude persists queue operations before its headless SDK projection
        drains them.  A successful native result can therefore race with the
        final ``task_notification`` event even though the exact terminal
        envelope is already durable.  After PID 1 has reaped the Turn, accept
        only queue entries appended after the controller's pre-Turn watermark
        and bound to the exact task, tool-use, and native Session identities
        observed on stdout.  Assistant prose and sidechain content are never
        inspected or interpreted as lifecycle state.
        """

        if (
            not self._native_result_succeeded
            or self._native_result_count != 1
            or not any(
                row.get("status") == "running"
                and row.get("task_type") != "local_bash"
                for row in self._native_tasks.values()
            )
        ):
            return 0
        try:
            lines = _native_transcript_delta_lines(
                projects_root=projects_root,
                native_session_id=native_session_id,
                watermark=watermark,
            )
            candidates: dict[str, set[str]] = {}
            candidate_count = 0
            for raw_line in lines:
                if len(raw_line) > MAX_NATIVE_LINE_BYTES:
                    raise RuntimeError("native_task_state_invalid")
                try:
                    record = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                receipt = _native_queue_terminal_receipt(
                    record,
                    native_session_id=native_session_id,
                )
                if receipt is None:
                    continue
                task_id, tool_use_id, status = receipt
                row = self._native_tasks.get(task_id)
                if (
                    row is None
                    or row.get("status") != "running"
                    or row.get("task_type") == "local_bash"
                    or row.get("tool_use_id") != tool_use_id
                    or row.get("native_session_id") != native_session_id
                ):
                    continue
                candidate_count += 1
                if candidate_count > MAX_NATIVE_RECEIPT_COUNT:
                    raise RuntimeError("native_task_state_invalid")
                candidates.setdefault(task_id, set()).add(status)
        except (OSError, RuntimeError, ValueError):
            self.append_event({
                "type": "chatds.native-task.recovery",
                "status": "unavailable",
                "code": "native_task_state_invalid",
            }, channel="controller")
            return 0

        settled = 0
        status_counts: dict[str, int] = {}
        for task_id in sorted(candidates):
            statuses = candidates[task_id]
            if len(statuses) != 1:
                continue
            status = next(iter(statuses))
            self._settle_native_task(
                task_id,
                status=status,
                source="native_transcript_queue",
            )
            settled += 1
            status_counts[status] = status_counts.get(status, 0) + 1
        if settled:
            self.append_event({
                "type": "chatds.native-task.reconciled",
                "source": "native_transcript_queue",
                "count": settled,
                "terminal_by_status": dict(sorted(status_counts.items())),
            }, channel="controller")
        return settled

    def _settle_native_task(
        self,
        task_id: str,
        *,
        status: str,
        source: str,
    ) -> None:
        if SAFE_NATIVE_TASK_ID.fullmatch(task_id) is None:
            return
        row = self._native_tasks.get(task_id)
        native_terminal_source = source
        if row is None:
            row = {
                "task_id": task_id,
                "task_type": "unknown",
                "status": status,
                "terminal_source": source,
            }
            self._native_tasks[task_id] = row
        elif row.get("status") == "running":
            if (
                status == "completed"
                and row.get("task_type") == "local_agent"
                and self._is_mandatory_workflow_worker(
                    str(row.get("native_agent_type") or "")
                )
            ):
                status, source = self._classify_bound_subagent(row)
                if (
                    status == "failed"
                    and source in TRANSIENT_NATIVE_SUBAGENT_OUTCOMES
                ):
                    row["terminal_claim_status"] = "completed"
                    row["terminal_claim_source"] = native_terminal_source
                    row["checkpoint_source"] = source
                    if (
                        native_terminal_source
                        in self._native_task_reconciliations
                        and not row.get("terminal_claim_counted")
                    ):
                        self._native_task_reconciliations[
                            native_terminal_source
                        ] += 1
                        row["terminal_claim_counted"] = True
                    self._publish_workflow_receipt()
                    return
            row["status"] = status
            row["terminal_source"] = source
            row["native_terminal_source"] = native_terminal_source
            row.pop("terminal_claim_status", None)
            row.pop("terminal_claim_source", None)
            row.pop("checkpoint_source", None)
        else:
            return
        if (
            native_terminal_source in self._native_task_reconciliations
            and not row.get("terminal_claim_counted")
        ):
            self._native_task_reconciliations[native_terminal_source] += 1
        self._publish_workflow_receipt()
        if self._is_mandatory_workflow_worker(
            str(row.get("native_agent_type") or "")
        ):
            self.append_event({
                "type": "chatds.workflow.worker-settled",
                "task_id": task_id,
                "native_agent_type": row.get("native_agent_type"),
                "status": row.get("status"),
                "terminal_source": row.get("terminal_source"),
            }, channel="controller")

    def reconcile_workflow_subagent_checkpoints(
        self, *, final: bool
    ) -> int:
        """Settle native terminal claims once their async writes are durable."""

        settled = 0
        for task_id in sorted(self._native_tasks):
            row = self._native_tasks[task_id]
            if (
                row.get("status") != "running"
                or row.get("terminal_claim_status") != "completed"
                or not self._is_mandatory_workflow_worker(
                    str(row.get("native_agent_type") or "")
                )
            ):
                continue
            status, source = self._classify_bound_subagent(row)
            if (
                status != "completed"
                and source in TRANSIENT_NATIVE_SUBAGENT_OUTCOMES
                and not final
            ):
                row["checkpoint_source"] = source
                continue
            row["status"] = status
            row["terminal_source"] = source
            row["native_terminal_source"] = row.get(
                "terminal_claim_source"
            )
            row.pop("terminal_claim_status", None)
            row.pop("terminal_claim_source", None)
            row.pop("checkpoint_source", None)
            settled += 1
            self.append_event({
                "type": "chatds.workflow.worker-settled",
                "task_id": task_id,
                "native_agent_type": row.get("native_agent_type"),
                "status": row.get("status"),
                "terminal_source": row.get("terminal_source"),
            }, channel="controller")
        if settled:
            self._publish_workflow_receipt()
        return settled

    def _classify_bound_subagent(
        self, row: dict[str, Any]
    ) -> tuple[str, str]:
        if (
            self._native_projects_root is None
            or not self._bound_native_projects_root_ready()
            or self._bound_native_session_id is None
        ):
            return "failed", "native_subagent_checkpoint_missing"
        return classify_native_subagent_outcome(
            projects_root=self._native_projects_root,
            native_session_id=self._bound_native_session_id,
            task=row,
        )

    def _bound_native_projects_root_ready(self) -> bool:
        if (
            self._native_projects_root is None
            or self._native_projects_anchor is None
        ):
            return False
        anchor, expected_device, expected_inode = self._native_projects_anchor
        try:
            anchor_info = os.lstat(anchor)
            if (
                not stat.S_ISDIR(anchor_info.st_mode)
                or anchor.is_symlink()
                or anchor_info.st_dev != expected_device
                or anchor_info.st_ino != expected_inode
            ):
                return False
            relative = self._native_projects_root.relative_to(anchor)
            current = anchor
            for part in relative.parts:
                current = current / part
                info = os.lstat(current)
                if not stat.S_ISDIR(info.st_mode) or current.is_symlink():
                    return False
        except (OSError, ValueError):
            return False
        return True

    def _record_workflow_start_violation(
        self, *, native_agent_type: str
    ) -> None:
        receipt = self.workflow_receipt
        if self._workflow_contract is None or receipt is None:
            return
        try:
            violation = workflow_start_violation(
                self._workflow_contract,
                receipt,
                native_agent_type=native_agent_type,
            )
        except ValueError as exc:
            raise RuntimeError("workflow_contract_invalid") from exc
        if violation is None or violation in self._workflow_violations:
            return
        if len(self._workflow_violations) >= 256:
            raise RuntimeError("workflow_contract_invalid")
        self._workflow_violations.append(violation)
        self.append_event({
            "type": "chatds.workflow.violation",
            **violation,
        }, channel="controller")

    def _is_mandatory_workflow_worker(
        self, native_agent_type: str
    ) -> bool:
        if self._workflow_contract is None or not native_agent_type:
            return False
        return any(
            worker.get("native_agent_type") == native_agent_type
            for phase in self._workflow_contract["phases"]
            for worker in phase["workers"]
        )

    def _publish_workflow_receipt(self) -> None:
        if (
            self._workflow_contract is None
            or self._workflow_receipt_path is None
        ):
            return
        receipt = build_workflow_receipt(
            self._workflow_contract,
            self._native_tasks.values(),
            self._workflow_violations,
        )
        try:
            if (
                receipt["status"] == "passed"
                and self._workflow_synthesis_baseline_path is not None
                and self._workflow_synthesis_workspace_root is not None
                and self._workflow_synthesis_artifact_contracts is not None
                and self._workflow_synthesis_baseline is None
            ):
                baseline = _workspace_snapshot(
                    self._workflow_synthesis_workspace_root
                )
                content_sha256 = _workflow_final_content_sha256(
                    workspace_root=self._workflow_synthesis_workspace_root,
                    snapshot=baseline,
                    artifact_contracts=(
                        self._workflow_synthesis_artifact_contracts
                    ),
                )
                _write_workflow_synthesis_baseline(
                    path=self._workflow_synthesis_baseline_path,
                    workflow=self._workflow_contract,
                    workspace_before=baseline,
                    final_content_sha256=content_sha256,
                    group_gid=self._workflow_receipt_gid,
                )
                self._workflow_synthesis_baseline = baseline
                self._workflow_synthesis_content_sha256 = content_sha256
                self.append_event({
                    "type": "chatds.workflow.synthesis-baseline",
                    "status": "committed",
                    "skill_name": self._workflow_contract["skill_name"],
                    "route_id": self._workflow_contract["route_id"],
                    "route_sha256": self._workflow_contract["route_sha256"],
                    "file_count": len(baseline),
                    "declared_final_count": len(content_sha256),
                }, channel="controller")
            write_workflow_receipt(
                self._workflow_receipt_path,
                receipt,
                owner_uid=0 if os.geteuid() == 0 else None,
                group_gid=self._workflow_receipt_gid,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError("workflow_contract_invalid") from exc

    def _observe_assistant_tool_calls(self, native: dict[str, Any]) -> None:
        message = native.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in {
                "tool_use", "server_tool_use",
            }:
                continue
            name = str(block.get("name") or "")
            tool_use_id = str(block.get("id") or "")
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                continue
            if name in {"TaskOutput", "AgentOutputTool", "BashOutputTool"}:
                task_id = str(arguments.get("task_id") or "")
                if (
                    tool_use_id
                    and SAFE_NATIVE_TASK_ID.fullmatch(task_id)
                    and len(tool_use_id) <= 256
                ):
                    self._task_output_calls[tool_use_id] = task_id
            elif name in {"Agent", "Task"}:
                native_agent_type = str(arguments.get("subagent_type") or "")
                if (
                    SAFE_NATIVE_TOOL_USE_ID.fullmatch(tool_use_id)
                    and re.fullmatch(
                        r"[A-Za-z0-9._:-]{1,512}", native_agent_type
                    )
                ):
                    self._native_agent_tool_types[
                        tool_use_id
                    ] = native_agent_type
            elif name == "Skill":
                raw_skill = str(
                    arguments.get("skill")
                    or arguments.get("name")
                    or arguments.get("command")
                    or ""
                ).strip().lstrip("/")
                skill_name = raw_skill.rsplit(":", 1)[-1]
                if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", skill_name):
                    self._invoked_skill_names.add(skill_name)
            elif (
                name == "mcp__chatds-schedule__schedule_create"
                and tool_use_id
                and len(tool_use_id) <= 256
            ):
                try:
                    normalized = normalize_schedule_create(
                        arguments,
                        capability_aliases=(
                            self._schedule_capability_aliases
                        ),
                    )
                except (TypeError, ValueError):
                    continue
                self._schedule_control_calls[tool_use_id] = normalized

    def _observe_task_output_results(self, native: dict[str, Any]) -> None:
        message = native.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = str(block.get("tool_use_id") or "")
            task_id = self._task_output_calls.get(tool_use_id)
            if not task_id or bool(block.get("is_error")):
                self._observe_schedule_control_result(block)
                continue
            result_text = _tool_result_text(block.get("content"))
            if len(result_text) > 1_000_000:
                continue
            retrieval = re.search(
                r"<retrieval_status>\s*([^<]+?)\s*</retrieval_status>",
                result_text,
            )
            status_match = re.search(
                r"<status>\s*(completed|failed|killed)\s*</status>",
                result_text,
            )
            result_task = re.search(
                r"<task_id>\s*([^<]+?)\s*</task_id>",
                result_text,
            )
            if (
                retrieval is None
                or retrieval.group(1).strip() != "success"
                or status_match is None
                or result_task is None
                or result_task.group(1).strip() != task_id
            ):
                continue
            self._settle_native_task(
                task_id,
                status=status_match.group(1),
                source="task_output",
            )
            self._observe_schedule_control_result(block)

    def _observe_schedule_control_result(self, block: dict[str, Any]) -> None:
        tool_use_id = str(block.get("tool_use_id") or "")
        request = self._schedule_control_calls.get(tool_use_id)
        if (
            request is None
            or tool_use_id in self._settled_control_tool_calls
            or bool(block.get("is_error"))
        ):
            return
        result_text = _tool_result_text(block.get("content"))
        if len(result_text) > 16_384:
            return
        try:
            receipt = json.loads(result_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "chatds.schedule.accepted.v1"
            or receipt.get("status") != "accepted_pending_terminal_commit"
        ):
            return
        canonical = json.dumps(
            request, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if receipt.get("request_sha256") != hashlib.sha256(canonical).hexdigest():
            return
        self._settled_control_tool_calls.add(tool_use_id)
        self._pending_control_writes.append({
            "schema": "chatds.schedule-write.v1",
            "operation": "create",
            "tool_call_id": tool_use_id,
            "request": request,
        })

    def append_event(
        self,
        event: dict[str, Any],
        *,
        channel: str,
        terminal: bool = False,
    ) -> int:
        return self._append(
            {"channel": channel, "event": event}, terminal=terminal
        )

    def close(self) -> None:
        if not self._stream.closed:
            os.fsync(self._stream.fileno())
            self._stream.close()

    def _append(self, value: dict[str, Any], *, terminal: bool = False) -> int:
        self._seq += 1
        envelope = {
            "seq": self._seq,
            "received_at_unix_ms": int(time.time() * 1000),
            **value,
        }
        encoded = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        view = memoryview(encoded)
        written = 0
        while written < len(view):
            count = self._stream.write(view[written:])
            if count is None or count <= 0:
                raise OSError("event_ledger_short_write")
            written += count
        if terminal or self._seq % SYNC_EVERY_EVENTS == 0:
            os.fsync(self._stream.fileno())
        return self._seq


def _load_config(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("run_config_path_invalid")
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
        raise RuntimeError("run_config_invalid")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != "chatds.claude-run.v1":
        raise RuntimeError("run_config_invalid")
    return payload


def _canonical_native_session_id(native_session_id: str) -> bool:
    try:
        uuid_value = uuid.UUID(native_session_id)
    except (ValueError, TypeError, AttributeError):
        return False
    return str(uuid_value) == native_session_id


def _native_state_directory_anchor(
    path: Path,
) -> tuple[Path, int, int] | None:
    """Freeze the nearest real ancestor without creating native state."""

    if not path.is_absolute():
        return None
    current = path
    missing_components = 0
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            missing_components += 1
            if missing_components > 8 or current.parent == current:
                return None
            current = current.parent
            continue
        except OSError:
            return None
        if (
            current.parent == current
            or not stat.S_ISDIR(info.st_mode)
            or current.is_symlink()
        ):
            return None
        return current, info.st_dev, info.st_ino


def _native_checkpoint_path(
    projects_root: Path,
    native_session_id: str,
) -> Path | None:
    if not _canonical_native_session_id(native_session_id):
        return None
    if not projects_root.is_dir() or projects_root.is_symlink():
        return None
    matches = list(projects_root.glob(f"*/{native_session_id}.jsonl"))
    if len(matches) != 1:
        return None
    path = matches[0]
    try:
        parent_info = os.lstat(path.parent)
        info = os.lstat(path)
    except OSError:
        return None
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or path.parent.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
    ):
        return None
    try:
        relative = path.relative_to(projects_root)
    except ValueError:
        return None
    if len(relative.parts) != 2:
        return None
    return path


def _native_checkpoint_exists(projects_root: Path, native_session_id: str) -> bool:
    """Require one regular transcript for the transaction-local Session ID."""

    path = _native_checkpoint_path(projects_root, native_session_id)
    if path is None:
        return False
    try:
        return os.lstat(path).st_size > 0
    except OSError:
        return False


def _native_transcript_watermark(
    projects_root: Path,
    native_session_id: str,
) -> dict[str, Any]:
    """Freeze the append boundary for one native Session transcript."""

    if not _canonical_native_session_id(native_session_id):
        return {"state": "invalid"}
    if not projects_root.exists():
        return {"state": "absent"}
    if not projects_root.is_dir() or projects_root.is_symlink():
        return {"state": "invalid"}
    matches = list(projects_root.glob(f"*/{native_session_id}.jsonl"))
    if not matches:
        return {"state": "absent"}
    path = _native_checkpoint_path(projects_root, native_session_id)
    if path is None:
        return {"state": "invalid"}
    try:
        info = os.lstat(path)
        relative = path.relative_to(projects_root).as_posix()
    except (OSError, ValueError):
        return {"state": "invalid"}
    return {
        "state": "present",
        "relative_path": relative,
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
    }


def _native_transcript_delta_lines(
    *,
    projects_root: Path,
    native_session_id: str,
    watermark: dict[str, Any],
) -> Iterator[bytes]:
    """Yield only transcript records appended during the current Turn."""

    if watermark.get("state") not in {"absent", "present"}:
        raise RuntimeError("native_task_state_invalid")
    path = _native_checkpoint_path(projects_root, native_session_id)
    if path is None:
        raise RuntimeError("native_task_state_invalid")
    try:
        info = os.lstat(path)
        relative = path.relative_to(projects_root).as_posix()
    except (OSError, ValueError) as exc:
        raise RuntimeError("native_task_state_invalid") from exc

    offset = 0
    if watermark.get("state") == "present":
        if (
            watermark.get("relative_path") != relative
            or watermark.get("device") != info.st_dev
            or watermark.get("inode") != info.st_ino
            or type(watermark.get("size")) is not int
        ):
            raise RuntimeError("native_task_state_invalid")
        offset = int(watermark["size"])
    if (
        offset < 0
        or info.st_size < offset
        or info.st_size - offset > MAX_NATIVE_RECEIPT_DELTA_BYTES
    ):
        raise RuntimeError("native_task_state_invalid")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
            or opened.st_size != info.st_size
        ):
            raise RuntimeError("native_task_state_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            if offset:
                stream.seek(offset - 1)
                if stream.read(1) != b"\n":
                    raise RuntimeError("native_task_state_invalid")
            stream.seek(offset)
            observed = 0
            for line in stream:
                observed += len(line)
                if observed > MAX_NATIVE_RECEIPT_DELTA_BYTES:
                    raise RuntimeError("native_task_state_invalid")
                yield line
            if stream.tell() != info.st_size:
                raise RuntimeError("native_task_state_invalid")
    finally:
        os.close(descriptor)


def _native_queue_terminal_receipt(
    record: object,
    *,
    native_session_id: str,
) -> tuple[str, str, str] | None:
    """Parse the machine header of one persisted native queue terminal."""

    if (
        not isinstance(record, dict)
        or record.get("type") != "queue-operation"
        or record.get("operation") != "enqueue"
        or record.get("sessionId") != native_session_id
    ):
        return None
    content = record.get("content")
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_NATIVE_LINE_BYTES:
        return None
    if not content.rstrip().endswith("</task-notification>"):
        return None
    match = NATIVE_TASK_NOTIFICATION_PREFIX.match(content)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def _workspace_snapshot(workspace_root: Path) -> dict[str, tuple[int, ...]]:
    """Capture a bounded, no-follow workspace identity snapshot.

    Content hashing every historical attachment before every Turn is
    needlessly expensive.  The kernel-owned ctime component cannot be restored
    by the worker, so the full regular-file identity tuple reliably selects
    new or mutated files for the post-Turn content-addressed artifact ledger.
    """

    root_info = os.lstat(workspace_root)
    if not stat.S_ISDIR(root_info.st_mode) or workspace_root.is_symlink():
        raise RuntimeError("workspace_artifact_root_invalid")
    result: dict[str, tuple[int, ...]] = {}
    pending = [workspace_root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                path = Path(entry.path)
                relative = path.relative_to(workspace_root).as_posix()
                if (
                    not relative
                    or len(relative.encode("utf-8"))
                    > MAX_WORKSPACE_RELATIVE_PATH_BYTES
                ):
                    raise RuntimeError("workspace_artifact_path_invalid")
                if stat.S_ISLNK(info.st_mode):
                    raise RuntimeError("workspace_artifact_symlink_invalid")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise RuntimeError("workspace_artifact_type_invalid")
                result[relative] = (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
                if len(result) > MAX_WORKSPACE_SNAPSHOT_FILES:
                    raise RuntimeError("workspace_artifact_file_limit")
    return result


def _write_workflow_synthesis_baseline(
    *,
    path: Path,
    workflow: dict[str, Any],
    workspace_before: dict[str, tuple[int, ...]],
    final_content_sha256: dict[str, str],
    group_gid: int | None,
) -> None:
    """Atomically publish the first passed-frontier workspace identity.

    PreToolUse path recognition remains useful feedback, but arbitrary shell
    languages and background processes make it unsuitable as the artifact
    authority.  This root-owned checkpoint gives both the native Stop hook
    and terminal controller the same monotonic synthesis epoch.
    """

    if (
        not path.is_absolute()
        or path.exists()
        or not path.parent.is_dir()
        or path.parent.is_symlink()
        or (
            group_gid is not None
            and (type(group_gid) is not int or group_gid < 0)
        )
    ):
        raise RuntimeError("workflow_synthesis_baseline_invalid")
    payload = {
        "schema": "chatds.workflow-synthesis-baseline.v1",
        "skill_name": workflow["skill_name"],
        "route_id": workflow["route_id"],
        "route_sha256": workflow["route_sha256"],
        "workspace_before": workspace_before,
        "final_content_sha256": final_content_sha256,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_WORKFLOW_SYNTHESIS_BASELINE_BYTES:
        raise RuntimeError("workflow_synthesis_baseline_invalid")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0),
            0o440,
        )
        view = memoryview(encoded)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("workflow_synthesis_baseline_short_write")
            offset += written
        if os.geteuid() == 0 or group_gid is not None:
            os.fchown(
                descriptor,
                0 if os.geteuid() == 0 else -1,
                -1 if group_gid is None else group_gid,
            )
        os.fchmod(descriptor, 0o440)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise RuntimeError(
            "workflow_synthesis_baseline_invalid"
        ) from exc


def _workflow_final_content_sha256(
    *,
    workspace_root: Path,
    snapshot: dict[str, tuple[int, ...]],
    artifact_contracts: list[dict[str, Any]],
) -> dict[str, str]:
    """Hash only declared final candidates at the passed frontier."""

    patterns = {
        _workspace_contract_pattern(row.get("declared_final_artifact"))
        for row in artifact_contracts
    }
    matches = sorted(
        relative
        for relative in snapshot
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)
    )
    if len(matches) > MAX_WORKSPACE_ARTIFACTS:
        raise RuntimeError("workflow_synthesis_baseline_invalid")
    result: dict[str, str] = {}
    total_bytes = 0
    for relative in matches:
        identity = snapshot[relative]
        size = int(identity[3])
        if size > MAX_WORKSPACE_ARTIFACT_FILE_BYTES:
            raise RuntimeError("workspace_artifact_size_limit")
        total_bytes += size
        if total_bytes > MAX_WORKSPACE_ARTIFACT_TOTAL_BYTES:
            raise RuntimeError("workspace_artifact_total_size_limit")
        result[relative] = _stable_file_sha256(
            workspace_root / relative,
            expected_identity=identity,
        )
    return result


def _stable_file_sha256(
    path: Path,
    *,
    expected_identity: tuple[int, ...],
) -> str:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        initial = os.fstat(descriptor)
        initial_identity = (
            initial.st_dev,
            initial.st_ino,
            initial.st_mode,
            initial.st_size,
            initial.st_mtime_ns,
            initial.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial_identity != expected_identity
        ):
            raise RuntimeError("workspace_artifact_changed_during_audit")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        final = os.fstat(descriptor)
        final_identity = (
            final.st_dev,
            final.st_ino,
            final.st_mode,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        )
        if final_identity != expected_identity:
            raise RuntimeError("workspace_artifact_changed_during_audit")
    finally:
        os.close(descriptor)
    current = os.lstat(path)
    current_identity = (
        current.st_dev,
        current.st_ino,
        current.st_mode,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )
    if (
        not stat.S_ISREG(current.st_mode)
        or current_identity != expected_identity
    ):
        raise RuntimeError("workspace_artifact_changed_during_audit")
    return digest.hexdigest()


def _emit_workspace_artifacts(
    *,
    ledger: "EventLedger",
    run_id: str,
    before: dict[str, tuple[int, ...]],
    after: dict[str, tuple[int, ...]],
    workspace_root: Path,
) -> None:
    changed = sorted(
        relative
        for relative, identity in after.items()
        if before.get(relative) != identity
    )
    if len(changed) > MAX_WORKSPACE_ARTIFACTS:
        raise RuntimeError("workspace_artifact_change_limit")
    total_bytes = 0
    for relative in changed:
        size = after[relative][3]
        if size > MAX_WORKSPACE_ARTIFACT_FILE_BYTES:
            raise RuntimeError("workspace_artifact_size_limit")
        total_bytes += size
        if total_bytes > MAX_WORKSPACE_ARTIFACT_TOTAL_BYTES:
            raise RuntimeError("workspace_artifact_total_size_limit")
        path = workspace_root / relative
        sha256 = _stable_file_sha256(
            path,
            expected_identity=after[relative],
        )
        artifact_identity = hashlib.sha256(
            (
                "chatds.claude.workspace-artifact.v1\0"
                + run_id
                + "\0"
                + relative
                + "\0"
                + sha256
            ).encode("utf-8")
        ).hexdigest()
        ledger.append_event({
            "type": "chatds.workspace.artifact",
            "path": relative,
            "title": Path(relative).name,
            "kind": "file",
            "size_bytes": size,
            "sha256": sha256,
            "source_event_key": (
                f"claude-workspace:{run_id}:{artifact_identity}"
            ),
        }, channel="controller")


def _tool_result_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


def _workspace_contract_pattern(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeError("artifact_contract_invalid")
    pattern = value.strip().replace("\\", "/")
    if (
        not pattern
        or pattern.startswith("/")
        or "\x00" in pattern
        or len(pattern.encode("utf-8")) > MAX_WORKSPACE_RELATIVE_PATH_BYTES
        or any(part in {"", ".", ".."} for part in pattern.split("/"))
    ):
        raise RuntimeError("artifact_contract_invalid")
    # Skill placeholders are data, not Harness policy.  At validation time
    # each bounded placeholder denotes exactly one path-segment wildcard.
    pattern = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]{0,63}\}", "*", pattern)
    if "{" in pattern or "}" in pattern:
        raise RuntimeError("artifact_contract_invalid")
    return pattern


def _active_artifact_contract_rows(
    *,
    config: dict[str, Any],
    skill_name: str,
) -> list[dict[str, Any]]:
    """Select immutable artifact declarations for one bound Skill."""

    rows = config.get("artifact_contracts")
    if rows is None:
        rows = []
    if (
        re.fullmatch(r"[A-Za-z0-9._-]{1,128}", skill_name) is None
        or not isinstance(rows, list)
        or len(rows) > MAX_ARTIFACT_CONTRACTS
    ):
        raise RuntimeError("artifact_contract_invalid")
    active: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("artifact_contract_invalid")
        if str(row.get("skill_name") or "") == skill_name:
            active.append(dict(row))
    return active


def _materialize_bound_artifact_stop_contract(
    *,
    config: dict[str, Any],
    turn_skill_binding: tuple[str, str] | None,
    workspace_before: dict[str, tuple[int, ...]],
    runtime_root: Path,
    controller_uid: int,
    worker_gid: int,
) -> Path | None:
    """Publish one worker-readable contract for Claude's native Stop hook."""

    if turn_skill_binding is None:
        return None
    rows = config.get("artifact_contracts")
    if rows is None:
        rows = []
    if not isinstance(rows, list) or len(rows) > MAX_ARTIFACT_CONTRACTS:
        raise RuntimeError("artifact_contract_invalid")
    skill_name = turn_skill_binding[0]
    active: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("artifact_contract_invalid")
        if str(row.get("skill_name") or "") == skill_name:
            active.append(dict(row))
    if not active:
        return None
    root_info = os.lstat(runtime_root)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or runtime_root.is_symlink()
        or root_info.st_uid != controller_uid
        or root_info.st_mode & 0o022
    ):
        raise RuntimeError("artifact_contract_runtime_root_invalid")
    payload = {
        "schema": "chatds.artifact-stop-contract.v1",
        "skill_name": skill_name,
        "contracts": active,
        # Preserve the same pre-execution frontier used by the authoritative
        # terminal audit.  Existing attachments or prior Session artifacts
        # must never satisfy a fresh Turn merely because the hook runs later.
        "workspace_before": workspace_before,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > 4 * 1024 * 1024:
        raise RuntimeError("artifact_contract_invalid")
    path = runtime_root / "artifact-stop-contract.json"
    with path.open("xb", buffering=0) as stream:
        stream.write(encoded)
        os.fsync(stream.fileno())
    os.chown(path, controller_uid, worker_gid)
    os.chmod(path, 0o440)
    return path


def _materialize_bound_native_lifecycle_contract(
    *,
    config: dict[str, Any],
    turn_skill_binding: tuple[str, str] | None,
    workspace_before: dict[str, tuple[int, ...]],
    runtime_root: Path,
    controller_uid: int,
    worker_gid: int,
) -> Path | None:
    """Publish one immutable contract for native workflow/artifact hooks."""

    bound_skill_name = (
        turn_skill_binding[0] if turn_skill_binding is not None else None
    )

    raw_workflow = config.get("workflow_contract")
    workflow: dict[str, Any] | None = None
    if raw_workflow is not None:
        try:
            workflow = validate_workflow_contract(raw_workflow)
        except ValueError as exc:
            raise RuntimeError("workflow_contract_invalid") from exc
        workflow_skill_name = str(workflow["skill_name"])
        if (
            bound_skill_name is not None
            and workflow_skill_name != bound_skill_name
        ):
            raise RuntimeError("workflow_contract_invalid")
        bound_skill_name = workflow_skill_name
    active_artifacts = (
        _active_artifact_contract_rows(
            config=config,
            skill_name=bound_skill_name,
        )
        if bound_skill_name is not None
        else []
    )
    if not active_artifacts and workflow is None:
        return None
    if bound_skill_name is None:
        raise RuntimeError("workflow_contract_invalid")

    root_info = os.lstat(runtime_root)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or runtime_root.is_symlink()
        or root_info.st_uid != controller_uid
        or root_info.st_mode & 0o022
    ):
        raise RuntimeError("artifact_contract_runtime_root_invalid")
    payload = {
        "schema": "chatds.native-lifecycle-contract.v1",
        "skill_name": bound_skill_name,
        "artifact_contracts": active_artifacts,
        "workspace_before": workspace_before,
        "workflow_contract": workflow,
        "workflow_receipt_path": (
            "/runtime/workflow-receipt.json"
            if workflow is not None
            else None
        ),
        "workflow_synthesis_baseline_path": (
            "/runtime/workflow-synthesis-baseline.json"
            if workflow is not None and active_artifacts
            else None
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > 8 * 1024 * 1024:
        raise RuntimeError("workflow_contract_invalid")
    path = runtime_root / "native-lifecycle-contract.json"
    with path.open("xb", buffering=0) as stream:
        stream.write(encoded)
        os.fsync(stream.fileno())
    os.chown(path, controller_uid, worker_gid)
    os.chmod(path, 0o440)
    return path


def _artifact_text_stats(path: Path) -> tuple[int, int]:
    """Count lines and H1/H2 headings without buffering unbounded lines."""

    line_count = 0
    heading_count = 0
    prefix = bytearray()
    current_has_bytes = False

    def finish_line() -> None:
        nonlocal line_count, heading_count, prefix, current_has_bytes
        line_count += 1
        if re.match(rb" {0,3}#{1,2}[ \t]+\S", bytes(prefix)):
            heading_count += 1
        prefix = bytearray()
        current_has_bytes = False

    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            parts = chunk.split(b"\n")
            for index, part in enumerate(parts):
                if part:
                    current_has_bytes = True
                    if len(prefix) < 1024:
                        prefix.extend(part[:1024 - len(prefix)])
                if index < len(parts) - 1:
                    finish_line()
    if current_has_bytes:
        finish_line()
    return line_count, heading_count


def _validate_artifact_contracts(
    *,
    contracts: object,
    invoked_skill_names: frozenset[str],
    before: dict[str, tuple[int, ...]],
    before_content_sha256: dict[str, str] | None = None,
    after: dict[str, tuple[int, ...]],
    workspace_root: Path,
    bound_skill_name: str | None = None,
) -> dict[str, Any]:
    """Validate only contracts for Skills actually invoked this Turn.

    Installed Skills remain ambient capabilities and must not force unrelated
    turns to create artifacts.  A native ``Skill`` tool receipt activates the
    corresponding immutable contract.  Final deliverables must be created or
    content-mutated after the supplied authority baseline; declared supporting
    modules may come from an earlier failed Turn in the same Session so
    continuation recovery remains possible.
    """

    if contracts is None:
        rows: list[object] = []
    elif isinstance(contracts, list) and len(contracts) <= MAX_ARTIFACT_CONTRACTS:
        rows = contracts
    else:
        raise RuntimeError("artifact_contract_invalid")
    active_skill_names = set(invoked_skill_names)
    if bound_skill_name is not None:
        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", bound_skill_name) is None:
            raise RuntimeError("artifact_contract_invalid")
        active_skill_names.add(bound_skill_name)
    active: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("artifact_contract_invalid")
        skill_name = str(row.get("skill_name") or "")
        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", skill_name) is None:
            raise RuntimeError("artifact_contract_invalid")
        if skill_name in active_skill_names:
            active.append(row)
    if not active:
        return {
            "status": "not_applicable",
            "activated_contract_count": 0,
            "finding_count": 0,
            "findings": [],
        }

    changed = {
        path for path, identity in after.items() if before.get(path) != identity
    }
    if before_content_sha256 is None:
        content_baseline: dict[str, str] = {}
    elif (
        isinstance(before_content_sha256, dict)
        and len(before_content_sha256) <= MAX_WORKSPACE_ARTIFACTS
        and all(
            isinstance(path, str)
            and path in before
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            for path, digest in before_content_sha256.items()
        )
    ):
        content_baseline = dict(before_content_sha256)
    else:
        raise RuntimeError("workflow_synthesis_baseline_invalid")
    findings: list[dict[str, Any]] = []
    validated: list[dict[str, Any]] = []

    def finding(code: str, **values: Any) -> None:
        if len(findings) < MAX_ARTIFACT_CONTRACT_FINDINGS:
            findings.append({"code": code, **values})

    for row in active:
        skill_name = str(row["skill_name"])
        final_pattern = _workspace_contract_pattern(
            row.get("declared_final_artifact")
        )
        matches = sorted(
            path for path in after
            if fnmatch.fnmatchcase(path, final_pattern)
        )
        changed_matches: list[str] = []
        for path in matches:
            if path not in changed:
                continue
            prior_digest = content_baseline.get(path)
            if prior_digest is not None:
                current_digest = _stable_file_sha256(
                    workspace_root / path,
                    expected_identity=after[path],
                )
                if current_digest == prior_digest:
                    continue
            changed_matches.append(path)
        if not matches:
            finding(
                "artifact_final_missing",
                skill_name=skill_name,
                pattern=final_pattern,
            )
            continue
        if not changed_matches:
            finding(
                (
                    "artifact_final_not_committed_after_workflow"
                    if before_content_sha256 is not None
                    else "artifact_final_not_committed_this_turn"
                ),
                skill_name=skill_name,
                pattern=final_pattern,
            )
            continue
        if len(changed_matches) != 1:
            finding(
                "artifact_final_ambiguous",
                skill_name=skill_name,
                pattern=final_pattern,
                actual=len(changed_matches),
            )
            continue
        relative = changed_matches[0]
        identity = after[relative]
        size_bytes = int(identity[3])
        minimum = row.get("expected_min_bytes")
        maximum = row.get("expected_max_bytes")
        if isinstance(minimum, int) and not isinstance(minimum, bool):
            if minimum < 0:
                raise RuntimeError("artifact_contract_invalid")
            if size_bytes < minimum:
                finding(
                    "artifact_min_bytes_not_met",
                    skill_name=skill_name,
                    path=relative,
                    actual=size_bytes,
                    expected=minimum,
                )
        elif minimum is not None:
            raise RuntimeError("artifact_contract_invalid")
        if isinstance(maximum, int) and not isinstance(maximum, bool):
            if maximum < 0:
                raise RuntimeError("artifact_contract_invalid")
            if maximum and size_bytes > maximum:
                finding(
                    "artifact_max_bytes_exceeded",
                    skill_name=skill_name,
                    path=relative,
                    actual=size_bytes,
                    expected=maximum,
                )
        elif maximum is not None:
            raise RuntimeError("artifact_contract_invalid")

        min_lines = row.get("expected_min_lines")
        max_lines = row.get("expected_max_lines")
        min_headings = row.get("declared_section_count")
        line_count: int | None = None
        heading_count: int | None = None
        if min_lines is not None or max_lines is not None or min_headings is not None:
            for value in (min_lines, max_lines, min_headings):
                if (
                    value is not None
                    and (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                    )
                ):
                    raise RuntimeError("artifact_contract_invalid")
            path = workspace_root / relative
            try:
                line_count, heading_count = _artifact_text_stats(path)
            except OSError as exc:
                raise RuntimeError("artifact_contract_audit_failed") from exc
            if isinstance(min_lines, int) and line_count < min_lines:
                finding(
                    "artifact_min_lines_not_met",
                    skill_name=skill_name,
                    path=relative,
                    actual=line_count,
                    expected=min_lines,
                )
            if isinstance(max_lines, int) and max_lines and line_count > max_lines:
                finding(
                    "artifact_max_lines_exceeded",
                    skill_name=skill_name,
                    path=relative,
                    actual=line_count,
                    expected=max_lines,
                )
            if (
                isinstance(min_headings, int)
                and min_headings
                and heading_count < min_headings
            ):
                finding(
                    "artifact_declared_sections_not_met",
                    skill_name=skill_name,
                    path=relative,
                    actual=heading_count,
                    expected=min_headings,
                )

        declared_modules = row.get("declared_modular_files") or []
        if (
            not isinstance(declared_modules, list)
            or len(declared_modules) > MAX_ARTIFACT_CONTRACT_FINDINGS
        ):
            raise RuntimeError("artifact_contract_invalid")
        final_parent = PurePosixPath(relative).parent
        for declared in declared_modules:
            module_pattern = _workspace_contract_pattern(declared)
            resolved_module_pattern = (
                module_pattern
                if str(final_parent) == "."
                else (final_parent / module_pattern).as_posix()
            )
            if not any(
                fnmatch.fnmatchcase(path, resolved_module_pattern)
                for path in after
            ):
                finding(
                    "artifact_declared_module_missing",
                    skill_name=skill_name,
                    pattern=module_pattern,
                    final_parent=(
                        "" if str(final_parent) == "." else str(final_parent)
                    ),
                )
        validated.append({
            "skill_name": skill_name,
            "path": relative,
            "size_bytes": size_bytes,
            "line_count": line_count,
            "heading_count": heading_count,
        })
    return {
        "status": "failed" if findings else "passed",
        "activated_contract_count": len(active),
        "finding_count": len(findings),
        "findings": findings,
        "validated": validated,
    }


def _pending_plan_task_count(tasks_root: Path, native_session_id: str) -> int:
    """Return unfinished Claude task-list items or fail on unsafe state."""

    try:
        uuid_value = uuid.UUID(native_session_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("native_task_session_invalid") from exc
    if str(uuid_value) != native_session_id:
        raise RuntimeError("native_task_session_invalid")
    session_root = tasks_root / native_session_id
    try:
        session_info = os.lstat(session_root)
    except FileNotFoundError:
        return 0
    if not stat.S_ISDIR(session_info.st_mode) or session_root.is_symlink():
        raise RuntimeError("native_task_state_invalid")
    entries = list(session_root.iterdir())
    if len(entries) > 4_096:
        raise RuntimeError("native_task_state_invalid")
    pending = 0
    for path in entries:
        if path.name == ".lock":
            lock_info = os.lstat(path)
            if not stat.S_ISREG(lock_info.st_mode) or path.is_symlink():
                raise RuntimeError("native_task_state_invalid")
            continue
        if re.fullmatch(r"[1-9][0-9]{0,9}\.json", path.name) is None:
            raise RuntimeError("native_task_state_invalid")
        info = os.lstat(path)
        if (
            not stat.S_ISREG(info.st_mode)
            or path.is_symlink()
            or info.st_size > 1024 * 1024
        ):
            raise RuntimeError("native_task_state_invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("native_task_state_invalid")
        status_value = str(payload.get("status") or "")
        if status_value in {"pending", "in_progress"}:
            pending += 1
        elif status_value not in {"completed", "deleted"}:
            raise RuntimeError("native_task_state_invalid")
    return pending


def _quarantine_native_cron_state(claude_root: Path, run_id: str) -> bool:
    """Move unsupported native cron state out of Claude's active load path.

    ChatDS uses disposable ``claude --print`` processes, while the native cron
    scheduler deliberately does not keep print mode alive.  Retaining its
    file in the active location can replay stale prompts on a later unrelated
    Turn, so the controller archives it inside the same Session state before
    Claude starts. No content crosses the Session boundary.
    """

    if SAFE_SESSION_ID.fullmatch(run_id) is None:
        raise RuntimeError("native_cron_state_invalid")
    source = claude_root / "scheduled_tasks.json"
    try:
        info = os.lstat(source)
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(info.st_mode)
        or source.is_symlink()
        or info.st_size > 2 * 1024 * 1024
    ):
        raise RuntimeError("native_cron_state_invalid")
    archive = claude_root / "chatds-native-cron-archive"
    archive.mkdir(mode=0o700, exist_ok=True)
    archive_info = os.lstat(archive)
    if not stat.S_ISDIR(archive_info.st_mode) or archive.is_symlink():
        raise RuntimeError("native_cron_state_invalid")
    destination = archive / f"{run_id}.json"
    try:
        os.lstat(destination)
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError("native_cron_state_invalid")
    source.rename(destination)
    directory_fd = os.open(archive, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True


def _claude_command(
    config: dict[str, Any],
    *,
    workspace_root: Path = Path("/workspace"),
    artifact_stop_contract_path: Path | None = None,
    native_lifecycle_contract_path: Path | None = None,
) -> tuple[list[str], bytes]:
    native_session_id = str(config["native_session_id"])
    api_model = str(config["api_model"])
    context_window_tokens = config.get("context_window_tokens")
    if (
        type(context_window_tokens) is not int
        or context_window_tokens < 200_000
        or context_window_tokens > 4_000_000
    ):
        raise RuntimeError("model_context_window_invalid")
    # Claude Code's public model syntax uses ``[1m]`` as a client-side
    # context capability marker and strips it before every API request.  Its
    # public ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` setting can then cap that
    # tier to the deployment-owned exact capacity.  Thus custom windows above
    # 200K avoid premature compaction without ever overestimating a sub-1M
    # provider, and the upstream model id remains unchanged.
    claude_model = (
        f"{api_model}[1m]" if context_window_tokens > 200_000 else api_model
    )
    command = [
        "/usr/local/bin/claude",
        "--print",
        "--verbose",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--no-chrome",
        "--thinking", "enabled",
        "--setting-sources", "",
        "--plugin-dir", "/skill-view/plugin",
        # The plugin loader can discover Skills and agents outside cwd, but
        # Claude's native filesystem permission layer separately asks before
        # their resources may be read. Register only the immutable Skill
        # resource subtree as an additional native working directory. The
        # container mount remains read-only in every permission tier, while
        # Claude retains its own path/symlink and read-only command checks.
        "--add-dir", "/skill-view/plugin/skills",
        "--mcp-config", "/skill-view/plugin/.mcp.json",
        "--strict-mcp-config",
        "--model", claude_model,
        # Keep Claude Code's own coherent built-in tool surface, including its
        # current Agent/Task aliases and task-management tools. ``--bare`` is
        # intentionally not used: Claude 2.1.152's simple mode reduces
        # ``default`` to Bash/Edit/Read and silently removes native delegation.
        # Empty setting sources, the per-Session HOME, immutable plugin view,
        # strict MCP config, container mounts, and exact egress policy remain
        # the authority boundary without a version-fragile tool-name list.
        "--tools", "default",
    ]
    permission_preset = str(config.get("permission_preset") or "workspace_write")
    if permission_preset == "session_full":
        command.extend([
            "--permission-mode", "bypassPermissions",
            "--dangerously-skip-permissions",
        ])
    elif permission_preset == "workspace_write":
        command.extend(["--permission-mode", "default"])
    elif permission_preset == "read_only":
        command.extend(["--permission-mode", "plan"])
    else:
        raise RuntimeError("permission_preset_invalid")
    # Permission tiers govern action authority, while native user-interaction
    # tools remain browser I/O in every tier.  Claude Code's own structured
    # stdio transport emits the exact can_use_tool request without introducing
    # a second tool loop or reconstructing native tool input.
    command.extend(["--permission-prompt-tool", "stdio"])
    if (
        artifact_stop_contract_path is not None
        and native_lifecycle_contract_path is not None
    ):
        raise RuntimeError("artifact_contract_invalid")
    if native_lifecycle_contract_path is not None:
        if (
            not native_lifecycle_contract_path.is_absolute()
            or native_lifecycle_contract_path.as_posix()
            != "/runtime/native-lifecycle-contract.json"
        ):
            raise RuntimeError("workflow_contract_invalid")
        hook_command = (
            "/usr/local/bin/python -I -m "
            "claude_runner.native_lifecycle_hook "
            "/runtime/native-lifecycle-contract.json"
        )
        command.extend([
            "--settings",
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [{
                            "matcher": (
                                "Agent|Task|Write|Edit|MultiEdit|"
                                "NotebookEdit|Bash"
                            ),
                            "hooks": [{
                                "type": "command",
                                "command": hook_command,
                                "timeout": 120,
                            }],
                        }],
                        "Stop": [{
                            "hooks": [{
                                "type": "command",
                                "command": hook_command,
                                "timeout": 120,
                            }],
                        }],
                    },
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ])
    elif artifact_stop_contract_path is not None:
        if (
            not artifact_stop_contract_path.is_absolute()
            or artifact_stop_contract_path.as_posix()
            != "/runtime/artifact-stop-contract.json"
        ):
            raise RuntimeError("artifact_contract_invalid")
        native_settings = {
            "hooks": {
                "Stop": [{
                    "hooks": [{
                        "type": "command",
                        "command": (
                            "/usr/local/bin/python -I -m "
                            "claude_runner.artifact_stop_hook "
                            "/runtime/artifact-stop-contract.json"
                        ),
                        "timeout": 120,
                    }],
                }],
            },
        }
        command.extend([
            "--settings",
            json.dumps(
                native_settings,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ])
    # Validate the controller receipt at the worker boundary, but do not turn
    # it into a parallel ChatDS system prompt. Claude's own native tool graph,
    # installed Skills and MCP schemas are the model-facing capability plane.
    validate_runtime_capability_contract(
        config.get("runtime_capability_contract")
    )
    disallowed_tools = ["CronCreate", "CronDelete", "CronList"]
    if not bool(config.get("native_web_tools")):
        # WebSearch/WebFetch are provider-hosted server tools, not ordinary
        # local tools. A generic Messages facade can accept their schemas yet
        # return empty pseudo-results or depend on unavailable claude.ai
        # safety services. Local Bash/Skill/MCP/browser capabilities remain.
        disallowed_tools.extend(["WebFetch", "WebSearch"])
    command.extend(["--disallowedTools", ",".join(disallowed_tools)])
    resume_from = str(config.get("resume_from_native_session_id") or "")
    if resume_from:
        command.extend([
            "--resume", resume_from,
            "--fork-session",
            "--session-id", native_session_id,
        ])
    else:
        command.extend(["--session-id", native_session_id])
    return command, _native_stream_json_input(
        config,
        workspace_root=workspace_root,
    )


def _native_stream_json_input(
    config: dict[str, Any],
    *,
    workspace_root: Path,
) -> bytes:
    """Lower receipt-only prompt blocks into one Claude SDK user message."""

    attachments = config.get("input_attachments") or []
    if not isinstance(attachments, list):
        raise RuntimeError("input_attachment_count_invalid")
    if attachments:
        verify_input_attachments(
            attachments=attachments,
            workspace=workspace_root,
        )
    content: list[dict[str, Any]] = []
    prompt = str(config.get("prompt") or "")
    turn_skill_binding = _turn_skill_binding(config)
    if turn_skill_binding is not None:
        skill_name, _source = turn_skill_binding
        native_command = f"/chatds-session-skills:{skill_name}"
        if not (
            prompt == native_command
            or prompt.startswith(native_command + " ")
            or prompt.startswith(native_command + "\n")
        ):
            prompt = f"{native_command} {prompt}".rstrip()
    if prompt:
        content.append({"type": "text", "text": prompt})
    for receipt in attachments:
        payload = (workspace_root / str(receipt["path"])).read_bytes()
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": str(receipt.get("media_type") or ""),
                "data": base64.b64encode(payload).decode("ascii"),
            },
        })
    if not content:
        raise RuntimeError("input_attachment_message_invalid")
    envelope = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": "",
    }
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_NATIVE_INPUT_BYTES:
        raise RuntimeError("native_input_size_limit")
    return encoded


def _turn_skill_binding(
    config: dict[str, Any],
) -> tuple[str, str] | None:
    value = config.get("turn_skill_binding")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"skill_name", "source"}
    ):
        raise RuntimeError("turn_skill_binding_invalid")
    skill_name = value.get("skill_name")
    source = value.get("source")
    if (
        not isinstance(skill_name, str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", skill_name) is None
        or source != "fresh_session_primary"
        or config.get("resume_from_native_session_id") is not None
    ):
        raise RuntimeError("turn_skill_binding_invalid")
    return skill_name, source


def _worker_environment(
    config: dict[str, Any],
    *,
    trust: dict[str, str],
    proxy_url: str,
    worker_tmp: Path,
) -> dict[str, str]:
    keep = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/state/home",
        "XDG_RUNTIME_DIR": str(worker_tmp),
        "XDG_CACHE_HOME": "/state/home/.cache",
        "XDG_CONFIG_HOME": "/state/home/.config",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(worker_tmp),
        "MPLCONFIGDIR": "/state/home/.config/matplotlib",
        "NODE_PATH": "/opt/chatds-browser-runtime/node_modules",
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/chatds-browser-runtime/ms-playwright",
        "BROWSER_EXECUTABLE": "/usr/local/bin/chatds-chromium-proxy",
        "CHROME_BIN": "/usr/local/bin/chatds-chromium-proxy",
        "SE_OFFLINE": "true",
        "SE_AVOID_STATS": "true",
        "SE_AVOID_BROWSER_DOWNLOAD": "true",
        "ANTHROPIC_API_KEY": os.environ["CLAUDE_PROVIDER_API_KEY"],
        "ANTHROPIC_BASE_URL": str(config["provider_claude_base_url"]),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(config["max_output_tokens"]),
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(
            config["context_window_tokens"]
        ),
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "all_proxy": proxy_url,
        "NO_PROXY": "localhost,127.0.0.1,[::1]",
        "no_proxy": "localhost,127.0.0.1,[::1]",
        "NODE_USE_ENV_PROXY": "1",
        "SKILL_EGRESS_PROXY_URL": proxy_url,
        **trust,
    }
    return keep


@contextmanager
def _session_workspace_lock(config: dict[str, Any]) -> Iterator[None]:
    """Hold the same local-volume lock used by Backend/Harness.

    The Turn container owns this lease, rather than the HTTP Supervisor, so a
    Supervisor restart cannot silently release workspace exclusion while the
    Claude process is still writing.  Only the trusted root controller can
    traverse the mounted lock plane; the worker drops all groups before it
    receives model-controlled input.
    """

    user_id = str(config.get("user_id") or "")
    conversation_id = str(config.get("conversation_id") or "")
    if not SAFE_SESSION_ID.fullmatch(user_id) or not SAFE_SESSION_ID.fullmatch(
        conversation_id
    ):
        raise RuntimeError("workspace_lock_identity_invalid")
    user_id = unicodedata.normalize("NFC", user_id)
    conversation_id = unicodedata.normalize("NFC", conversation_id)
    digest = hashlib.sha256(WORKSPACE_LOCK_IDENTITY_DOMAIN)
    for value in (user_id, conversation_id):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    lock_name = f"v1-{digest.hexdigest()}.lock"
    lock_root = Path("/run/chatds-workspace-lock-plane/locks")
    try:
        parent_info = os.lstat(lock_root)
    except OSError as exc:
        raise RuntimeError("workspace_lock_plane_unavailable") from exc
    if lock_root.is_symlink() or not stat.S_ISDIR(parent_info.st_mode):
        raise RuntimeError("workspace_lock_plane_unsafe")
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(lock_root, parent_flags)
    descriptor: int | None = None
    locked = False
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_name, flags, 0o600, dir_fd=parent_fd)
        info = os.fstat(descriptor)
        current = os.stat(lock_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise RuntimeError("workspace_lock_object_unsafe")
        deadline = time.monotonic() + 60.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError("workspace_lock_timeout") from exc
                time.sleep(0.02)
        yield
    finally:
        if descriptor is not None:
            if locked:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)
        os.close(parent_fd)


def _start_worker_compositor(
    environment: dict[str, str],
    *,
    worker_uid: int,
    worker_gid: int,
) -> tuple[subprocess.Popen[bytes], dict[str, str], tuple[Path, ...]]:
    temporary = Path(environment["TMPDIR"])
    socket_name = f"wayland-chatds-{os.getpid()}"
    socket_path = temporary / socket_name
    lock_path = temporary / f"{socket_name}.lock"
    log_path = temporary / f"weston-chatds-{os.getpid()}.log"
    process = subprocess.Popen(
        [
            "/usr/bin/weston",
            "--backend=headless-backend.so",
            f"--socket={socket_name}",
            "--idle-time=0",
            "--no-config",
            f"--log={log_path}",
            "--width=1440",
            "--height=1000",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        close_fds=True,
        preexec_fn=lambda: _drop_worker(worker_uid, worker_gid),
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("wayland_compositor_start_failed")
        try:
            info = os.lstat(socket_path)
        except FileNotFoundError:
            time.sleep(0.02)
            continue
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != worker_uid
            or info.st_gid != worker_gid
        ):
            _stop_process_group(process)
            raise RuntimeError("wayland_compositor_socket_unsafe")
        child_environment = dict(environment)
        child_environment.pop("DISPLAY", None)
        child_environment.pop("XAUTHORITY", None)
        child_environment["WAYLAND_DISPLAY"] = socket_name
        child_environment["XDG_SESSION_TYPE"] = "wayland"
        return process, child_environment, (socket_path, lock_path, log_path)
    _stop_process_group(process)
    raise RuntimeError("wayland_compositor_start_timeout")


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            process.wait()
            return
        time.sleep(0.02)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _drop_worker(uid: int, gid: int) -> None:
    os.setsid()
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.umask(0o077)


def _close_stdin_after_native_result(
    process: subprocess.Popen[bytes],
    input_lock: threading.Lock,
    ledger: EventLedger,
    *,
    keep_stdin_open: bool,
) -> bool:
    """End one stream-json input after Claude durably publishes its result.

    Interactive permission mode must keep stdin available for native
    ``control_response`` messages during the Turn.  The CLI also treats an
    open stream-json stdin as a signal that another user message may follow,
    so leaving it open after the native result prevents ``--print`` from
    exiting.  The ledger observation is authoritative and stderr cannot set
    it.
    """

    if not keep_stdin_open or not ledger.saw_native_result:
        return False
    try:
        with input_lock:
            stream = process.stdin
            if stream is None or stream.closed:
                return False
            stream.close()
        return True
    except (BrokenPipeError, OSError, ValueError):
        return False


def _run_child(
    command: list[str],
    prompt: bytes,
    environment: dict[str, str],
    *,
    worker_uid: int,
    worker_gid: int,
    ledger: EventLedger,
    run_id: str,
    permission_preset: str,
) -> int:
    global _child
    _child = subprocess.Popen(
        command,
        cwd="/workspace",
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=False,
        preexec_fn=lambda: _drop_worker(worker_uid, worker_gid),
    )
    assert _child.stdin is not None and _child.stdout is not None and _child.stderr is not None
    input_lock = threading.Lock()
    # Native user-interaction tools can surface in every permission tier. Keep
    # structured stdin available until Claude publishes its native result;
    # `_close_stdin_after_native_result` then closes it deterministically.
    keep_stdin_open = True

    def write_input(value: bytes) -> bool:
        try:
            with input_lock:
                if _child is None or _child.stdin is None or _child.poll() is not None:
                    return False
                _child.stdin.write(value)
                _child.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def feed_prompt() -> None:
        try:
            write_input(prompt)
        except (BrokenPipeError, OSError):
            pass
        finally:
            if not keep_stdin_open:
                try:
                    assert _child is not None and _child.stdin is not None
                    _child.stdin.close()
                except (OSError, ValueError):
                    pass

    feeder = threading.Thread(target=feed_prompt, daemon=True, name="claude-prompt-writer")
    feeder.start()
    selector = selectors.DefaultSelector()
    selector.register(_child.stdout, selectors.EVENT_READ, "stdout")
    selector.register(_child.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    pending_approvals: dict[str, tuple[dict[str, Any], int]] = {}
    resolved_approvals: set[str] = set()
    approval_root = Path("/state/control/runs") / run_id / "approvals"

    def observe_control_request(
        line: bytes,
        channel: str,
        native_seq: int,
    ) -> None:
        if channel != "stdout":
            return
        try:
            native = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        inner = native.get("request") if isinstance(native, dict) else None
        request_id = str(native.get("request_id") or "") if isinstance(native, dict) else ""
        if (
            not isinstance(native, dict)
            or native.get("type") != "control_request"
            or not isinstance(inner, dict)
            or inner.get("subtype") != "can_use_tool"
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id) is None
        ):
            return
        pending_approvals.setdefault(request_id, (native, native_seq))

    def send_decision(
        request_id: str,
        native: dict[str, Any],
        decision: str,
        *,
        updated_input: dict[str, Any] | None = None,
    ) -> bool:
        inner = native["request"]
        response_payload = {
            "behavior": decision,
            **(
                {
                    "updatedInput": (
                        updated_input
                        if updated_input is not None
                        else dict(inner.get("input") or {})
                    )
                }
                if decision == "allow"
                else {"message": "Denied by ChatDS Session permission policy or user"}
            ),
        }
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response_payload,
            },
        }
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        delivered = write_input(encoded)
        if delivered:
            # This is a delivery receipt, not an optimistic UI acknowledgement:
            # only publish it after Claude's open stdin accepted the exact
            # native control response.
            ledger.append_event({
                "type": "chatds.approval.decided",
                "request_id": request_id,
                "decision": decision,
                "tool_name": str(inner.get("tool_name") or "tool")[:512],
                "interaction_kind": (
                    native_user_interaction_kind(
                        inner.get("tool_name"), inner.get("input")
                    )
                    or "approval"
                ),
            }, channel="controller")
        return delivered

    def drain_approval_mailbox() -> None:
        for request_id, pending in list(pending_approvals.items()):
            native, native_seq = pending
            if request_id in resolved_approvals:
                continue
            inner = native["request"]
            tool_name = str(inner.get("tool_name") or "")
            interaction_kind = native_user_interaction_kind(
                tool_name, inner.get("input")
            )
            if is_native_user_interaction(tool_name) and interaction_kind is None:
                # A malformed intrinsic interaction must not hang a confined
                # Turn or be auto-allowed by the full-access tier.
                if send_decision(request_id, native, "deny"):
                    resolved_approvals.add(request_id)
                    pending_approvals.pop(request_id, None)
                continue
            user_interaction = interaction_kind is not None
            if permission_preset == "read_only" and not user_interaction:
                if send_decision(request_id, native, "deny"):
                    resolved_approvals.add(request_id)
                    pending_approvals.pop(request_id, None)
                continue
            if permission_preset == "session_full" and not user_interaction:
                if send_decision(request_id, native, "allow"):
                    resolved_approvals.add(request_id)
                    pending_approvals.pop(request_id, None)
                continue
            response_path = approval_root / (
                hashlib.sha256(request_id.encode()).hexdigest() + ".json"
            )
            if not response_path.exists():
                continue
            try:
                value = json.loads(response_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            decision = str(value.get("decision") or "") if isinstance(value, dict) else ""
            if (
                not isinstance(value, dict)
                or value.get("schema") != "chatds.claude-approval.v1"
                or value.get("run_id") != run_id
                or value.get("request_id") != request_id
                or value.get("request_seq") != native_seq
                or decision not in {"allow", "deny"}
            ):
                continue
            updated_input = value.get("updated_input")
            if updated_input is not None and not isinstance(updated_input, dict):
                continue
            if send_decision(
                request_id,
                native,
                decision,
                updated_input=updated_input,
            ):
                resolved_approvals.add(request_id)
                pending_approvals.pop(request_id, None)
    leader_exited_at: float | None = None
    while selector.get_map():
        for key, _mask in selector.select(timeout=0.5):
            chunk = os.read(key.fileobj.fileno(), 64 * 1024)
            channel = str(key.data)
            if not chunk:
                residual = bytes(buffers[channel])
                if residual:
                    ledger.append_line(residual, channel=channel)
                selector.unregister(key.fileobj)
                continue
            buffers[channel].extend(chunk)
            while b"\n" in buffers[channel]:
                line, _, rest = buffers[channel].partition(b"\n")
                buffers[channel] = bytearray(rest)
                native_seq = ledger.append_line(line, channel=channel)
                observe_control_request(line, channel, native_seq)
                _close_stdin_after_native_result(
                    _child,
                    input_lock,
                    ledger,
                    keep_stdin_open=keep_stdin_open,
                )
            if len(buffers[channel]) > MAX_NATIVE_LINE_BYTES:
                ledger.append_line(bytes(buffers[channel]), channel=channel)
                buffers[channel].clear()
        drain_approval_mailbox()
        if _child.poll() is not None:
            if leader_exited_at is None:
                leader_exited_at = time.monotonic()
            elif time.monotonic() - leader_exited_at >= 2.0:
                # A background Bash/browser descendant must not keep inherited
                # pipes and the workspace lease alive after Claude's own
                # terminal.  The entire per-Turn process group is disposable.
                _stop_process_group(_child)
        else:
            leader_exited_at = None
    exit_code = int(_child.wait())
    _stop_process_group(_child)
    if keep_stdin_open:
        try:
            with input_lock:
                if _child.stdin is not None and not _child.stdin.closed:
                    _child.stdin.close()
        except (OSError, ValueError):
            pass
    # PID 1 has now synchronously reaped every process in the disposable Turn
    # group.  Native local-bash notifications can legitimately be absent when
    # TaskOutput was consumed directly or the parent returned first; convert
    # only those OS-owned tasks to killed receipts.  Delegated agent tasks are
    # deliberately left pending and continue to gate a successful terminal.
    ledger.reconcile_worker_process_exit()
    feeder.join(timeout=5.0)
    return exit_code


def _install_signal_handlers() -> None:
    def stop(signum, _frame) -> None:
        global _termination_reason
        if _termination_reason is None:
            _termination_reason = (
                "hard_timeout" if signum == signal.SIGUSR1 else "cancelled"
            )
        child = _child
        if child is None or child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGUSR1, stop)


def _fatal(code: str) -> int:
    if code not in SAFE_RUNNER_FATAL_CODES:
        code = "runner_bootstrap_failed"
    print(json.dumps({"type": "chatds.runner.fatal", "code": code}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
