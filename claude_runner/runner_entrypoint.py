"""PID 1 controller inside one networkless, per-Turn Claude container."""

from __future__ import annotations

import hashlib
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


MAX_NATIVE_LINE_BYTES = 64 * 1024 * 1024
SYNC_EVERY_EVENTS = 20
MAX_WORKSPACE_SNAPSHOT_FILES = 65_536
MAX_WORKSPACE_ARTIFACTS = 8_192
MAX_WORKSPACE_ARTIFACT_FILE_BYTES = 1024 * 1024 * 1024
MAX_WORKSPACE_ARTIFACT_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_WORKSPACE_RELATIVE_PATH_BYTES = 1024
WORKSPACE_LOCK_IDENTITY_DOMAIN = b"chatds-workspace-mutation-lock-v1\0"
SAFE_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
_child: subprocess.Popen[bytes] | None = None
_termination_reason: str | None = None


def main() -> int:
    if os.geteuid() != 0:
        return _fatal("runner_controller_not_root")
    try:
        config = _load_config(Path(os.environ["CHATDS_RUN_CONFIG"]))
        ledger = EventLedger(Path(os.environ["CHATDS_EVENT_LEDGER"]))
        worker_uid = int(os.environ.get("CLAUDE_RUNNER_WORKER_UID", "65529"))
        worker_gid = int(os.environ.get("CLAUDE_RUNNER_WORKER_GID", "65529"))
        runtime_root = Path("/runtime/worker")
        runtime_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chown(runtime_root, worker_uid, worker_gid)
        worker_tmp = runtime_root / "tmp"
        worker_tmp.mkdir(mode=0o700)
        os.chown(worker_tmp, worker_uid, worker_gid)
        with _session_workspace_lock(config):
            workspace_before = _workspace_snapshot(Path("/workspace"))
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
                command, prompt = _claude_command(config)
                _install_signal_handlers()
                exit_code = _run_child(
                    command,
                    prompt,
                    environment,
                    worker_uid=worker_uid,
                    worker_gid=worker_gid,
                    ledger=ledger,
                )
            finally:
                _stop_process_group(compositor)
                for path in compositor_paths:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            receipt = bridge.shutdown_and_seal()
            bridge_thread.join(timeout=5.0)
            if bridge_thread.is_alive():
                raise RuntimeError("egress_bridge_did_not_stop")
            _emit_workspace_artifacts(
                ledger=ledger,
                run_id=str(config["run_id"]),
                before=workspace_before,
                after=_workspace_snapshot(Path("/workspace")),
                workspace_root=Path("/workspace"),
            )
        checkpoint_ready = _native_checkpoint_exists(
            Path("/state/home/.claude/projects"),
            str(config["native_session_id"]),
        )
        pending_plan_task_count = _pending_plan_task_count(
            Path("/state/home/.claude/tasks"),
            str(config["native_session_id"]),
        )
        pending_native_task_count = ledger.active_native_task_count
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
                and pending_plan_task_count == 0
                and pending_native_task_count == 0
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
            "error": terminal_error,
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
            return _fatal(type(exc).__name__)
        terminal_event = {
            "type": "chatds.supervisor.terminal",
            "status": "failed",
            "error": type(exc).__name__,
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

    if not isinstance(exc, BridgeConfigurationError):
        return None
    message = str(exc)
    if re.fullmatch(r"[a-z][a-z0-9 -]{0,127}", message) is None:
        return None
    return "egress_" + re.sub(r"[ -]+", "_", message)


def _terminal_error(
    *,
    termination_reason: str | None,
    exit_code: int,
    ledger: "EventLedger",
    checkpoint_ready: bool,
    egress_receipt: dict[str, Any],
    pending_plan_task_count: int,
    pending_native_task_count: int,
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
    if pending_plan_task_count:
        return "native_plan_tasks_pending"
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
        self._active_native_tasks: set[str] = set()
        if path.exists():
            raise RuntimeError("event_ledger_already_exists")
        self._stream = path.open("xb", buffering=0)
        os.chmod(path, 0o600, follow_symlinks=False)

    def append_line(self, line: bytes, *, channel: str) -> None:
        if len(line) > MAX_NATIVE_LINE_BYTES:
            self.append_event({
                "type": "chatds.runner.diagnostic",
                "code": "native_line_too_large",
                "size_bytes": len(line),
            }, channel="controller")
            return
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        try:
            native = json.loads(text)
        except json.JSONDecodeError:
            self._append({"channel": channel, "text": text})
            return
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
                self._active_native_tasks.add(task_id)
            elif subtype in {
                "task_notification",
                "task_completed",
                "task_failed",
            } and task_id:
                self._active_native_tasks.discard(task_id)
        self._append({"channel": channel, "event": native})

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
        return len(self._active_native_tasks)

    def append_event(
        self,
        event: dict[str, Any],
        *,
        channel: str,
        terminal: bool = False,
    ) -> None:
        self._append({"channel": channel, "event": event}, terminal=terminal)

    def close(self) -> None:
        if not self._stream.closed:
            os.fsync(self._stream.fileno())
            self._stream.close()

    def _append(self, value: dict[str, Any], *, terminal: bool = False) -> None:
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


def _claude_command(config: dict[str, Any]) -> tuple[list[str], bytes]:
    native_session_id = str(config["native_session_id"])
    command = [
        "/usr/local/bin/claude",
        "--print",
        "--verbose",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--no-chrome",
        "--thinking", "enabled",
        "--permission-mode", "bypassPermissions",
        "--dangerously-skip-permissions",
        "--setting-sources", "",
        "--plugin-dir", "/skill-view/plugin",
        "--mcp-config", "/skill-view/plugin/.mcp.json",
        "--strict-mcp-config",
        "--model", str(config["api_model"]),
        # Keep Claude Code's own coherent built-in tool surface, including its
        # current Agent/Task aliases and task-management tools. ``--bare`` is
        # intentionally not used: Claude 2.1.152's simple mode reduces
        # ``default`` to Bash/Edit/Read and silently removes native delegation.
        # Empty setting sources, the per-Session HOME, immutable plugin view,
        # strict MCP config, container mounts, and exact egress policy remain
        # the authority boundary without a version-fragile tool-name list.
        "--tools", "default",
    ]
    if not bool(config.get("native_web_tools")):
        # WebSearch/WebFetch are provider-hosted server tools, not ordinary
        # local tools. A generic Messages facade can accept their schemas yet
        # return empty pseudo-results or depend on unavailable claude.ai
        # safety services. Local Bash/Skill/MCP/browser capabilities remain.
        command.extend([
            "--disallowedTools", "WebFetch,WebSearch",
        ])
    resume_from = str(config.get("resume_from_native_session_id") or "")
    if resume_from:
        command.extend([
            "--resume", resume_from,
            "--fork-session",
            "--session-id", native_session_id,
        ])
    else:
        command.extend(["--session-id", native_session_id])
    return command, str(config["prompt"]).encode("utf-8")


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


def _run_child(
    command: list[str],
    prompt: bytes,
    environment: dict[str, str],
    *,
    worker_uid: int,
    worker_gid: int,
    ledger: EventLedger,
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
    def feed_prompt() -> None:
        try:
            assert _child is not None and _child.stdin is not None
            _child.stdin.write(prompt)
        except (BrokenPipeError, OSError):
            pass
        finally:
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
                ledger.append_line(line, channel=channel)
            if len(buffers[channel]) > MAX_NATIVE_LINE_BYTES:
                ledger.append_line(bytes(buffers[channel]), channel=channel)
                buffers[channel].clear()
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
    print(json.dumps({"type": "chatds.runner.fatal", "code": code}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
