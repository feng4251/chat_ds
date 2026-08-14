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
from pathlib import Path
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
from claude_runner.runtime_capabilities import render_runtime_capability_prompt
from claude_runner.input_attachments import verify_input_attachments
from claude_runner.mcp_schedule_control import (
    normalize_schedule_create,
    normalize_schedule_tool_aliases,
)


MAX_NATIVE_LINE_BYTES = 64 * 1024 * 1024
MAX_NATIVE_INPUT_BYTES = 96 * 1024 * 1024
SYNC_EVERY_EVENTS = 20
MAX_WORKSPACE_SNAPSHOT_FILES = 65_536
MAX_WORKSPACE_ARTIFACTS = 8_192
MAX_WORKSPACE_ARTIFACT_FILE_BYTES = 1024 * 1024 * 1024
MAX_WORKSPACE_ARTIFACT_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_WORKSPACE_RELATIVE_PATH_BYTES = 1024
MAX_ARTIFACT_CONTRACTS = 64
MAX_ARTIFACT_CONTRACT_FINDINGS = 128
WORKSPACE_LOCK_IDENTITY_DOMAIN = b"chatds-workspace-mutation-lock-v1\0"
SAFE_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
SAFE_NATIVE_TASK_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
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
        ledger.bind_schedule_tool_aliases(
            config.get("schedule_tool_aliases")
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
                command, prompt = _claude_command(config)
                _install_signal_handlers()
                exit_code = _run_child(
                    command,
                    prompt,
                    environment,
                    worker_uid=worker_uid,
                    worker_gid=worker_gid,
                    ledger=ledger,
                    run_id=str(config["run_id"]),
                    permission_preset=str(config.get("permission_preset") or "session_full"),
                )
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
            controller_stage = "artifact_contract_audit"
            artifact_contract_receipt = _validate_artifact_contracts(
                contracts=config.get("artifact_contracts"),
                invoked_skill_names=ledger.invoked_skill_names,
                before=workspace_before,
                after=workspace_after,
                workspace_root=Path("/workspace"),
            )
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
            and artifact_contract_receipt.get("status") != "failed"
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
        self._task_output_calls: dict[str, str] = {}
        self._schedule_control_calls: dict[str, dict[str, Any]] = {}
        self._schedule_tool_aliases: dict[str, str | None] | None = None
        self._pending_control_writes: list[dict[str, Any]] = []
        self._settled_control_tool_calls: set[str] = set()
        self._invoked_skill_names: set[str] = set()
        self._native_task_reconciliations = {
            "native_notification": 0,
            "task_output": 0,
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
                    existing = self._native_tasks.get(task_id)
                    if existing is None:
                        self._native_tasks[task_id] = {
                            "task_id": task_id,
                            "task_type": task_type,
                            "status": "running",
                            "terminal_source": None,
                        }
                    elif existing.get("status") != "running":
                        existing["status"] = "running"
                        existing["terminal_source"] = None
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

    def bind_schedule_tool_aliases(self, value: object) -> None:
        """Bind the immutable controller vocabulary before native output.

        ``None`` preserves rolling compatibility for views compiled before
        the vocabulary became part of the Skill-view identity. New views
        always provide an exact map shared with their schedule MCP process.
        """

        self._schedule_tool_aliases = (
            None if value is None else normalize_schedule_tool_aliases(value)
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
        if row is None:
            row = {
                "task_id": task_id,
                "task_type": "unknown",
                "status": status,
                "terminal_source": source,
            }
            self._native_tasks[task_id] = row
        elif row.get("status") == "running":
            row["status"] = status
            row["terminal_source"] = source
        else:
            return
        if source in self._native_task_reconciliations:
            self._native_task_reconciliations[source] += 1

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
                        tool_aliases=self._schedule_tool_aliases,
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


def _native_checkpoint_exists(projects_root: Path, native_session_id: str) -> bool:
    """Require one regular transcript for the transaction-local Session ID."""

    try:
        uuid_value = uuid.UUID(native_session_id)
    except (ValueError, TypeError, AttributeError):
        return False
    if str(uuid_value) != native_session_id:
        return False
    if not projects_root.is_dir() or projects_root.is_symlink():
        return False
    matches = list(projects_root.glob(f"*/{native_session_id}.jsonl"))
    if len(matches) != 1:
        return False
    path = matches[0]
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_size > 0


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
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        current = os.lstat(path)
        current_identity = (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )
        if not stat.S_ISREG(current.st_mode) or current_identity != after[relative]:
            raise RuntimeError("workspace_artifact_changed_during_audit")
        sha256 = digest.hexdigest()
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
    after: dict[str, tuple[int, ...]],
    workspace_root: Path,
) -> dict[str, Any]:
    """Validate only contracts for Skills actually invoked this Turn.

    Installed Skills remain ambient capabilities and must not force unrelated
    turns to create artifacts.  A native ``Skill`` tool receipt activates the
    corresponding immutable contract.  Final deliverables must be created or
    mutated in this Turn; declared supporting modules may come from an earlier
    failed Turn in the same Session so continuation recovery remains possible.
    """

    if contracts is None:
        rows: list[object] = []
    elif isinstance(contracts, list) and len(contracts) <= MAX_ARTIFACT_CONTRACTS:
        rows = contracts
    else:
        raise RuntimeError("artifact_contract_invalid")
    active: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("artifact_contract_invalid")
        skill_name = str(row.get("skill_name") or "")
        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", skill_name) is None:
            raise RuntimeError("artifact_contract_invalid")
        if skill_name in invoked_skill_names:
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
        changed_matches = [path for path in matches if path in changed]
        if not matches:
            finding(
                "artifact_final_missing",
                skill_name=skill_name,
                pattern=final_pattern,
            )
            continue
        if not changed_matches:
            finding(
                "artifact_final_not_committed_this_turn",
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
        for declared in declared_modules:
            module_pattern = _workspace_contract_pattern(declared)
            if not any(
                fnmatch.fnmatchcase(path, module_pattern) for path in after
            ):
                finding(
                    "artifact_declared_module_missing",
                    skill_name=skill_name,
                    pattern=module_pattern,
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
    permission_preset = str(config.get("permission_preset") or "session_full")
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
    command.extend([
        "--append-system-prompt",
        render_runtime_capability_prompt(
            config.get("runtime_capability_contract")
        ),
    ])
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
    interactive = permission_preset == "workspace_write"
    keep_stdin_open = permission_preset in {"read_only", "workspace_write"}

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

    def send_decision(request_id: str, native: dict[str, Any], decision: str) -> bool:
        inner = native["request"]
        response_payload = {
            "behavior": decision,
            **(
                {"updatedInput": dict(inner.get("input") or {})}
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
            }, channel="controller")
        return delivered

    def drain_approval_mailbox() -> None:
        for request_id, pending in list(pending_approvals.items()):
            native, native_seq = pending
            if request_id in resolved_approvals:
                continue
            if permission_preset == "read_only":
                if send_decision(request_id, native, "deny"):
                    resolved_approvals.add(request_id)
                    pending_approvals.pop(request_id, None)
                continue
            if not interactive:
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
            if send_decision(request_id, native, decision):
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
