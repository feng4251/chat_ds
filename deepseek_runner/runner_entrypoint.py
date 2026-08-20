"""PID 1 controller for one isolated upstream DeepSeek Harness Turn."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from deepseek_runner.native_workflow import (
    build_deepseek_workflow_receipt,
    validate_deepseek_workflow_projection,
)
from deepseek_runner.native_artifacts import (
    MAX_ARTIFACT_PROJECTION_BYTES,
    active_artifact_skill_names,
    compile_deepseek_artifact_projection,
    validate_deepseek_artifact_projection,
)
from native_security.artifact_contract import (
    validate_artifact_contracts,
    workspace_snapshot,
)

from chatds_browser_runtime.proxy_bridge import (
    EXPECTED_BRIDGE_GID,
    EXPECTED_PROXY_UID,
    PROXY_CA_CERTIFICATE_PATH,
    PROXY_LEAF_SPKI_PATH,
    PROXY_SOCKET_PATH,
    LoopbackProxyBridge,
    ProxySocketAuthority,
    ProxyTrustAuthority,
)
MAX_FILES = 200_000
MAX_CHANGED_FILES = 8_192
MAX_NATIVE_EVENT_LINE_BYTES = 16 * 1024 * 1024
MAX_NATIVE_TURN_INPUT_BYTES = 64 * 1024 * 1024
MAX_WORKFLOW_PROJECTION_BYTES = 40 * 1024 * 1024
MAX_CONTROL_DECISION_LINE_BYTES = 64 * 1024
MAX_MCP_SERVERS = 128
WORKSPACE_LOCK_IDENTITY_DOMAIN = b"chatds-workspace-mutation-lock-v1\0"
SAFE_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
SAFE_NATIVE_SESSION_ID = re.compile(r"^chatds-[0-9a-f]{32}$")
SAFE_MCP_SERVER_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_child: subprocess.Popen[bytes] | None = None
_stop_reason: str | None = None


@contextmanager
def _session_workspace_lock(config: dict[str, Any]):
    user_id = unicodedata.normalize("NFC", str(config.get("user_id") or ""))
    conversation_id = unicodedata.normalize(
        "NFC", str(config.get("conversation_id") or "")
    )
    if not SAFE_SESSION_ID.fullmatch(user_id) or not SAFE_SESSION_ID.fullmatch(
        conversation_id
    ):
        raise RuntimeError("workspace_lock_identity_invalid")
    digest = hashlib.sha256(WORKSPACE_LOCK_IDENTITY_DOMAIN)
    for value in (user_id, conversation_id):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    root = Path("/run/chatds-workspace-lock-plane/locks")
    info = os.lstat(root)
    if root.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("workspace_lock_plane_unsafe")
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent = os.open(root, parent_flags)
    descriptor = None
    locked = False
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            f"v1-{digest.hexdigest()}.lock", flags, 0o600, dir_fd=parent
        )
        object_info = os.fstat(descriptor)
        if not stat.S_ISREG(object_info.st_mode) or stat.S_IMODE(object_info.st_mode) != 0o600:
            raise RuntimeError("workspace_lock_object_unsafe")
        deadline = time.monotonic() + 60
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
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        os.close(parent)




class ControlDecisionForwarder:
    """Forward supervisor control decisions into worker-readable tmpfs.

    The supervisor receives approval decisions via HTTP and writes them to a
    controller-owned mailbox. This forwarder copies complete JSONL rows into
    the worker-readable tmpfs path where the DSH control bridge polls them.
    """

    def __init__(self, mailbox: Path, worker_path: Path) -> None:
        self.mailbox = mailbox
        self.worker_path = worker_path
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="control-decision-forwarder",
        )
        self._error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("control_decision_forwarder_did_not_stop")
        if self._error is not None:
            raise RuntimeError(self._error)

    def _run(self) -> None:
        position = 0
        pending = bytearray()
        self.worker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        try:
            while True:
                try:
                    with self.mailbox.open("rb") as stream:
                        stream.seek(position)
                        block = stream.read(16384)
                except FileNotFoundError:
                    block = b""
                if block:
                    position += len(block)
                    pending.extend(block)
                    while True:
                        boundary = pending.find(b"\n")
                        if boundary < 0:
                            break
                        line = bytes(pending[:boundary])
                        del pending[:boundary + 1]
                        if len(line) > 0:
                            if len(line) > MAX_CONTROL_DECISION_LINE_BYTES:
                                raise RuntimeError("control_decision_line_too_large")
                            try:
                                row = json.loads(line)
                            except (UnicodeError, ValueError, TypeError) as exc:
                                raise RuntimeError("control_decision_json_invalid") from exc
                            if (
                                not isinstance(row, dict)
                                or re.fullmatch(
                                    r"[A-Za-z0-9_.:-]{1,128}",
                                    str(row.get("request_id") or ""),
                                ) is None
                                or type(row.get("request_seq")) is not int
                                or row["request_seq"] < 1
                                or row.get("decision") not in {"allow", "deny"}
                                or bool(set(row) - {
                                    "request_id", "request_seq", "decision",
                                    "selected", "custom",
                                })
                                or row.get("selected") is not None
                                and (
                                    not isinstance(row.get("selected"), list)
                                    or len(row["selected"]) > 4
                                    or any(
                                        not isinstance(value, str)
                                        or len(value) > 4_000
                                        for value in row["selected"]
                                    )
                                )
                                or row.get("custom") is not None
                                and (
                                    not isinstance(row.get("custom"), str)
                                    or len(row["custom"]) > 4_000
                                )
                            ):
                                raise RuntimeError("control_decision_schema_invalid")
                            encoded = json.dumps(
                                row,
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                            ).encode() + b"\n"
                            with self.worker_path.open("ab") as out:
                                out.write(encoded)
                                out.flush()
                                os.fsync(out.fileno())
                    continue
                if len(pending) > MAX_CONTROL_DECISION_LINE_BYTES:
                    raise RuntimeError("control_decision_line_too_large")
                if self._stop.is_set():
                    if pending:
                        raise RuntimeError("control_decision_line_incomplete")
                    break
                time.sleep(0.05)
        except BaseException as exc:
            self._error = (
                str(exc) if isinstance(exc, RuntimeError) and str(exc)
                else "control_decision_forwarder_failed"
            )[:128]

class Ledger:
    def __init__(self, path: Path) -> None:
        if path.as_posix() != "/run/chatds-control/events.jsonl":
            raise RuntimeError("deepseek_event_ledger_path_invalid")
        self.path = path
        self._lock = threading.RLock()
        self._seq = 0
        if self.path.exists():
            lines = self.path.read_bytes().splitlines()
            if lines:
                self._seq = int(json.loads(lines[-1]).get("seq") or 0)

    def append(self, event: dict[str, Any], *, channel: str = "controller") -> None:
        with self._lock:
            self._seq += 1
            envelope = {
                "seq": self._seq,
                "received_at_unix_ms": int(time.time() * 1000),
                "channel": channel,
                "event": event,
            }
            payload = json.dumps(
                envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode() + b"\n"
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
                0o600,
            )
            try:
                view = memoryview(payload)
                offset = 0
                while offset < len(view):
                    offset += os.write(fd, view[offset:])
                os.fsync(fd)
            finally:
                os.close(fd)


class NativeEventReceiver:
    """Receive events only from the exact native Harness process.

    The endpoint lives below the root-owned controller directory. Linux peer
    credentials bind every accepted stream to the PID launched by this PID 1,
    so a same-UID model tool subprocess cannot forge workflow or terminal
    evidence merely by discovering the endpoint path.
    """

    def __init__(self, endpoint: Path, ledger: Ledger, *, worker_gid: int) -> None:
        self.endpoint = endpoint
        self.ledger = ledger
        self.worker_gid = worker_gid
        self._stop = threading.Event()
        self._authorized = threading.Event()
        self._authorized_pid: int | None = None
        self._listener: socket.socket | None = None
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="deepseek-native-event-receiver",
        )
        self._error: str | None = None

    def start(self) -> None:
        if self.endpoint.exists() or self.endpoint.is_symlink():
            raise RuntimeError("native_event_socket_exists")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.endpoint))
            os.chown(self.endpoint, os.geteuid(), self.worker_gid)
            os.chmod(self.endpoint, 0o620)
            listener.listen(1)
            listener.settimeout(0.1)
        except BaseException:
            listener.close()
            raise
        self._listener = listener
        self._thread.start()

    def authorize_pid(self, pid: int) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise RuntimeError("native_event_peer_invalid")
        if self._authorized_pid is not None:
            raise RuntimeError("native_event_peer_already_authorized")
        self._authorized_pid = pid
        self._authorized.set()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        listener = self._listener
        if listener is not None:
            listener.close()
            self._listener = None
        try:
            self.endpoint.unlink(missing_ok=True)
        except OSError:
            pass
        if self._thread.is_alive():
            raise RuntimeError("native_event_receiver_did_not_stop")
        if self._error is not None:
            raise RuntimeError(self._error)

    def _run(self) -> None:
        pending = bytearray()
        try:
            listener = self._listener
            if listener is None:
                raise RuntimeError("native_event_socket_unavailable")
            while True:
                try:
                    connection, _ = listener.accept()
                    break
                except TimeoutError:
                    if self._stop.is_set():
                        return
            if not self._authorized.wait(timeout=10):
                raise RuntimeError("native_event_peer_unbound")
            credentials = connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            peer_pid, _peer_uid, _peer_gid = struct.unpack("3i", credentials)
            if peer_pid != self._authorized_pid:
                raise RuntimeError("native_event_peer_invalid")
            connection.settimeout(0.1)
            with connection:
                while True:
                    try:
                        block = connection.recv(1024 * 1024)
                    except TimeoutError:
                        if self._stop.is_set():
                            continue
                        continue
                    if not block:
                        if pending:
                            raise RuntimeError("native_event_line_incomplete")
                        return
                    pending.extend(block)
                    while True:
                        boundary = pending.find(b"\n")
                        if boundary < 0:
                            break
                        line = bytes(pending[:boundary])
                        del pending[:boundary + 1]
                        self._forward(line)
                    if len(pending) > MAX_NATIVE_EVENT_LINE_BYTES:
                        raise RuntimeError("native_event_line_too_large")
        except BaseException as exc:
            self._error = (
                str(exc) if isinstance(exc, RuntimeError) and str(exc)
                else "native_event_receiver_failed"
            )[:128]

    def _forward(self, line: bytes) -> None:
        if not line:
            return
        if len(line) > MAX_NATIVE_EVENT_LINE_BYTES:
            raise RuntimeError("native_event_line_too_large")
        try:
            envelope = json.loads(line)
        except (UnicodeError, ValueError, TypeError) as exc:
            raise RuntimeError("native_event_json_invalid") from exc
        event = envelope if isinstance(envelope, dict) else None
        if not isinstance(event, dict) or event.get("type") != "deepseek.session.event":
            raise RuntimeError("native_event_schema_invalid")
        self.ledger.append(event, channel="deepseek-harness")


def _safe_mcp_server_id(source: str, observed: set[str]) -> str:
    if SAFE_MCP_SERVER_ID.fullmatch(source) and source not in observed:
        observed.add(source)
        return source
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", source).strip("_-") or "server"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    candidate = f"{stem[:19]}-{digest}"
    if candidate in observed:
        raise RuntimeError("mcp_server_identity_collision")
    observed.add(candidate)
    return candidate


def _compile_mcp_patch(skill_root: Path, target: Path) -> dict[str, str]:
    """Translate the immutable ChatDS MCP view into upstream DSH plugins."""

    source = skill_root / "plugin" / ".mcp.json"
    if source.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("mcp_config_too_large")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RuntimeError("mcp_config_invalid") from exc
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    if not isinstance(servers, dict) or len(servers) > MAX_MCP_SERVERS:
        raise RuntimeError("mcp_config_invalid")
    entries: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    observed: set[str] = set()
    # Web search is deliberately supplied through DSH's provider-neutral Web
    # seam. Persistent schedules remain controller-owned and are not exposed
    # until this engine can publish the corresponding pending-write receipt.
    controller_owned = {"chatds-web-search", "chatds-schedule"}
    for original, row in sorted(servers.items()):
        if original in controller_owned:
            continue
        if not isinstance(original, str) or not isinstance(row, dict):
            raise RuntimeError("mcp_config_invalid")
        server_id = _safe_mcp_server_id(original, observed)
        mapping[original] = server_id
        server_type = row.get("type")
        if server_type == "stdio":
            command = row.get("command")
            args = row.get("args", [])
            env = row.get("env", {})
            if (
                not isinstance(command, str) or not command
                or not isinstance(args, list)
                or any(not isinstance(value, str) for value in args)
                or not isinstance(env, dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in env.items())
            ):
                raise RuntimeError("mcp_stdio_config_invalid")
            config = {
                "transport": "stdio",
                "serverName": server_id,
                "command": command,
                "args": args,
                "env": env,
                "cwd": "/workspace",
                "toolCallTimeoutMs": 120_000,
                "failOnStartupError": False,
            }
        elif server_type in {"http", "sse"}:
            url = row.get("url")
            headers = row.get("headers", {})
            if (
                not isinstance(url, str) or not url
                or not isinstance(headers, dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in headers.items())
            ):
                raise RuntimeError("mcp_http_config_invalid")
            config = {
                "transport": "streamable-http",
                "serverName": server_id,
                "url": url,
                "headers": headers,
                "toolCallTimeoutMs": 120_000,
                "failOnStartupError": False,
            }
        else:
            raise RuntimeError("mcp_transport_unsupported")
        entries.append({
            "id": f"chatds-mcp-{server_id}",
            "name": "@deepseek-ai/dsh-mcp-client",
            "config": config,
        })
    payload = [{"insert": entries}] if entries else []
    target.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return mapping


def _load_config() -> dict[str, Any]:
    path = Path(os.environ.get("CHATDS_RUN_CONFIG", ""))
    if path.as_posix() != "/run/chatds-control/request.json":
        raise RuntimeError("deepseek_run_config_path_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "chatds.deepseek-run.v1":
        raise RuntimeError("deepseek_run_config_invalid")
    return value


def _native_turn_payload(config: dict[str, Any]) -> bytes:
    """Compile the exact model-readable native Turn input."""

    native_session_id = config.get("native_session_id")
    permission_preset = _native_permission_preset(config)
    initial_prompt = config.get("initial_prompt")
    turn_prompt = config.get("turn_prompt")
    if (
        not isinstance(native_session_id, str)
        or SAFE_NATIVE_SESSION_ID.fullmatch(native_session_id) is None
        or not isinstance(initial_prompt, str)
        or not initial_prompt.strip()
        or not isinstance(turn_prompt, str)
        or not turn_prompt.strip()
    ):
        raise RuntimeError("native_turn_input_invalid")
    payload = json.dumps(
        {
            "schema": "chatds.deepseek-native-turn.v1",
            "native_session_id": native_session_id,
            "permission_preset": permission_preset,
            "initial_prompt": initial_prompt,
            "turn_prompt": turn_prompt,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if not payload or len(payload) > MAX_NATIVE_TURN_INPUT_BYTES:
        raise RuntimeError("native_turn_input_size_invalid")
    return payload


def _native_permission_preset(config: dict[str, Any]) -> str:
    """Compile one browser permission tier to its exact upstream baseline.

    The middle Web tier means that writes require an explicit one-shot grant.
    DSH expresses that natively as a read-only standing sandbox whose fs/shell
    tools may request ``workspace-write`` escalation.  Mount authority still
    distinguishes hard read-only (workspace mounted ro) from approvable write
    (workspace mounted rw); full access keeps DSH's native bypass preset.
    """

    permission = config.get("permission_preset")
    native = {
        "read_only": "read-only",
        "workspace_write": "read-only",
        "session_full": "danger-full-access",
    }.get(permission)
    if native is None:
        raise RuntimeError("deepseek_permission_preset_invalid")
    return native


def _write_native_turn_input(
    config: dict[str, Any],
    path: Path,
    *,
    worker_gid: int,
) -> None:
    """Materialize one bounded prompt outside argv/env and keep it immutable."""

    if path.as_posix() != "/runtime/controller/native-turn.json":
        raise RuntimeError("native_turn_input_path_invalid")
    payload = _native_turn_payload(config)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o440)
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            offset += os.write(descriptor, view[offset:])
        os.fsync(descriptor)
        os.fchown(descriptor, 0, worker_gid)
        os.fchmod(descriptor, 0o440)
    finally:
        os.close(descriptor)


def _workflow_projection_payload(config: dict[str, Any]) -> bytes | None:
    value = config.get("workflow_projection")
    if value is None:
        return None
    try:
        projection = validate_deepseek_workflow_projection(value)
    except ValueError as exc:
        raise RuntimeError("deepseek_workflow_projection_invalid") from exc
    payload = json.dumps(
        projection,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not payload or len(payload) > MAX_WORKFLOW_PROJECTION_BYTES:
        raise RuntimeError("deepseek_workflow_projection_size_invalid")
    return payload


def _write_workflow_projection(
    config: dict[str, Any],
    path: Path,
    *,
    worker_gid: int,
) -> bool:
    if path.as_posix() != "/runtime/controller/native-workflow.json":
        raise RuntimeError("deepseek_workflow_projection_path_invalid")
    payload = _workflow_projection_payload(config)
    if payload is None:
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o440)
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            offset += os.write(descriptor, view[offset:])
        os.fsync(descriptor)
        os.fchown(descriptor, 0, worker_gid)
        os.fchmod(descriptor, 0o440)
    finally:
        os.close(descriptor)
    return True


def _artifact_projection_payload(
    config: dict[str, Any],
    before: dict[str, tuple[int, ...]],
) -> bytes | None:
    try:
        projection = compile_deepseek_artifact_projection(
            contracts=config.get("artifact_contracts", []),
            bound_skill_name=config.get("bound_skill_name"),
            workflow_projection=config.get("workflow_projection"),
            native_session_id=str(config.get("native_session_id") or ""),
            workspace_before=before,
        )
    except ValueError as exc:
        raise RuntimeError("deepseek_artifact_projection_invalid") from exc
    if projection is None:
        return None
    try:
        normalized = validate_deepseek_artifact_projection(projection)
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeError("deepseek_artifact_projection_invalid") from exc
    if not payload or len(payload) > MAX_ARTIFACT_PROJECTION_BYTES:
        raise RuntimeError("deepseek_artifact_projection_size_invalid")
    return payload


def _write_artifact_projection(
    config: dict[str, Any],
    before: dict[str, tuple[int, ...]],
    path: Path,
    *,
    worker_gid: int,
) -> bool:
    if path.as_posix() != "/runtime/controller/native-artifacts.json":
        raise RuntimeError("deepseek_artifact_projection_path_invalid")
    payload = _artifact_projection_payload(config, before)
    if payload is None:
        return False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o440)
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            offset += os.write(descriptor, view[offset:])
        os.fsync(descriptor)
        os.fchown(descriptor, 0, worker_gid)
        os.fchmod(descriptor, 0o440)
    finally:
        os.close(descriptor)
    return True


def _emit_artifacts(
    ledger: Ledger,
    root: Path,
    before: dict[str, tuple[int, ...]],
    after: dict[str, tuple[int, ...]],
) -> None:
    changed = [path for path, value in after.items() if before.get(path) != value]
    if len(changed) > MAX_CHANGED_FILES:
        raise RuntimeError("workspace_artifact_change_limit")
    for relative in changed:
        path = root.joinpath(*relative.split("/"))
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise RuntimeError("workspace_artifact_unsafe")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        ledger.append({
            "type": "chatds.deepseek.artifact",
            "path": relative,
            "size_bytes": info.st_size,
            "sha256": digest.hexdigest(),
        })


def _terminal_outcome(
    *,
    exit_code: int,
    stop_reason: str | None,
    workflow_passed: bool,
    artifact_passed: bool,
) -> tuple[str, str | None]:
    """Choose exactly one authoritative terminal from monotonic receipts."""

    if stop_reason == "cancelled":
        return "cancelled", "cancelled"
    if stop_reason is not None:
        return "failed", stop_reason
    if not workflow_passed:
        return "failed", "workflow_contract_failed"
    if not artifact_passed:
        return "failed", "artifact_contract_failed"
    if exit_code != 0:
        return "failed", "runner_exit_nonzero"
    return "succeeded", None


def _signal_handler(signum: int, _frame: object) -> None:
    global _stop_reason
    _stop_reason = "cancelled" if signum in {signal.SIGINT, signal.SIGTERM} else "hard_timeout"
    child = _child
    if child is None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def _environment(
    config: dict[str, Any],
    *,
    proxy_url: str,
    trust: dict[str, str],
    worker_tmp: Path,
) -> dict[str, str]:
    sandbox_mode = _native_permission_preset(config)
    web_permission_preset = str(config.get("permission_preset") or "")
    if web_permission_preset not in {
        "read_only", "workspace_write", "session_full",
    }:
        raise RuntimeError("deepseek_permission_preset_invalid")
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/state/home",
        "DSH_HOME": "/state/dsh",
        "XDG_RUNTIME_DIR": str(worker_tmp),
        "XDG_CACHE_HOME": "/state/home/.cache",
        "TMPDIR": str(worker_tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DEEPSEEK_API_KEY": os.environ["DEEPSEEK_HARNESS_PROVIDER_API_KEY"],
        "DEEPSEEK_BASE_URL": str(config["provider_base_url"]),
        "CHATDS_DSH_MODEL": str(config["api_model"]),
        "CHATDS_DSH_CONTEXT_WINDOW": str(config["context_window_tokens"]),
        "CHATDS_DSH_MAX_OUTPUT_TOKENS": str(config["max_output_tokens"]),
        "CHATDS_SEARXNG_SEARCH_URL": str(config["searxng_search_url"]),
        "CHATDS_DSH_EVENT_PLUGIN": "/opt/chatds-deepseek-plugins/event_bridge.mjs",
        "CHATDS_DSH_SEARXNG_PLUGIN": "/opt/chatds-deepseek-plugins/searxng_provider.mjs",
        "CHATDS_EVENT_SOCKET": "/runtime/controller/native-events.sock",
        "CHATDS_DSH_TURN_INPUT": "/runtime/controller/native-turn.json",
        "DSH_TOOLS_MODE": "native",
        "DSH_PERMISSION_MODE": sandbox_mode,
        "CHATDS_WEB_PERMISSION_PRESET": web_permission_preset,
        "DSH_TELEMETRY_DISABLED": "1",
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "all_proxy": proxy_url,
        "NO_PROXY": "localhost,127.0.0.1,[::1]",
        "no_proxy": "localhost,127.0.0.1,[::1]",
        "NODE_USE_ENV_PROXY": "1",
        **trust,
    }
    if config.get("workflow_projection") is not None:
        environment["CHATDS_DSH_WORKFLOW_PROJECTION"] = (
            "/runtime/controller/native-workflow.json"
        )
    if config.get("artifact_contracts"):
        environment["CHATDS_DSH_ARTIFACT_PROJECTION"] = (
            "/runtime/controller/native-artifacts.json"
        )
    return environment


def _ledger_envelopes(path: Path):
    observed = 0
    with path.open("rb") as stream:
        for line in stream:
            observed += len(line)
            if observed > 512 * 1024 * 1024:
                raise RuntimeError("deepseek_event_ledger_size_limit")
            if len(line) > MAX_NATIVE_EVENT_LINE_BYTES:
                raise RuntimeError("native_event_line_too_large")
            try:
                value = json.loads(line)
            except (UnicodeError, ValueError, TypeError) as exc:
                raise RuntimeError("deepseek_event_ledger_invalid") from exc
            if not isinstance(value, dict):
                raise RuntimeError("deepseek_event_ledger_invalid")
            yield value


def _native_command(mcp_patch: Path) -> list[str]:
    """Build the immutable upstream CLI invocation for one isolated Turn."""

    return [
        "/usr/local/bin/node",
        "--expose-internals",
        "--use-env-proxy",
        "/opt/deepseek-harness/apps/cli/lib/bin.js",
        "--profile", "headless",
        "--patch", "/opt/chatds-deepseek-plugins/chatds.patch.yml",
        "--patch", str(mcp_patch),
        "chatds-native-turn",
    ]


def main() -> int:
    ledger = Ledger(Path("/run/chatds-control/events.jsonl"))
    stage = "load_config"
    bridge: LoopbackProxyBridge | None = None
    bridge_thread: threading.Thread | None = None
    event_receiver: NativeEventReceiver | None = None
    control_forwarder: ControlDecisionForwarder | None = None
    try:
        if os.geteuid() != 0:
            raise RuntimeError("runner_controller_not_root")
        config = _load_config()
        worker_uid = int(os.environ.get("DEEPSEEK_HARNESS_RUNNER_WORKER_UID", "65529"))
        worker_gid = int(os.environ.get("DEEPSEEK_HARNESS_RUNNER_WORKER_GID", "65529"))
        for path in (Path("/state/home"), Path("/state/dsh"), Path("/runtime/worker")):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chown(path, worker_uid, worker_gid)
        controller_runtime = Path("/runtime/controller")
        controller_runtime.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.chown(controller_runtime, 0, worker_gid)
        os.chmod(controller_runtime, 0o750)
        worker_tmp = Path("/runtime/worker/tmp")
        worker_tmp.mkdir(exist_ok=True, mode=0o700)
        os.chown(worker_tmp, worker_uid, worker_gid)
        native_turn_input = Path("/runtime/controller/native-turn.json")
        _write_native_turn_input(
            config,
            native_turn_input,
            worker_gid=worker_gid,
        )
        native_workflow_projection = Path(
            "/runtime/controller/native-workflow.json"
        )
        _write_workflow_projection(
            config,
            native_workflow_projection,
            worker_gid=worker_gid,
        )
        mcp_patch = Path("/runtime/controller/mcp.patch.json")
        control_decisions = Path("/runtime/worker/control-decisions.jsonl")
        mcp_mapping = _compile_mcp_patch(Path("/skill-view"), mcp_patch)
        os.chown(mcp_patch, 0, worker_gid)
        os.chmod(mcp_patch, 0o440)
        control_decisions.touch(mode=0o600, exist_ok=False)
        os.chown(control_decisions, worker_uid, worker_gid)
        os.chmod(control_decisions, 0o600)
        ledger.append({
            "type": "chatds.deepseek.runtime.config",
            "context_window_tokens": config["context_window_tokens"],
            "max_output_tokens": config["max_output_tokens"],
            "native_session_id": config["native_session_id"],
            "permission_preset": config["permission_preset"],
            "native_permission_preset": _native_permission_preset(config),
            "web_search_enabled": bool(config.get("web_search_enabled")),
            "mcp_server_mapping": mcp_mapping,
        })
        stage = "workspace_lock"
        with _session_workspace_lock(config):
            before = workspace_snapshot(Path("/workspace"))
            _write_artifact_projection(
                config,
                before,
                Path("/runtime/controller/native-artifacts.json"),
                worker_gid=worker_gid,
            )
            stage = "egress_bridge_start"
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
            ).materialize(
                Path("/runtime/worker"), worker_uid=worker_uid, worker_gid=worker_gid
            )
            policy = config["egress_policy"]
            bridge = LoopbackProxyBridge(
                authority,
                ("127.0.0.1", 0),
                origin_allowlist=tuple(policy["origin_allowlist"]),
                egress_rules=tuple(policy["egress_rules"]),
                private_origins=tuple(policy["private_origins"]),
                public_read=policy.get("public_read"),
                policy_token=os.environ["SKILL_EGRESS_POLICY_TOKEN"],
                trust_generation=trust["SKILL_EGRESS_TRUST_GENERATION"],
                budget_scope_sha256=policy["budget_scope_sha256"],
                call_id_sha256=policy["call_id_sha256"],
                limits=policy["limits"],
            )
            bridge_thread = threading.Thread(
                target=bridge.serve_forever,
                kwargs={"poll_interval": 0.1},
                daemon=True,
                name="deepseek-egress-bridge",
            )
            bridge_thread.start()
            proxy_url = f"http://127.0.0.1:{bridge.server_address[1]}"
            environment = _environment(
                config, proxy_url=proxy_url, trust=trust, worker_tmp=worker_tmp
            )
            environment["CHATDS_CONTROL_DECISIONS"] = "/runtime/worker/control-decisions.jsonl"
            command = _native_command(mcp_patch)
            stage = "native_execution"
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1):
                signal.signal(signum, _signal_handler)
            global _child
            stdout_path = Path("/runtime/worker/stdout.log")
            stderr_path = Path("/runtime/worker/stderr.log")
            event_receiver = NativeEventReceiver(
                Path("/runtime/controller/native-events.sock"),
                ledger,
                worker_gid=worker_gid,
            )
            event_receiver.start()

            # Forward supervisor control decisions into worker-readable tmpfs
            # Supervisor writes decisions to /run/chatds-control/control-decisions.jsonl
            control_mailbox = Path("/run/chatds-control/control-decisions.jsonl")
            control_forwarder = ControlDecisionForwarder(control_mailbox, control_decisions)
            control_forwarder.start()
            with stdout_path.open("w+b") as stdout_file, stderr_path.open("w+b") as stderr_file:
                _child = subprocess.Popen(
                    command,
                    cwd="/workspace",
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    user=worker_uid,
                    group=worker_gid,
                    extra_groups=(),
                    umask=0o077,
                    start_new_session=True,
                )
                event_receiver.authorize_pid(_child.pid)
                exit_code = _child.wait()
                stdout_file.flush()
                stderr_file.flush()
                stdout_file.seek(max(0, stdout_file.tell() - 64_000))
                stderr_file.seek(max(0, stderr_file.tell() - 64_000))
                stdout = stdout_file.read()
                stderr = stderr_file.read()
            control_forwarder.stop()
            control_forwarder = None
            event_receiver.stop()
            event_receiver = None
            if stdout:
                ledger.append({
                    "type": "chatds.deepseek.stdout",
                    "text": stdout.decode("utf-8", errors="replace")[-64_000:],
                }, channel="native-stdout")
            if stderr:
                ledger.append({
                    "type": "chatds.deepseek.stderr",
                    "text": stderr.decode("utf-8", errors="replace")[-64_000:],
                }, channel="native-stderr")
            workflow_passed = True
            if config.get("workflow_projection") is not None:
                stage = "workflow_contract_audit"
                workflow_receipt = build_deepseek_workflow_receipt(
                    config["workflow_projection"],
                    _ledger_envelopes(ledger.path),
                )
                workflow_passed = workflow_receipt.get("status") == "passed"
                ledger.append({
                    "type": "chatds.deepseek.workflow_receipt",
                    **workflow_receipt,
                })
            stage = "egress_bridge_seal"
            bridge.shutdown_and_seal()
            bridge_thread.join(timeout=5)
            if bridge_thread.is_alive():
                raise RuntimeError("egress_bridge_did_not_stop")
            bridge = None
            after = workspace_snapshot(Path("/workspace"))
            artifact_passed = True
            if workflow_passed:
                stage = "artifact_contract_audit"
                active_skill_names = active_artifact_skill_names(
                    contracts=config.get("artifact_contracts", []),
                    workflow_projection=config.get("workflow_projection"),
                    bound_skill_name=config.get("bound_skill_name"),
                    envelopes=_ledger_envelopes(ledger.path),
                    native_session_id=str(config["native_session_id"]),
                )
                artifact_receipt = validate_artifact_contracts(
                    contracts=config.get("artifact_contracts", []),
                    active_skill_name=None,
                    active_skill_names=active_skill_names,
                    before=before,
                    after=after,
                    workspace_root=Path("/workspace"),
                )
                artifact_passed = artifact_receipt.get("status") != "failed"
            else:
                artifact_receipt = {
                    "schema": "chatds.native-artifact-receipt.v1",
                    "status": "deferred",
                    "reason": "workflow_contract_failed",
                }
            ledger.append({
                "type": "chatds.deepseek.artifact_receipt",
                **artifact_receipt,
            })
            _emit_artifacts(ledger, Path("/workspace"), before, after)
        status, terminal_error = _terminal_outcome(
            exit_code=exit_code,
            stop_reason=_stop_reason,
            workflow_passed=workflow_passed,
            artifact_passed=artifact_passed,
        )
        ledger.append({
            "type": "chatds.supervisor.terminal",
            "status": status,
            "exit_code": exit_code,
            "error": terminal_error,
            "error_stage": None if status == "succeeded" else stage,
        })
        return 0 if status == "succeeded" else 1
    except BaseException as exc:
        if control_forwarder is not None:
            try:
                control_forwarder.stop()
            except Exception:
                pass
        if event_receiver is not None:
            try:
                event_receiver.stop()
            except Exception:
                pass
        if bridge is not None:
            try:
                bridge.shutdown_and_seal()
            except Exception:
                pass
        if bridge_thread is not None:
            bridge_thread.join(timeout=2)
        code = str(exc) if isinstance(exc, RuntimeError) and str(exc) else type(exc).__name__
        try:
            ledger.append({
                "type": "chatds.supervisor.terminal",
                "status": "failed",
                "exit_code": 70,
                "error": code[:128],
                "error_stage": stage,
            })
        except Exception:
            pass
        sys.stderr.write(json.dumps({"type": "chatds.deepseek.fatal", "code": code[:128]}) + "\n")
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
