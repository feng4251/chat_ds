"""PID 1 controller for one isolated upstream DeepSeek Harness Turn."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
MAX_MCP_SERVERS = 128
WORKSPACE_LOCK_IDENTITY_DOMAIN = b"chatds-workspace-mutation-lock-v1\0"
SAFE_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
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


class NativeEventForwarder:
    """Move untrusted worker events into the controller-owned ledger.

    The model process can append only to a tmpfs spool owned by its UID.  It
    never receives write or traversal authority over the supervisor ledger.
    This controller thread forwards complete, bounded JSONL records in order.
    """

    def __init__(self, spool: Path, ledger: Ledger) -> None:
        self.spool = spool
        self.ledger = ledger
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="deepseek-native-event-forwarder",
        )
        self._error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("native_event_forwarder_did_not_stop")
        if self._error is not None:
            raise RuntimeError(self._error)

    def _run(self) -> None:
        position = 0
        pending = bytearray()
        try:
            while True:
                try:
                    with self.spool.open("rb") as stream:
                        stream.seek(position)
                        block = stream.read(1024 * 1024)
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
                        self._forward(line)
                    if len(pending) > MAX_NATIVE_EVENT_LINE_BYTES:
                        raise RuntimeError("native_event_line_too_large")
                    continue
                if self._stop.is_set():
                    if pending:
                        raise RuntimeError("native_event_line_incomplete")
                    return
                time.sleep(0.02)
        except BaseException as exc:
            self._error = (
                str(exc) if isinstance(exc, RuntimeError) and str(exc)
                else "native_event_forwarder_failed"
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
        event = envelope.get("event") if isinstance(envelope, dict) else None
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


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    count = 0
    for walk_root, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(dirs)
        for name in sorted(files):
            count += 1
            if count > MAX_FILES:
                raise RuntimeError("workspace_entry_limit_exceeded")
            path = Path(walk_root) / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError("workspace_contains_special_file")
            result[path.relative_to(root).as_posix()] = (
                int(info.st_size),
                int(info.st_mtime_ns),
            )
    return result


def _emit_artifacts(
    ledger: Ledger,
    root: Path,
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
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
    permission = str(config.get("permission_preset") or "workspace_write")
    sandbox_mode = {
        "read_only": "read-only",
        "workspace_write": "workspace-write",
        "session_full": "danger-full-access",
    }.get(permission)
    if sandbox_mode is None:
        raise RuntimeError("deepseek_permission_preset_invalid")
    tools = {
        str(value) for value in config.get("tools", [])
        if isinstance(value, str)
    }


def _native_command(config: dict[str, Any], mcp_patch: Path) -> list[str]:
    """Build the immutable upstream CLI invocation for one isolated Turn."""

    return [
        "/usr/local/bin/node",
        "--expose-internals",
        "--use-env-proxy",
        "/opt/deepseek-harness/apps/cli/lib/bin.js",
        "--profile", "headless",
        "--patch", "/opt/chatds-deepseek-plugins/chatds.patch.yml",
        "--patch", str(mcp_patch),
        str(config["prompt"]),
    ]
    shell_tools = {
        "execute_code", "run_skill_python", "run_skill_script",
        "run_declared_command", "skill_http_get", "skill_http_post_json",
        "web_extract",
    }
    file_tools = {
        "read_file", "write_file", "patch_file", "merge_files", "search_files",
    }
    skill_tools = {"skills_list", "skill_view", "skill_copy_resource"}
    goal_tools = {"get_goal", "create_goal", "update_goal"}
    return {
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
        "CHATDS_DSH_WEB_SEARCH_ENABLED": "1" if config.get("web_search_enabled") else "0",
        "CHATDS_DSH_SHELL_ENABLED": "1" if tools & shell_tools else "0",
        "CHATDS_DSH_FILES_ENABLED": "1" if tools & file_tools else "0",
        "CHATDS_DSH_SKILLS_ENABLED": "1" if tools & skill_tools else "0",
        "CHATDS_DSH_SUBAGENTS_ENABLED": "1" if "delegate_task" in tools else "0",
        "CHATDS_DSH_TODO_ENABLED": "1" if "todo" in tools else "0",
        "CHATDS_DSH_GOALS_ENABLED": "1" if tools & goal_tools else "0",
        "CHATDS_SEARXNG_SEARCH_URL": str(config["searxng_search_url"]),
        "CHATDS_DSH_EVENT_PLUGIN": "/opt/chatds-deepseek-plugins/event_bridge.mjs",
        "CHATDS_DSH_SEARXNG_PLUGIN": "/opt/chatds-deepseek-plugins/searxng_provider.mjs",
        "CHATDS_EVENT_LEDGER": "/runtime/worker/native-events.jsonl",
        "DSH_PERMISSION_MODE": sandbox_mode,
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


def main() -> int:
    ledger = Ledger(Path("/run/chatds-control/events.jsonl"))
    stage = "load_config"
    bridge: LoopbackProxyBridge | None = None
    bridge_thread: threading.Thread | None = None
    forwarder: NativeEventForwarder | None = None
    try:
        if os.geteuid() != 0:
            raise RuntimeError("runner_controller_not_root")
        config = _load_config()
        worker_uid = int(os.environ.get("DEEPSEEK_HARNESS_RUNNER_WORKER_UID", "65529"))
        worker_gid = int(os.environ.get("DEEPSEEK_HARNESS_RUNNER_WORKER_GID", "65529"))
        for path in (Path("/state/home"), Path("/state/dsh"), Path("/runtime/worker")):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chown(path, worker_uid, worker_gid)
        worker_tmp = Path("/runtime/worker/tmp")
        worker_tmp.mkdir(exist_ok=True, mode=0o700)
        os.chown(worker_tmp, worker_uid, worker_gid)
        native_spool = Path("/runtime/worker/native-events.jsonl")
        native_spool.touch(mode=0o600, exist_ok=False)
        os.chown(native_spool, worker_uid, worker_gid)
        mcp_patch = Path("/runtime/worker/mcp.patch.json")
        mcp_mapping = _compile_mcp_patch(Path("/skill-view"), mcp_patch)
        os.chown(mcp_patch, worker_uid, worker_gid)
        os.chmod(mcp_patch, 0o400)
        ledger.append({
            "type": "chatds.deepseek.runtime.config",
            "context_window_tokens": config["context_window_tokens"],
            "max_output_tokens": config["max_output_tokens"],
            "web_search_enabled": bool(config.get("web_search_enabled")),
            "mcp_server_mapping": mcp_mapping,
        })
        stage = "workspace_lock"
        with _session_workspace_lock(config):
            before = _snapshot(Path("/workspace"))
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
            command = _native_command(config, mcp_patch)
            stage = "native_execution"
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1):
                signal.signal(signum, _signal_handler)
            global _child
            stdout_path = Path("/runtime/worker/stdout.log")
            stderr_path = Path("/runtime/worker/stderr.log")
            forwarder = NativeEventForwarder(native_spool, ledger)
            forwarder.start()
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
                exit_code = _child.wait()
                stdout_file.flush()
                stderr_file.flush()
                stdout_file.seek(max(0, stdout_file.tell() - 64_000))
                stderr_file.seek(max(0, stderr_file.tell() - 64_000))
                stdout = stdout_file.read()
                stderr = stderr_file.read()
            forwarder.stop()
            forwarder = None
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
            stage = "egress_bridge_seal"
            bridge.shutdown_and_seal()
            bridge_thread.join(timeout=5)
            if bridge_thread.is_alive():
                raise RuntimeError("egress_bridge_did_not_stop")
            bridge = None
            after = _snapshot(Path("/workspace"))
            _emit_artifacts(ledger, Path("/workspace"), before, after)
        status = "cancelled" if _stop_reason == "cancelled" else (
            "succeeded" if exit_code == 0 else "failed"
        )
        ledger.append({
            "type": "chatds.supervisor.terminal",
            "status": status,
            "exit_code": exit_code,
            "error": None if status == "succeeded" else (_stop_reason or "runner_exit_nonzero"),
            "error_stage": None if status == "succeeded" else stage,
        })
        return 0 if status == "succeeded" else 1
    except BaseException as exc:
        if forwarder is not None:
            try:
                forwarder.stop()
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
