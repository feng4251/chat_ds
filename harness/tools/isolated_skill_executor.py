"""Client for the isolated session-code and Skill-execution protocol.

This module is intentionally lower level than the public Skill tools.  Its
caller must first resolve and authorize an installed Skill root.  The client
then snapshots only that exact root and the current session workspace, sends
content-addressed regular files over a bounded Unix socket request, validates
the complete response, and atomically replaces each returned workspace file.
Skill Python may run as a CLI or as one strictly data-described public
top-level function call; neither form accepts model-authored wrapper code.
The additive protocol-v2 API retains authenticated persistent CLI processes
and strictly declared public class/factory objects in trusted runtime state.
Direct networking is absent; exact Skill executions can receive only the
runtime-compiled origin set through the policy proxy.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from tools.executor_slot_pool import (
    ExecutorSlotPoolError,
    ExecutorSlotReservation,
    get_executor_slot_pool,
)
from tools.session_sandbox_policy import (
    MAX_SESSION_SANDBOX_EGRESS_ORIGINS,
    MAX_SESSION_SANDBOX_EGRESS_RULES,
    SESSION_SANDBOX_RUNTIME_PROFILE,
    SessionSandboxEgressRule,
    SessionSandboxPolicyError,
    normalize_http_origin,
    normalize_session_sandbox_egress_rules,
)
from tools.workspace_lock import (
    run_sync_cancellation_safe,
    workspace_mutation_guard,
)


EXECUTOR_SOCKET = os.environ.get(
    "EXECUTOR_SOCKET", "/run/chat-ds-executor/executor.sock"
)
PROTOCOL_VERSION = 1
PROCESS_PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 96 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_STDOUT_BYTES = 80_000
MAX_STDERR_BYTES = 20_000
MAX_SKILL_TIMEOUT = 300
MAX_CODE_BYTES = 900_000
MAX_ARGS = 64
MAX_ARG_BYTES = 32_768
MAX_ARG_CHARS = 4_096
MAX_FUNCTION_INPUT_BYTES = 128_000
MAX_FUNCTION_ARGS = 64
MAX_FUNCTION_KWARGS = 128
MAX_FUNCTION_JSON_DEPTH = 20
MAX_PATH_BYTES = 2_048
MAX_PATH_DEPTH = 32
MAX_PATH_COMPONENT_BYTES = 255

MAX_SKILL_FILES = 1_024
MAX_SKILL_FILE_BYTES = 8 * 1024 * 1024
MAX_SKILL_TOTAL_BYTES = 24 * 1024 * 1024
MAX_WORKSPACE_FILES = 512
MAX_WORKSPACE_FILE_BYTES = 24 * 1024 * 1024
MAX_WORKSPACE_TOTAL_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_FILES = 512
MAX_OUTPUT_FILE_BYTES = 24 * 1024 * 1024
MAX_OUTPUT_TOTAL_BYTES = 24 * 1024 * 1024
MAX_SNAPSHOT_ENTRIES = 4_096
MAX_RUNTIME_REQUIREMENTS = 80
MAX_RUNTIME_COMMANDS = 80
MAX_RUNTIME_ENVIRONMENT_VARIABLES = 80
MAX_RUNTIME_PLATFORM_GROUPS = 32
MAX_RUNTIME_PLATFORMS_PER_GROUP = 32
MAX_RUNTIME_DECLARATION_CHARS = 512
MAX_PROCESS_LEASE_TTL_SECONDS = 3_600
MAX_PROCESS_RUNTIME_SECONDS = 3_600
MAX_PROCESS_STDIN_CHUNK_BYTES = 64 * 1024
MAX_PROCESS_CALL_BYTES = 4 * 1024
MAX_PROCESS_READ_BYTES = 256 * 1024
MAX_PROCESS_READ_WAIT_MS = 60_000
PROCESS_SYNC_ACK_GRACE_SECONDS = 300

# These are the actual terminal codes emitted by the process protocol.  The
# legacy ``lease_not_found`` alias remains accepted for a rolling Harness /
# executor upgrade, while malformed handles are quarantined because they do
# not prove whether the originally reserved worker is still occupied.
TERMINAL_PROCESS_LEASE_RELEASE_ERROR_CODES = frozenset({
    "lease_expired",
    "lease_lost",
    "lease_closed",
    "lease_not_found",
})
TERMINAL_PROCESS_LEASE_QUARANTINE_ERROR_CODES = frozenset({
    "invalid_lease_handle",
})

SUPPORTED_EXTENSIONS = frozenset({".py", ".sh", ".bash", ".js", ".mjs", ".cjs"})
PUBLIC_FUNCTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
RUNTIME_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
RUNTIME_ENVIRONMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
RUNTIME_PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WORKSPACE_SKIP_DIRS = frozenset({
    ".chatds",
    "debug",
    "cache",
    "__pycache__",
    ".pytest_cache",
    ".git",
})


def _validated_egress_origins(
    values: tuple[str, ...] | list[str] | None,
) -> list[str]:
    """Validate runtime-owned origin grants before crossing the executor UDS."""

    if values is None:
        return []
    if not isinstance(values, (tuple, list)) or len(values) > (
        MAX_SESSION_SANDBOX_EGRESS_ORIGINS
    ):
        raise IsolatedSkillExecutorError(
            "invalid_egress_policy",
            "Session sandbox egress origins exceed the bounded policy.",
        )
    result: list[str] = []
    for value in values:
        try:
            normalized = normalize_http_origin(value)
        except SessionSandboxPolicyError as exc:
            raise IsolatedSkillExecutorError(
                "invalid_egress_policy",
                "Session sandbox egress contains an invalid HTTP(S) origin.",
            ) from exc
        if normalized != value or normalized in result:
            raise IsolatedSkillExecutorError(
                "invalid_egress_policy",
                "Session sandbox egress origins must be canonical and unique.",
            )
        result.append(normalized)
    return result


def _validated_exact_egress_policy(
    *,
    egress_rules: (
        tuple[dict[str, Any], ...]
        | list[dict[str, Any]]
        | tuple[SessionSandboxEgressRule, ...]
        | None
    ),
    private_origins: tuple[str, ...] | list[str] | None,
    legacy_origins: tuple[str, ...] | list[str] | None,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Validate exact policy fields before crossing the executor UDS.

    ``legacy_origins`` is accepted only as a redundant equality assertion
    when method/prefix rules are present. It can never create authority.
    """

    raw_rules = () if egress_rules is None else egress_rules
    try:
        rules = normalize_session_sandbox_egress_rules(raw_rules)
    except SessionSandboxPolicyError as exc:
        raise IsolatedSkillExecutorError(
            "invalid_egress_policy",
            "Session sandbox egress contains malformed exact URL rules.",
        ) from exc
    if len(rules) > MAX_SESSION_SANDBOX_EGRESS_RULES:
        raise IsolatedSkillExecutorError(
            "invalid_egress_policy",
            "Session sandbox egress exceeds the bounded exact-rule policy.",
        )
    rule_payload = [rule.as_payload() for rule in rules]
    origins = list(dict.fromkeys(
        normalize_http_origin(rule.url_prefix)
        for rule in rules
    ))
    if len(origins) > MAX_SESSION_SANDBOX_EGRESS_ORIGINS:
        raise IsolatedSkillExecutorError(
            "invalid_egress_policy",
            "Session sandbox egress exceeds the bounded origin policy.",
        )
    asserted_origins = _validated_egress_origins(legacy_origins)
    if asserted_origins and asserted_origins != origins:
        raise IsolatedSkillExecutorError(
            "invalid_egress_policy",
            "Origin-only egress authority is forbidden; origins must equal "
            "the exact method/prefix rule projection.",
        )
    raw_private = _validated_egress_origins(private_origins)
    if any(origin not in set(origins) for origin in raw_private):
        raise IsolatedSkillExecutorError(
            "invalid_egress_policy",
            "Private sandbox origins must be a subset derived from exact URL "
            "rules.",
        )
    return rule_payload, origins, raw_private


def _validated_egress_budget_binding(
    *,
    has_rules: bool,
    budget_scope_sha256: str | None,
    call_id_sha256: str | None,
) -> tuple[int, dict[str, str]]:
    """Validate the additive v3 aggregate-budget/audit binding.

    Omitting both fields preserves the legacy v2 wire contract for old
    internal callers.  New runtime-owned tool dispatches provide both fields
    and therefore fail closed unless the executor understands v3.
    """

    if budget_scope_sha256 is None and call_id_sha256 is None:
        return 2, {}
    if (
        not has_rules
        or not isinstance(budget_scope_sha256, str)
        or not isinstance(call_id_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", budget_scope_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", call_id_sha256) is None
    ):
        raise IsolatedSkillExecutorError(
            "invalid_egress_policy",
            "Bounded egress requires matching lowercase SHA-256 scope and "
            "call bindings derived from runtime state.",
        )
    return 3, {
        "budget_scope_sha256": budget_scope_sha256,
        "call_id_sha256": call_id_sha256,
    }


class IsolatedSkillExecutorError(ValueError):
    """A stable local snapshot, transport, or response validation failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        dispatch_unknown: bool = False,
        terminal_lease_state: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.dispatch_unknown = bool(dispatch_unknown)
        self.terminal_lease_state = (
            terminal_lease_state
            if terminal_lease_state in {"closed", "quarantined"}
            else None
        )


class _ProcessDispatchCancelled(asyncio.CancelledError):
    """Cancellation whose process operation may already have been accepted."""

    dispatch_unknown = True


@dataclass(frozen=True, slots=True)
class _SkillSnapshotFile:
    """One immutable regular file captured by the bounded tree walker."""

    path: str
    content: bytes = field(repr=False)
    sha256: str


@dataclass(frozen=True, slots=True)
class SkillPackageSnapshot:
    """Immutable exact Skill bytes shared by authorization, routing and send.

    The constructor is public only as a Python type boundary; consumers must
    obtain values from :func:`snapshot_skill_package`.  Every protocol use
    revalidates the complete object before serializing it, so a hand-built or
    otherwise corrupted instance cannot become executor input.
    """

    files: tuple[_SkillSnapshotFile, ...] = field(repr=False)
    sha256: str

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files)

    def read_bytes(self, relative_path: str) -> bytes:
        for item in self.files:
            if item.path == relative_path:
                return item.content
        raise KeyError(relative_path)

    def file_sha256(self, relative_path: str) -> str:
        for item in self.files:
            if item.path == relative_path:
                return item.sha256
        raise KeyError(relative_path)


@dataclass(frozen=True, slots=True)
class ProcessOwnerScope:
    """Trusted runtime identity; never expose its token in a model tool schema."""

    user_id: str
    session_id: str
    root_run_id: str
    _authority_token: str = field(repr=False)

    def _protocol_value(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "root_run_id": self.root_run_id,
            "authority_token": self._authority_token,
        }


@dataclass(slots=True)
class IsolatedProcessLease:
    """Opaque client-side capability retained by trusted Harness runtime state."""

    handle: str
    skill_sha256: str
    script_sha256: str
    entrypoint: str
    invocation_mode: str
    class_name: str | None
    factory_name: str | None
    _owner_scope: ProcessOwnerScope = field(repr=False)
    _workspace: Path = field(repr=False)
    _socket_path: str = field(repr=False)
    _baseline: dict[str, tuple[int, str]] = field(repr=False)
    _egress_policy_version: int = field(default=2, repr=False)
    _budget_scope_sha256: str | None = field(default=None, repr=False)
    _call_id_sha256: str | None = field(default=None, repr=False)
    _egress_authority_sha256: str | None = field(default=None, repr=False)
    _slot_reservation: ExecutorSlotReservation | None = field(
        default=None,
        repr=False,
    )
    _pending_sync_operation: str | None = field(default=None, repr=False)
    _pending_sync_prepare_op_id: str | None = field(default=None, repr=False)
    _pending_sync_response: dict[str, Any] | None = field(default=None, repr=False)
    _pending_sync_artifacts: list[tuple[str, bytes, dict[str, Any]]] | None = field(
        default=None,
        repr=False,
    )
    _pending_sync_ack_op_id: str | None = field(default=None, repr=False)
    _pending_sync_applied: list[dict[str, Any]] | None = field(
        default=None,
        repr=False,
    )
    _pending_sync_apply_artifacts: bool | None = field(
        default=None,
        repr=False,
    )
    _closed_response: dict[str, Any] | None = field(default=None, repr=False)
    closed: bool = False


def terminal_process_lease_error_action(
    error: IsolatedSkillExecutorError,
) -> str | None:
    """Classify only server-proven terminal lease outcomes."""

    if error.dispatch_unknown:
        return None
    if error.terminal_lease_state == "closed":
        return "release"
    if error.terminal_lease_state == "quarantined":
        return "quarantine"
    if error.code in TERMINAL_PROCESS_LEASE_RELEASE_ERROR_CODES:
        return "release"
    if error.code in TERMINAL_PROCESS_LEASE_QUARANTINE_ERROR_CODES:
        return "quarantine"
    return None


async def finalize_terminal_process_lease_error(
    lease: IsolatedProcessLease,
    error: IsolatedSkillExecutorError,
) -> bool:
    """Transition the physical slot before a manager forgets a lost lease."""

    action = terminal_process_lease_error_action(error)
    if action is None:
        return False
    reservation = lease._slot_reservation
    if reservation is not None:
        try:
            if not reservation.terminal:
                if action == "release":
                    await _release_executor_slot(reservation)
                else:
                    await _quarantine_executor_slot(
                        reservation,
                        error.code,
                    )
        finally:
            if (
                reservation.terminal
                and lease._slot_reservation is reservation
            ):
                lease._slot_reservation = None
    lease.closed = True
    return True


def create_process_owner_scope(
    *,
    user_id: str,
    session_id: str,
    root_run_id: str,
) -> ProcessOwnerScope:
    """Create a non-model-facing owner capability from ToolContext identity."""

    values = {
        "user_id": user_id,
        "session_id": session_id,
        "root_run_id": root_run_id,
    }
    for field_name, value in values.items():
        if not isinstance(value, str) or not value or "\x00" in value:
            raise IsolatedSkillExecutorError(
                "invalid_owner_scope",
                f"{field_name} must be a non-empty runtime identity string.",
            )
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise IsolatedSkillExecutorError(
                "invalid_owner_scope",
                f"{field_name} must be valid UTF-8.",
            ) from exc
        if len(encoded) > 256 or any(ord(character) < 0x20 for character in value):
            raise IsolatedSkillExecutorError(
                "invalid_owner_scope",
                f"{field_name} is outside the bounded runtime identity policy.",
            )
    return ProcessOwnerScope(
        user_id=user_id,
        session_id=session_id,
        root_run_id=root_run_id,
        _authority_token=secrets.token_urlsafe(32),
    )


def _safe_relative_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise IsolatedSkillExecutorError(
            "invalid_path", f"{field} must be a non-empty relative path."
        )
    if "\x00" in value or "\\" in value:
        raise IsolatedSkillExecutorError(
            "invalid_path", f"{field} contains an unsafe path character."
        )
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise IsolatedSkillExecutorError(
            "invalid_path", f"{field} is not valid UTF-8."
        ) from exc
    components = value.split("/")
    if (
        PurePosixPath(value).is_absolute()
        or len(encoded) > MAX_PATH_BYTES
        or len(components) > MAX_PATH_DEPTH
        or any(part in {"", ".", ".."} for part in components)
        or any(len(part.encode("utf-8")) > MAX_PATH_COMPONENT_BYTES for part in components)
    ):
        raise IsolatedSkillExecutorError(
            "invalid_path", f"{field} is outside the bounded relative-path policy."
        )
    return "/".join(components)


def _verified_directory(root: Path, *, field: str) -> Path:
    candidate = Path(root)
    try:
        lexical = candidate.lstat()
    except OSError as exc:
        raise IsolatedSkillExecutorError(
            "missing_snapshot_root", f"{field} is unavailable."
        ) from exc
    if stat.S_ISLNK(lexical.st_mode) or not stat.S_ISDIR(lexical.st_mode):
        raise IsolatedSkillExecutorError(
            "unsafe_snapshot_root", f"{field} must be a real, non-symlink directory."
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise IsolatedSkillExecutorError(
            "unsafe_snapshot_root", f"{field} cannot be resolved safely."
        ) from exc
    # A trusted Skill/workspace resolver should already provide a canonical
    # path. Rejecting a different lexical path also catches symlink ancestors,
    # rather than checking only whether the leaf itself is a symlink.
    if Path(os.path.abspath(candidate)) != resolved:
        raise IsolatedSkillExecutorError(
            "unsafe_snapshot_root", f"{field} contains a symlink or non-canonical component."
        )
    return resolved


def _read_snapshot_file(
    path: Path,
    *,
    display_path: str,
    max_file_bytes: int,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise IsolatedSkillExecutorError(
            "unsafe_snapshot_file", f"Cannot safely open snapshot file: {display_path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise IsolatedSkillExecutorError(
                "unsafe_snapshot_file",
                f"Snapshot entries must be independent regular files: {display_path}",
            )
        if before.st_size > max_file_bytes:
            raise IsolatedSkillExecutorError(
                "snapshot_limit_exceeded", f"Snapshot file is too large: {display_path}"
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if len(content) != before.st_size or before_identity != after_identity:
            raise IsolatedSkillExecutorError(
                "snapshot_race", f"Snapshot file changed while being read: {display_path}"
            )
        return content
    finally:
        os.close(descriptor)


def _snapshot_tree(
    root: Path,
    *,
    field: str,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
    skip_dirs: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    verified_root = _verified_directory(root, field=field)
    files: list[dict[str, Any]] = []
    total = 0
    entries = 0
    stack: list[tuple[Path, str, int]] = [(verified_root, "", 0)]

    while stack:
        directory, prefix, depth = stack.pop()
        try:
            remaining_entries = MAX_SNAPSHOT_ENTRIES - entries
            children: list[os.DirEntry[str]] = []
            with os.scandir(directory) as scanner:
                for child in scanner:
                    children.append(child)
                    if len(children) > remaining_entries:
                        raise IsolatedSkillExecutorError(
                            "snapshot_limit_exceeded",
                            f"{field} has too many filesystem entries.",
                        )
            children.sort(key=lambda entry: entry.name)
        except OSError as exc:
            raise IsolatedSkillExecutorError(
                "unsafe_snapshot_tree", f"Cannot safely scan {field}."
            ) from exc
        for child in children:
            entries += 1
            if entries > MAX_SNAPSHOT_ENTRIES:
                raise IsolatedSkillExecutorError(
                    "snapshot_limit_exceeded", f"{field} has too many filesystem entries."
                )
            relative = f"{prefix}/{child.name}" if prefix else child.name
            safe_relative = _safe_relative_path(relative, field=f"{field} path")
            try:
                item_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise IsolatedSkillExecutorError(
                    "unsafe_snapshot_tree", f"Cannot inspect snapshot entry: {safe_relative}"
                ) from exc
            mode = item_stat.st_mode
            if stat.S_ISLNK(mode):
                raise IsolatedSkillExecutorError(
                    "unsafe_snapshot_file", f"Symlink snapshot entries are forbidden: {safe_relative}"
                )
            if stat.S_ISDIR(mode):
                if child.name in skip_dirs:
                    continue
                if depth + 1 >= MAX_PATH_DEPTH:
                    raise IsolatedSkillExecutorError(
                        "snapshot_limit_exceeded", f"Snapshot is too deeply nested: {safe_relative}"
                    )
                stack.append((Path(child.path), safe_relative, depth + 1))
                continue
            if not stat.S_ISREG(mode) or item_stat.st_nlink != 1:
                raise IsolatedSkillExecutorError(
                    "unsafe_snapshot_file",
                    f"Snapshot entries must be independent regular files: {safe_relative}",
                )
            if len(files) >= max_files:
                raise IsolatedSkillExecutorError(
                    "snapshot_limit_exceeded", f"{field} exceeds the {max_files}-file limit."
                )
            content = _read_snapshot_file(
                Path(child.path),
                display_path=safe_relative,
                max_file_bytes=max_file_bytes,
            )
            total += len(content)
            if total > max_total_bytes:
                raise IsolatedSkillExecutorError(
                    "snapshot_limit_exceeded", f"{field} exceeds its aggregate byte limit."
                )
            digest = hashlib.sha256(content).hexdigest()
            files.append({
                "path": safe_relative,
                "content_b64": base64.b64encode(content).decode("ascii"),
                "size_bytes": len(content),
                "sha256": digest,
            })

    files.sort(key=lambda item: item["path"])
    return files


def _validated_args(args: list[str] | None) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list) or len(args) > MAX_ARGS:
        raise IsolatedSkillExecutorError(
            "invalid_args", f"args must be an array with at most {MAX_ARGS} strings."
        )
    result: list[str] = []
    total = 0
    for index, item in enumerate(args):
        if not isinstance(item, str) or "\x00" in item or len(item) > MAX_ARG_CHARS:
            raise IsolatedSkillExecutorError(
                "invalid_args", f"args[{index}] is not a bounded NUL-free string."
            )
        try:
            total += len(item.encode("utf-8", errors="strict"))
        except UnicodeError as exc:
            raise IsolatedSkillExecutorError(
                "invalid_args", f"args[{index}] is not valid UTF-8."
            ) from exc
        if total > MAX_ARG_BYTES:
            raise IsolatedSkillExecutorError("invalid_args", "args exceeds its aggregate byte limit.")
        result.append(item)
    return result


def _validate_function_json(value: Any, *, field: str, depth: int = 0) -> None:
    if depth > MAX_FUNCTION_JSON_DEPTH:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            f"{field} exceeds JSON nesting depth {MAX_FUNCTION_JSON_DEPTH}.",
        )
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_function_json(item, field=field, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise IsolatedSkillExecutorError(
                    "invalid_function_call", f"{field} object keys must be strings."
                )
            _validate_function_json(item, field=field, depth=depth + 1)
        return
    raise IsolatedSkillExecutorError(
        "invalid_function_call",
        f"{field} contains a non-JSON value of type {type(value).__name__}.",
    )


def _validated_invocation(
    *,
    entrypoint: str,
    cli_args: list[str],
    function_name: str | None,
    function_args: list[Any] | None,
    function_kwargs: dict[str, Any] | None,
    class_name: str | None,
    method_name: str | None,
    constructor_args: list[Any] | None,
    constructor_kwargs: dict[str, Any] | None,
    method_args: list[Any] | None,
    method_kwargs: dict[str, Any] | None,
) -> dict[str, Any]:
    function_fields_present = any(
        value is not None for value in (function_name, function_args, function_kwargs)
    )
    instance_fields_present = any(
        value is not None
        for value in (
            class_name,
            method_name,
            constructor_args,
            constructor_kwargs,
            method_args,
            method_kwargs,
        )
    )
    if function_fields_present and instance_fields_present:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            "Top-level function and instance-method modes are mutually exclusive.",
        )
    if not function_fields_present and not instance_fields_present:
        return {"mode": "cli"}
    if PurePosixPath(entrypoint).suffix != ".py":
        raise IsolatedSkillExecutorError(
            "invalid_function_call", "Callable invocation requires a Python entrypoint."
        )
    if cli_args:
        raise IsolatedSkillExecutorError(
            "invalid_function_call", "Callable invocation cannot include CLI args."
        )

    if instance_fields_present:
        for field, value in (("class_name", class_name), ("method_name", method_name)):
            if (
                not isinstance(value, str)
                or value.startswith("_")
                or not PUBLIC_FUNCTION_RE.fullmatch(value)
            ):
                raise IsolatedSkillExecutorError(
                    "invalid_function_call",
                    f"{field} must be one public, non-dotted Python identifier.",
                )
        init_positional = [] if constructor_args is None else constructor_args
        init_keywords = {} if constructor_kwargs is None else constructor_kwargs
        call_positional = [] if method_args is None else method_args
        call_keywords = {} if method_kwargs is None else method_kwargs
        if (
            not isinstance(init_positional, list)
            or not isinstance(call_positional, list)
            or len(init_positional) + len(call_positional) > MAX_FUNCTION_ARGS
        ):
            raise IsolatedSkillExecutorError(
                "invalid_function_call",
                "Combined constructor_args and method_args must be arrays with at "
                f"most {MAX_FUNCTION_ARGS} items.",
            )
        if (
            not isinstance(init_keywords, dict)
            or not isinstance(call_keywords, dict)
            or len(init_keywords) + len(call_keywords) > MAX_FUNCTION_KWARGS
        ):
            raise IsolatedSkillExecutorError(
                "invalid_function_call",
                "Combined constructor_kwargs and method_kwargs must be objects "
                f"with at most {MAX_FUNCTION_KWARGS} keys.",
            )
        for field, keywords in (
            ("constructor_kwargs", init_keywords),
            ("method_kwargs", call_keywords),
        ):
            for key in keywords:
                if (
                    not isinstance(key, str)
                    or key.startswith("_")
                    or not PUBLIC_FUNCTION_RE.fullmatch(key)
                ):
                    raise IsolatedSkillExecutorError(
                        "invalid_function_call",
                        f"Invalid public keyword argument in {field}: {key!r}.",
                    )
        invocation = {
            "mode": "instance_method",
            "class_name": class_name,
            "method_name": method_name,
            "constructor_args": init_positional,
            "constructor_kwargs": init_keywords,
            "method_args": call_positional,
            "method_kwargs": call_keywords,
        }
        for field in (
            "constructor_args",
            "constructor_kwargs",
            "method_args",
            "method_kwargs",
        ):
            _validate_function_json(invocation[field], field=field)
        try:
            encoded = json.dumps(
                {key: invocation[key] for key in invocation if key != "mode"},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise IsolatedSkillExecutorError(
                "invalid_function_call",
                "Instance-method arguments must be finite valid JSON.",
            ) from exc
        if len(encoded) > MAX_FUNCTION_INPUT_BYTES:
            raise IsolatedSkillExecutorError(
                "invalid_function_call",
                "Instance-method argument JSON exceeds "
                f"{MAX_FUNCTION_INPUT_BYTES} bytes.",
            )
        return invocation

    if function_name is None:
        raise IsolatedSkillExecutorError(
            "invalid_function_call", "function_args/function_kwargs require function_name."
        )
    if (
        not isinstance(function_name, str)
        or function_name.startswith("_")
        or not PUBLIC_FUNCTION_RE.fullmatch(function_name)
    ):
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            "Function name must be one public, non-dotted Python identifier.",
        )
    positional = [] if function_args is None else function_args
    keywords = {} if function_kwargs is None else function_kwargs
    if not isinstance(positional, list) or len(positional) > MAX_FUNCTION_ARGS:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            f"function_args must be an array with at most {MAX_FUNCTION_ARGS} items.",
        )
    if not isinstance(keywords, dict) or len(keywords) > MAX_FUNCTION_KWARGS:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            f"function_kwargs must be an object with at most {MAX_FUNCTION_KWARGS} items.",
        )
    for key in keywords:
        if (
            not isinstance(key, str)
            or key.startswith("_")
            or not PUBLIC_FUNCTION_RE.fullmatch(key)
        ):
            raise IsolatedSkillExecutorError(
                "invalid_function_call", f"Invalid public keyword argument name: {key!r}."
            )
    _validate_function_json(positional, field="function_args")
    _validate_function_json(keywords, field="function_kwargs")
    invocation = {
        "mode": "function",
        "name": function_name,
        "args": positional,
        "kwargs": keywords,
    }
    try:
        encoded = json.dumps(
            {"args": positional, "kwargs": keywords},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IsolatedSkillExecutorError(
            "invalid_function_call", "Function arguments must be finite valid JSON."
        ) from exc
    if len(encoded) > MAX_FUNCTION_INPUT_BYTES:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            f"Function argument JSON exceeds {MAX_FUNCTION_INPUT_BYTES} bytes.",
        )
    return invocation


def _canonical_snapshot_digest(snapshot: list[dict[str, Any]]) -> str:
    manifest = [
        {
            "path": item["path"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in sorted(snapshot, key=lambda item: item["path"])
    ]
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_protocol_files(
    snapshot: SkillPackageSnapshot,
) -> list[dict[str, Any]]:
    """Validate and serialize one immutable Skill package capability."""

    if not isinstance(snapshot, SkillPackageSnapshot):
        raise IsolatedSkillExecutorError(
            "invalid_skill_snapshot",
            "A trusted immutable SkillPackageSnapshot is required.",
        )
    if not snapshot.files or len(snapshot.files) > MAX_SKILL_FILES:
        raise IsolatedSkillExecutorError(
            "invalid_skill_snapshot",
            "The immutable Skill snapshot has an invalid file count.",
        )
    records: list[dict[str, Any]] = []
    total_bytes = 0
    previous_path = ""
    for item in snapshot.files:
        if not isinstance(item, _SkillSnapshotFile):
            raise IsolatedSkillExecutorError(
                "invalid_skill_snapshot",
                "The immutable Skill snapshot contains an invalid file record.",
            )
        safe_path = _safe_relative_path(item.path, field="skill snapshot path")
        if safe_path <= previous_path:
            raise IsolatedSkillExecutorError(
                "invalid_skill_snapshot",
                "Skill snapshot paths must be unique and canonically sorted.",
            )
        previous_path = safe_path
        if (
            not isinstance(item.content, bytes)
            or len(item.content) > MAX_SKILL_FILE_BYTES
        ):
            raise IsolatedSkillExecutorError(
                "invalid_skill_snapshot",
                f"Skill snapshot file exceeds its bound: {safe_path}",
            )
        digest = hashlib.sha256(item.content).hexdigest()
        if not hmac.compare_digest(digest, str(item.sha256 or "")):
            raise IsolatedSkillExecutorError(
                "invalid_skill_snapshot",
                f"Skill snapshot content digest is invalid: {safe_path}",
            )
        total_bytes += len(item.content)
        if total_bytes > MAX_SKILL_TOTAL_BYTES:
            raise IsolatedSkillExecutorError(
                "invalid_skill_snapshot",
                "The immutable Skill snapshot exceeds its aggregate byte bound.",
            )
        records.append({
            "path": safe_path,
            "content_b64": base64.b64encode(item.content).decode("ascii"),
            "size_bytes": len(item.content),
            "sha256": digest,
        })
    actual_digest = _canonical_snapshot_digest(records)
    if not hmac.compare_digest(actual_digest, str(snapshot.sha256 or "")):
        raise IsolatedSkillExecutorError(
            "invalid_skill_snapshot",
            "The immutable Skill snapshot package digest is invalid.",
        )
    if "SKILL.md" not in {item["path"] for item in records}:
        raise IsolatedSkillExecutorError(
            "invalid_skill_snapshot",
            "The verified Skill root must contain SKILL.md.",
        )
    return records


def snapshot_skill_package(skill_root: Path) -> SkillPackageSnapshot:
    """Capture one bounded package exactly once for routing and execution."""

    records = _snapshot_tree(
        Path(skill_root),
        field="skill_root",
        max_files=MAX_SKILL_FILES,
        max_file_bytes=MAX_SKILL_FILE_BYTES,
        max_total_bytes=MAX_SKILL_TOTAL_BYTES,
    )
    if "SKILL.md" not in {item["path"] for item in records}:
        raise IsolatedSkillExecutorError(
            "invalid_skill_snapshot",
            "The verified Skill root must contain SKILL.md.",
        )
    files = tuple(
        _SkillSnapshotFile(
            path=str(item["path"]),
            content=base64.b64decode(
                str(item["content_b64"]).encode("ascii"),
                validate=True,
            ),
            sha256=str(item["sha256"]),
        )
        for item in records
    )
    snapshot = SkillPackageSnapshot(
        files=files,
        sha256=_canonical_snapshot_digest(records),
    )
    # Keep one validation boundary even though the source records were built
    # locally.  This makes future callers unable to rely on unchecked fields.
    _snapshot_protocol_files(snapshot)
    return snapshot


def compute_skill_package_digest(skill_root: Path) -> str:
    """Return the canonical digest used to authorize an exact Skill package.

    This public helper deliberately uses the same safe tree walk, limits, and
    manifest encoding as process-lease creation.  Callers can therefore bind
    package authority before opening a lease without maintaining a second,
    subtly different digest implementation.
    """

    return snapshot_skill_package(Path(skill_root)).sha256


def _process_auth_key() -> bytes:
    token = os.environ.get("EXECUTOR_V2_AUTH_TOKEN", "")
    try:
        encoded = token.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise IsolatedSkillExecutorError(
            "v2_auth_unavailable",
            "Persistent process protocol authentication is not configured safely.",
        ) from exc
    if len(encoded) < 32 or len(encoded) > 4_096:
        raise IsolatedSkillExecutorError(
            "v2_auth_unavailable",
            "Persistent process protocol authentication is not configured safely.",
        )
    return encoded


def _encode_process_request(payload: dict[str, Any]) -> bytes:
    unsigned = dict(payload)
    unsigned.pop("auth_hmac", None)
    try:
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IsolatedSkillExecutorError(
            "invalid_process_request",
            "Process request must contain bounded valid JSON.",
        ) from exc
    payload["auth_hmac"] = hmac.new(
        _process_auth_key(),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        raise IsolatedSkillExecutorError(
            "request_limit_exceeded",
            "Encoded process executor request exceeds its protocol limit.",
        )
    return encoded


def _validated_persistent_open_invocation(
    *,
    entrypoint: str,
    cli_args: list[str],
    class_name: str | None,
    factory_name: str | None,
    constructor_args: list[Any] | None,
    constructor_kwargs: dict[str, Any] | None,
) -> dict[str, Any]:
    fields_present = any(
        value is not None
        for value in (class_name, factory_name, constructor_args, constructor_kwargs)
    )
    if not fields_present:
        return {"mode": "cli"}
    if (class_name is None) == (factory_name is None):
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            "Exactly one of class_name or factory_name is required for a persistent object.",
        )
    invocation_mode = "instance" if class_name is not None else "factory"
    selected_name = class_name if class_name is not None else factory_name
    if (
        PurePosixPath(entrypoint).suffix != ".py"
        or cli_args
        or not isinstance(selected_name, str)
        or selected_name.startswith("_")
        or PUBLIC_FUNCTION_RE.fullmatch(selected_name) is None
    ):
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            "Persistent object mode requires one public class/factory in a Python entrypoint and no CLI args.",
        )
    positional = [] if constructor_args is None else constructor_args
    keywords = {} if constructor_kwargs is None else constructor_kwargs
    if not isinstance(positional, list) or len(positional) > MAX_FUNCTION_ARGS:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            f"constructor_args must contain at most {MAX_FUNCTION_ARGS} JSON values.",
        )
    if not isinstance(keywords, dict) or len(keywords) > MAX_FUNCTION_KWARGS:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            f"constructor_kwargs must contain at most {MAX_FUNCTION_KWARGS} JSON values.",
        )
    for key in keywords:
        if (
            not isinstance(key, str)
            or key.startswith("_")
            or PUBLIC_FUNCTION_RE.fullmatch(key) is None
        ):
            raise IsolatedSkillExecutorError(
                "invalid_function_call",
                "constructor_kwargs keys must be public Python identifiers.",
            )
    _validate_function_json(positional, field="constructor_args")
    _validate_function_json(keywords, field="constructor_kwargs")
    try:
        encoded = json.dumps(
            {"args": positional, "kwargs": keywords},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            "Constructor arguments must be finite valid JSON.",
        ) from exc
    if len(encoded) > MAX_FUNCTION_INPUT_BYTES:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            "Constructor argument JSON exceeds its byte limit.",
        )
    if invocation_mode == "instance":
        return {
            "mode": "instance",
            "class_name": selected_name,
            "constructor_args": positional,
            "constructor_kwargs": keywords,
        }
    return {
        "mode": "factory",
        "factory_name": selected_name,
        "factory_args": positional,
        "factory_kwargs": keywords,
    }


def build_skill_script_request(
    *,
    skill_root: Path,
    workspace: Path,
    entrypoint: str,
    args: list[str] | None = None,
    timeout: int = 120,
    cwd: str = "workspace",
    function_name: str | None = None,
    function_args: list[Any] | None = None,
    function_kwargs: dict[str, Any] | None = None,
    class_name: str | None = None,
    method_name: str | None = None,
    constructor_args: list[Any] | None = None,
    constructor_kwargs: dict[str, Any] | None = None,
    method_args: list[Any] | None = None,
    method_kwargs: dict[str, Any] | None = None,
    expected_skill_sha256: str | None = None,
    egress_origins: tuple[str, ...] | list[str] | None = None,
    egress_rules: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    private_origins: tuple[str, ...] | list[str] | None = None,
    budget_scope_sha256: str | None = None,
    call_id_sha256: str | None = None,
    request_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Build one bounded, content-addressed protocol request."""

    safe_entrypoint = _safe_relative_path(entrypoint, field="entrypoint")
    if PurePosixPath(safe_entrypoint).suffix not in SUPPORTED_EXTENSIONS:
        raise IsolatedSkillExecutorError(
            "unsupported_script_type",
            "entrypoint must end in .py, .sh, .bash, .js, .mjs, or .cjs.",
        )
    safe_args = _validated_args(args)
    invocation = _validated_invocation(
        entrypoint=safe_entrypoint,
        cli_args=safe_args,
        function_name=function_name,
        function_args=function_args,
        function_kwargs=function_kwargs,
        class_name=class_name,
        method_name=method_name,
        constructor_args=constructor_args,
        constructor_kwargs=constructor_kwargs,
        method_args=method_args,
        method_kwargs=method_kwargs,
    )
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_SKILL_TIMEOUT:
        raise IsolatedSkillExecutorError(
            "invalid_timeout", f"timeout must be between 1 and {MAX_SKILL_TIMEOUT} seconds."
        )
    if cwd not in {"workspace", "script", "skill"}:
        raise IsolatedSkillExecutorError(
            "invalid_cwd", "cwd must be 'workspace', 'script', or 'skill'."
        )
    if request_id is None:
        safe_request_id = str(uuid.uuid4())
    else:
        try:
            safe_request_id = str(uuid.UUID(request_id))
        except (ValueError, AttributeError) as exc:
            raise IsolatedSkillExecutorError(
                "invalid_request_id", "request_id must be a UUID string."
            ) from exc

    skill_files = _snapshot_tree(
        Path(skill_root),
        field="skill_root",
        max_files=MAX_SKILL_FILES,
        max_file_bytes=MAX_SKILL_FILE_BYTES,
        max_total_bytes=MAX_SKILL_TOTAL_BYTES,
    )
    skill_paths = {item["path"] for item in skill_files}
    if "SKILL.md" not in skill_paths:
        raise IsolatedSkillExecutorError(
            "invalid_skill_snapshot", "The verified Skill root must contain SKILL.md."
        )
    if safe_entrypoint not in skill_paths:
        raise IsolatedSkillExecutorError(
            "missing_entrypoint", "entrypoint is not a regular file in the verified Skill root."
        )
    if expected_skill_sha256 is not None:
        if (
            not isinstance(expected_skill_sha256, str)
            or len(expected_skill_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_skill_sha256
            )
        ):
            raise IsolatedSkillExecutorError(
                "invalid_authority",
                "expected_skill_sha256 must be a lowercase SHA-256 digest.",
            )
        actual_skill_sha256 = _canonical_snapshot_digest(skill_files)
        if not hmac.compare_digest(actual_skill_sha256, expected_skill_sha256):
            raise IsolatedSkillExecutorError(
                "authority_digest_mismatch",
                "The execution snapshot no longer matches authorized Skill package bytes.",
            )
    workspace_files = _snapshot_tree(
        Path(workspace),
        field="workspace",
        max_files=MAX_WORKSPACE_FILES,
        max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
        max_total_bytes=MAX_WORKSPACE_TOTAL_BYTES,
        skip_dirs=WORKSPACE_SKIP_DIRS,
    )
    (
        safe_egress_rules,
        safe_egress_origins,
        safe_private_origins,
    ) = _validated_exact_egress_policy(
        egress_rules=egress_rules,
        private_origins=private_origins,
        legacy_origins=egress_origins,
    )
    egress_policy_version, egress_budget_binding = (
        _validated_egress_budget_binding(
            has_rules=bool(safe_egress_rules),
            budget_scope_sha256=budget_scope_sha256,
            call_id_sha256=call_id_sha256,
        )
    )
    payload: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "skill_script",
        "request_id": safe_request_id,
        "entrypoint": safe_entrypoint,
        "argv": safe_args,
        "invocation": invocation,
        "timeout": timeout,
        "cwd": cwd,
        "skill_files": skill_files,
        "workspace_files": workspace_files,
        "egress_origins": safe_egress_origins,
        "egress_policy_version": egress_policy_version,
        "egress_rules": safe_egress_rules,
        "private_origins": safe_private_origins,
        **egress_budget_binding,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        raise IsolatedSkillExecutorError(
            "request_limit_exceeded", "Encoded Skill executor request exceeds its protocol limit."
        )
    return payload, encoded


def build_process_lease_open_request(
    *,
    owner_scope: ProcessOwnerScope,
    skill_root: Path,
    workspace: Path,
    entrypoint: str,
    args: list[str] | None = None,
    cwd: str = "workspace",
    idle_ttl_seconds: int = 300,
    max_runtime_seconds: int = MAX_PROCESS_RUNTIME_SECONDS,
    class_name: str | None = None,
    factory_name: str | None = None,
    constructor_args: list[Any] | None = None,
    constructor_kwargs: dict[str, Any] | None = None,
    egress_origins: tuple[str, ...] | list[str] | None = None,
    egress_rules: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    private_origins: tuple[str, ...] | list[str] | None = None,
    budget_scope_sha256: str | None = None,
    call_id_sha256: str | None = None,
    request_id: str | None = None,
    op_id: str | None = None,
    skill_snapshot: SkillPackageSnapshot | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Build a signed v2 lease-open request from trusted runtime state."""

    if not isinstance(owner_scope, ProcessOwnerScope):
        raise IsolatedSkillExecutorError(
            "invalid_owner_scope",
            "owner_scope must be created from trusted runtime context.",
        )
    safe_entrypoint = _safe_relative_path(entrypoint, field="entrypoint")
    if PurePosixPath(safe_entrypoint).suffix not in SUPPORTED_EXTENSIONS:
        raise IsolatedSkillExecutorError(
            "unsupported_script_type",
            "entrypoint must end in .py, .sh, .bash, .js, .mjs, or .cjs.",
        )
    safe_args = _validated_args(args)
    invocation = _validated_persistent_open_invocation(
        entrypoint=safe_entrypoint,
        cli_args=safe_args,
        class_name=class_name,
        factory_name=factory_name,
        constructor_args=constructor_args,
        constructor_kwargs=constructor_kwargs,
    )
    if cwd not in {"workspace", "script", "skill"}:
        raise IsolatedSkillExecutorError(
            "invalid_cwd",
            "cwd must be exactly 'workspace', 'script', or 'skill'.",
        )
    for field_name, value, maximum in (
        ("idle_ttl_seconds", idle_ttl_seconds, MAX_PROCESS_LEASE_TTL_SECONDS),
        ("max_runtime_seconds", max_runtime_seconds, MAX_PROCESS_RUNTIME_SECONDS),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise IsolatedSkillExecutorError(
                "invalid_process_quota",
                f"{field_name} must be between 1 and {maximum}.",
            )
    try:
        safe_request_id = str(uuid.UUID(request_id)) if request_id is not None else str(uuid.uuid4())
        safe_op_id = str(uuid.UUID(op_id)) if op_id is not None else str(uuid.uuid4())
    except (ValueError, AttributeError) as exc:
        raise IsolatedSkillExecutorError(
            "invalid_request_id",
            "request_id and op_id must be UUID strings.",
        ) from exc
    skill_files = (
        _snapshot_protocol_files(skill_snapshot)
        if skill_snapshot is not None
        else _snapshot_protocol_files(
            snapshot_skill_package(Path(skill_root))
        )
    )
    skill_paths = {item["path"] for item in skill_files}
    if "SKILL.md" not in skill_paths:
        raise IsolatedSkillExecutorError(
            "invalid_skill_snapshot",
            "The verified Skill root must contain SKILL.md.",
        )
    if safe_entrypoint not in skill_paths:
        raise IsolatedSkillExecutorError(
            "missing_entrypoint",
            "entrypoint is not a regular file in the verified Skill root.",
        )
    workspace_files = _snapshot_tree(
        Path(workspace),
        field="workspace",
        max_files=MAX_WORKSPACE_FILES,
        max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
        max_total_bytes=MAX_WORKSPACE_TOTAL_BYTES,
        skip_dirs=WORKSPACE_SKIP_DIRS,
    )
    (
        safe_egress_rules,
        safe_egress_origins,
        safe_private_origins,
    ) = _validated_exact_egress_policy(
        egress_rules=egress_rules,
        private_origins=private_origins,
        legacy_origins=egress_origins,
    )
    egress_policy_version, egress_budget_binding = (
        _validated_egress_budget_binding(
            has_rules=bool(safe_egress_rules),
            budget_scope_sha256=budget_scope_sha256,
            call_id_sha256=call_id_sha256,
        )
    )
    entrypoint_record = next(
        item for item in skill_files if item["path"] == safe_entrypoint
    )
    payload: dict[str, Any] = {
        "protocol_version": PROCESS_PROTOCOL_VERSION,
        "kind": "process_lease",
        "operation": "open",
        "request_id": safe_request_id,
        "op_id": safe_op_id,
        "owner_scope": owner_scope._protocol_value(),
        "entrypoint": safe_entrypoint,
        "argv": safe_args,
        "invocation": invocation,
        "cwd": cwd,
        "idle_ttl_seconds": idle_ttl_seconds,
        "max_runtime_seconds": max_runtime_seconds,
        "skill_sha256": _canonical_snapshot_digest(skill_files),
        "script_sha256": entrypoint_record["sha256"],
        "skill_files": skill_files,
        "workspace_files": workspace_files,
        "egress_origins": safe_egress_origins,
        "egress_policy_version": egress_policy_version,
        "egress_rules": safe_egress_rules,
        "private_origins": safe_private_origins,
        **egress_budget_binding,
    }
    return payload, _encode_process_request(payload)


def _build_process_operation_request(
    lease: IsolatedProcessLease,
    operation: str,
    *,
    request_id: str | None = None,
    op_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bytes]:
    if not isinstance(lease, IsolatedProcessLease):
        raise IsolatedSkillExecutorError(
            "invalid_lease",
            "A trusted IsolatedProcessLease capability is required.",
        )
    if lease.closed and operation != "close":
        raise IsolatedSkillExecutorError("lease_closed", "The process lease is closed.")
    try:
        safe_request_id = str(uuid.UUID(request_id)) if request_id is not None else str(uuid.uuid4())
        safe_op_id = str(uuid.UUID(op_id)) if op_id is not None else str(uuid.uuid4())
    except (ValueError, AttributeError) as exc:
        raise IsolatedSkillExecutorError(
            "invalid_request_id",
            "request_id and op_id must be UUID strings.",
        ) from exc
    payload: dict[str, Any] = {
        "protocol_version": PROCESS_PROTOCOL_VERSION,
        "kind": "process_lease",
        "operation": operation,
        "request_id": safe_request_id,
        "op_id": safe_op_id,
        "owner_scope": lease._owner_scope._protocol_value(),
        "lease_handle": lease.handle,
        "skill_sha256": lease.skill_sha256,
        "script_sha256": lease.script_sha256,
    }
    if extra:
        overlap = set(payload).intersection(extra)
        if overlap:
            raise IsolatedSkillExecutorError(
                "invalid_process_request",
                f"Process operation attempted to replace bound fields: {sorted(overlap)!r}.",
            )
        payload.update(extra)
    return payload, _encode_process_request(payload)


def build_process_reap_request(
    *,
    request_id: str | None = None,
    op_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Build the authenticated, model-inaccessible startup reap request."""

    try:
        safe_request_id = (
            str(uuid.UUID(request_id))
            if request_id is not None
            else str(uuid.uuid4())
        )
        safe_op_id = (
            str(uuid.UUID(op_id))
            if op_id is not None
            else str(uuid.uuid4())
        )
    except (ValueError, AttributeError) as exc:
        raise IsolatedSkillExecutorError(
            "invalid_request_id",
            "request_id and op_id must be UUID strings.",
        ) from exc
    payload: dict[str, Any] = {
        "protocol_version": PROCESS_PROTOCOL_VERSION,
        "kind": "process_lease",
        "operation": "reap_all",
        "request_id": safe_request_id,
        "op_id": safe_op_id,
    }
    return payload, _encode_process_request(payload)


def build_declared_command_request(
    *,
    skill_root: Path,
    workspace: Path,
    executable: str,
    argv: list[str] | None = None,
    cwd: str = "workspace",
    timeout: int = 120,
    egress_origins: tuple[str, ...] | list[str] | None = None,
    egress_rules: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    private_origins: tuple[str, ...] | list[str] | None = None,
    budget_scope_sha256: str | None = None,
    call_id_sha256: str | None = None,
    request_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Build one no-shell command request from a trusted compiled grant."""
    if not isinstance(executable, str) or RUNTIME_COMMAND_RE.fullmatch(executable) is None:
        raise IsolatedSkillExecutorError(
            "invalid_executable",
            "executable must be one PATH command name without a slash.",
        )
    safe_argv = _validated_args(argv)
    if cwd not in {"workspace", "skill"}:
        raise IsolatedSkillExecutorError(
            "invalid_cwd", "cwd must be exactly 'workspace' or 'skill'."
        )
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_SKILL_TIMEOUT:
        raise IsolatedSkillExecutorError(
            "invalid_timeout", f"timeout must be between 1 and {MAX_SKILL_TIMEOUT} seconds."
        )
    if request_id is None:
        safe_request_id = str(uuid.uuid4())
    else:
        try:
            safe_request_id = str(uuid.UUID(request_id))
        except (ValueError, AttributeError) as exc:
            raise IsolatedSkillExecutorError(
                "invalid_request_id", "request_id must be a UUID string."
            ) from exc
    skill_files = _snapshot_tree(
        Path(skill_root),
        field="skill_root",
        max_files=MAX_SKILL_FILES,
        max_file_bytes=MAX_SKILL_FILE_BYTES,
        max_total_bytes=MAX_SKILL_TOTAL_BYTES,
    )
    if "SKILL.md" not in {item["path"] for item in skill_files}:
        raise IsolatedSkillExecutorError(
            "invalid_skill_snapshot", "The verified Skill root must contain SKILL.md."
        )
    workspace_files = _snapshot_tree(
        Path(workspace),
        field="workspace",
        max_files=MAX_WORKSPACE_FILES,
        max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
        max_total_bytes=MAX_WORKSPACE_TOTAL_BYTES,
        skip_dirs=WORKSPACE_SKIP_DIRS,
    )
    (
        safe_egress_rules,
        safe_egress_origins,
        safe_private_origins,
    ) = _validated_exact_egress_policy(
        egress_rules=egress_rules,
        private_origins=private_origins,
        legacy_origins=egress_origins,
    )
    egress_policy_version, egress_budget_binding = (
        _validated_egress_budget_binding(
            has_rules=bool(safe_egress_rules),
            budget_scope_sha256=budget_scope_sha256,
            call_id_sha256=call_id_sha256,
        )
    )
    payload: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "declared_command",
        "request_id": safe_request_id,
        "executable": executable,
        "argv": safe_argv,
        "timeout": timeout,
        "cwd": cwd,
        "skill_files": skill_files,
        "workspace_files": workspace_files,
        "egress_origins": safe_egress_origins,
        "egress_policy_version": egress_policy_version,
        "egress_rules": safe_egress_rules,
        "private_origins": safe_private_origins,
        **egress_budget_binding,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        raise IsolatedSkillExecutorError(
            "request_limit_exceeded",
            "Encoded declared-command request exceeds its protocol limit.",
        )
    return payload, encoded


def build_session_code_request(
    *,
    workspace: Path,
    code: str,
    timeout: int = 30,
    skills_root: Path | None = None,
    request_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Build a bounded request for model-authored Python with session files.

    Unlike the legacy source-only request, this protocol carries a disposable
    workspace snapshot and (only when requested by the caller) an immutable
    session-Skill snapshot.  No host path is mounted into the executor.
    """

    if not isinstance(code, str) or not code.strip():
        raise IsolatedSkillExecutorError("invalid_code", "code must be a non-empty string.")
    try:
        code_bytes = code.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise IsolatedSkillExecutorError("invalid_code", "code must be valid UTF-8.") from exc
    if len(code_bytes) > MAX_CODE_BYTES:
        raise IsolatedSkillExecutorError(
            "code_limit_exceeded", f"code exceeds the {MAX_CODE_BYTES}-byte limit."
        )
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_SKILL_TIMEOUT:
        raise IsolatedSkillExecutorError(
            "invalid_timeout", f"timeout must be between 1 and {MAX_SKILL_TIMEOUT} seconds."
        )
    if request_id is None:
        safe_request_id = str(uuid.uuid4())
    else:
        try:
            safe_request_id = str(uuid.UUID(request_id))
        except (ValueError, AttributeError) as exc:
            raise IsolatedSkillExecutorError(
                "invalid_request_id", "request_id must be a UUID string."
            ) from exc

    workspace_files = _snapshot_tree(
        Path(workspace),
        field="workspace",
        max_files=MAX_WORKSPACE_FILES,
        max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
        max_total_bytes=MAX_WORKSPACE_TOTAL_BYTES,
        skip_dirs=WORKSPACE_SKIP_DIRS,
    )
    skill_files: list[dict[str, Any]] = []
    if skills_root is not None:
        skill_files = _snapshot_tree(
            Path(skills_root),
            field="skills_root",
            max_files=MAX_SKILL_FILES,
            max_file_bytes=MAX_SKILL_FILE_BYTES,
            max_total_bytes=MAX_SKILL_TOTAL_BYTES,
        )

    payload: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "session_code",
        "request_id": safe_request_id,
        "code": code,
        "timeout": timeout,
        "skill_files": skill_files,
        "workspace_files": workspace_files,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        raise IsolatedSkillExecutorError(
            "request_limit_exceeded", "Encoded session-code request exceeds its protocol limit."
        )
    return payload, encoded


def _validated_runtime_declarations(
    value: list[str] | tuple[str, ...] | None,
    *,
    field: str,
    limit: int,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise IsolatedSkillExecutorError(
            "invalid_runtime_capability_request",
            f"{field} must contain at most {limit} declaration strings.",
        )
    result: list[str] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, str)
            or not item
            or "\x00" in item
            or "\r" in item
            or "\n" in item
            or len(item) > MAX_RUNTIME_DECLARATION_CHARS
            or (pattern is not None and pattern.fullmatch(item) is None)
        ):
            raise IsolatedSkillExecutorError(
                "invalid_runtime_capability_request",
                f"{field}[{index}] is not a supported bounded declaration.",
            )
        result.append(item)
    if len(set(result)) != len(result):
        raise IsolatedSkillExecutorError(
            "invalid_runtime_capability_request",
            f"{field} contains duplicate declarations.",
        )
    return result


def build_runtime_capabilities_request(
    *,
    requirements: list[str] | tuple[str, ...] | None = None,
    commands: list[str] | tuple[str, ...] | None = None,
    environment_variables: list[str] | tuple[str, ...] | None = None,
    platform_groups: list[list[str]] | tuple[tuple[str, ...], ...] | None = None,
    request_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Build a declaration-only probe for the exact isolated runtime image."""

    safe_requirements = _validated_runtime_declarations(
        requirements,
        field="requirements",
        limit=MAX_RUNTIME_REQUIREMENTS,
    )
    safe_commands = _validated_runtime_declarations(
        commands,
        field="commands",
        limit=MAX_RUNTIME_COMMANDS,
        pattern=RUNTIME_COMMAND_RE,
    )
    safe_environment = _validated_runtime_declarations(
        environment_variables,
        field="environment_variables",
        limit=MAX_RUNTIME_ENVIRONMENT_VARIABLES,
        pattern=RUNTIME_ENVIRONMENT_RE,
    )
    raw_groups = [] if platform_groups is None else platform_groups
    if not isinstance(raw_groups, (list, tuple)) or len(raw_groups) > MAX_RUNTIME_PLATFORM_GROUPS:
        raise IsolatedSkillExecutorError(
            "invalid_runtime_capability_request",
            "platform_groups exceeds its bounded array policy.",
        )
    safe_platform_groups = [
        _validated_runtime_declarations(
            group,
            field=f"platform_groups[{index}]",
            limit=MAX_RUNTIME_PLATFORMS_PER_GROUP,
            pattern=RUNTIME_PLATFORM_RE,
        )
        for index, group in enumerate(raw_groups)
    ]
    if request_id is None:
        safe_request_id = str(uuid.uuid4())
    else:
        try:
            safe_request_id = str(uuid.UUID(request_id))
        except (ValueError, AttributeError) as exc:
            raise IsolatedSkillExecutorError(
                "invalid_request_id", "request_id must be a UUID string."
            ) from exc
    payload: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "runtime_capabilities",
        "request_id": safe_request_id,
        "requirements": safe_requirements,
        "commands": safe_commands,
        "environment_variables": safe_environment,
        "platform_groups": safe_platform_groups,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        raise IsolatedSkillExecutorError(
            "request_limit_exceeded",
            "Encoded runtime-capability request exceeds its protocol limit.",
        )
    return payload, encoded


def _decode_artifacts(response: dict[str, Any]) -> list[tuple[str, bytes, dict[str, Any]]]:
    value = response.get("artifacts", [])
    if not isinstance(value, list) or len(value) > MAX_OUTPUT_FILES:
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor artifacts exceed the bounded array policy."
        )
    decoded: list[tuple[str, bytes, dict[str, Any]]] = []
    seen: set[str] = set()
    total = 0
    required_fields = {"path", "change", "content_b64", "size_bytes", "sha256"}
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != required_fields:
            raise IsolatedSkillExecutorError(
                "invalid_response", f"Executor artifact {index} has an invalid shape."
            )
        relative = _safe_relative_path(item.get("path"), field=f"artifacts[{index}].path")
        if PurePosixPath(relative).parts[0] in WORKSPACE_SKIP_DIRS:
            raise IsolatedSkillExecutorError(
                "unsafe_workspace_path",
                f"Executor returned an artifact under a reserved workspace path: {relative}",
            )
        if relative in seen:
            raise IsolatedSkillExecutorError(
                "invalid_response", f"Executor returned duplicate artifact path: {relative}"
            )
        seen.add(relative)
        if item.get("change") not in {"created", "modified"}:
            raise IsolatedSkillExecutorError(
                "invalid_response", f"Executor artifact has an invalid change type: {relative}"
            )
        size = item.get("size_bytes")
        digest = item.get("sha256")
        content_b64 = item.get("content_b64")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= MAX_OUTPUT_FILE_BYTES
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(content_b64, str)
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response", f"Executor artifact metadata is invalid: {relative}"
            )
        try:
            content = base64.b64decode(content_b64.encode("ascii"), validate=True)
        except (UnicodeError, binascii.Error, ValueError) as exc:
            raise IsolatedSkillExecutorError(
                "invalid_response", f"Executor artifact base64 is invalid: {relative}"
            ) from exc
        actual_digest = hashlib.sha256(content).hexdigest()
        if len(content) != size or not hmac.compare_digest(actual_digest, digest):
            raise IsolatedSkillExecutorError(
                "artifact_integrity_error", f"Executor artifact integrity check failed: {relative}"
            )
        total += size
        if total > MAX_OUTPUT_TOTAL_BYTES:
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor artifacts exceed the aggregate byte limit."
            )
        decoded.append((relative, content, item))
    return decoded


_EGRESS_AUDIT_FIELDS = {
    "profile",
    "version",
    "budget_scope_sha256",
    "call_id_sha256",
    "rules_sha256",
    "counts",
    "limits",
    "exhausted",
    "receipt_sha256",
}
_EGRESS_AUDIT_COUNT_FIELDS = {
    "accepted_connections",
    "client_to_proxy_wire_bytes",
    "proxy_to_client_wire_bytes",
    "budget_rejections",
    "clean_closes",
}
_EGRESS_AUDIT_LIMIT_FIELDS = {
    "max_outbound_bytes",
    "max_requests",
    "max_response_wire_bytes",
}


def _canonical_json_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(encoded).hexdigest()


def _egress_authority_sha256(request: dict[str, Any]) -> str:
    return _canonical_json_sha256({
        "origins": list(request.get("egress_origins") or []),
        "egress_rules": list(request.get("egress_rules") or []),
        "private_origins": list(request.get("private_origins") or []),
    })


def _validate_egress_audit_receipt(
    receipt: Any,
    *,
    expected_budget_scope_sha256: str,
    expected_call_id_sha256: str,
    expected_rules_sha256: str,
) -> dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != _EGRESS_AUDIT_FIELDS:
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor bounded-egress audit receipt is malformed.",
        )
    counts = receipt.get("counts")
    limits = receipt.get("limits")
    claimed_sha256 = receipt.get("receipt_sha256")
    if (
        receipt.get("profile") != "bounded_controlled_exchange"
        or receipt.get("version") != 1
        or isinstance(receipt.get("version"), bool)
        or receipt.get("budget_scope_sha256")
        != expected_budget_scope_sha256
        or receipt.get("call_id_sha256") != expected_call_id_sha256
        or receipt.get("rules_sha256") != expected_rules_sha256
        or not isinstance(receipt.get("exhausted"), bool)
        or not isinstance(counts, dict)
        or set(counts) != _EGRESS_AUDIT_COUNT_FIELDS
        or not isinstance(limits, dict)
        or set(limits) != _EGRESS_AUDIT_LIMIT_FIELDS
        or not isinstance(claimed_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", claimed_sha256) is None
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in counts.values()
        )
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            for value in limits.values()
        )
        or counts["accepted_connections"] > limits["max_requests"]
        or counts["client_to_proxy_wire_bytes"]
        > limits["max_outbound_bytes"]
        or counts["proxy_to_client_wire_bytes"]
        > limits["max_response_wire_bytes"]
        or counts["clean_closes"] > counts["accepted_connections"]
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor bounded-egress audit receipt is inconsistent.",
        )
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256")
    if not hmac.compare_digest(
        claimed_sha256,
        _canonical_json_sha256(unsigned),
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor bounded-egress audit receipt digest is invalid.",
        )
    return dict(receipt)


def _validate_terminal_egress_response(
    response: dict[str, Any],
    *,
    policy_version: int,
    budget_scope_sha256: str | None,
    call_id_sha256: str | None,
    authority_sha256: str | None,
    require_audit: bool,
) -> None:
    observed_version = response.get("egress_policy_version")
    if (
        observed_version != policy_version
        and not (
            policy_version == 2
            and observed_version is None
        )
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor egress policy version does not match the request.",
        )
    receipt = response.get("egress_audit_receipt")
    if policy_version == 2:
        if receipt is not None:
            raise IsolatedSkillExecutorError(
                "invalid_response",
                "Legacy executor response unexpectedly carried a v3 audit.",
            )
        return
    if (
        budget_scope_sha256 is None
        or call_id_sha256 is None
        or authority_sha256 is None
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Harness lost the bounded-egress audit binding.",
        )
    if receipt is None and not require_audit:
        return
    _validate_egress_audit_receipt(
        receipt,
        expected_budget_scope_sha256=budget_scope_sha256,
        expected_call_id_sha256=call_id_sha256,
        expected_rules_sha256=authority_sha256,
    )


def _one_shot_execution_requires_egress_audit(
    response: dict[str, Any],
) -> bool:
    """Validate execution evidence and identify post-spawn responses.

    A successful or timed-out one-shot invocation necessarily crossed the
    process-spawn boundary and therefore must carry an exact integer exit
    status. Error responses may be emitted before spawn, but once they carry a
    return code they are likewise post-spawn. Keeping this decision independent
    of the presence of the audit receipt prevents a corrupt response from
    deleting both ``returncode`` and the receipt to bypass v3 validation.
    """

    status = response.get("status")
    has_returncode = "returncode" in response
    if has_returncode and (
        isinstance(response.get("returncode"), bool)
        or not isinstance(response.get("returncode"), int)
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor one-shot return code is malformed.",
        )
    if status in {"success", "timeout"} and not has_returncode:
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor one-shot terminal response lacks execution evidence.",
        )
    return status in {"success", "timeout"} or has_returncode


def validate_process_lease_response(
    response: Any,
    *,
    request: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, bytes, dict[str, Any]]]]:
    if not isinstance(response, dict):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor process response must be an object.",
        )
    operation = request.get("operation")
    if (
        response.get("protocol_version") != PROCESS_PROTOCOL_VERSION
        or response.get("kind") != "process_lease_result"
        or response.get("operation") != operation
        or response.get("request_id") != request.get("request_id")
        or response.get("network") != "disabled"
        or "auth_hmac" in response
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor process protocol, identity, or isolation policy does not match.",
        )
    status = response.get("status")
    if status not in {"success", "error"}:
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor process response status is invalid.",
        )
    if status == "error":
        if (
            not isinstance(response.get("error_code"), str)
            or not isinstance(response.get("error"), str)
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response",
                "Executor process error is malformed.",
            )
        terminal_state = response.get("terminal_lease_state")
        if terminal_state is not None:
            error_code = response.get("error_code")
            artifact_terminal = bool(
                operation == "close"
                and (
                    (
                        terminal_state == "closed"
                        and error_code
                        == "close_artifact_collection_failed"
                    )
                    or (
                        terminal_state == "quarantined"
                        and error_code == "worker_containment_failed"
                    )
                )
                and response.get("artifacts_discarded") is True
                and isinstance(
                    response.get("artifact_error_code"),
                    str,
                )
                and bool(response.get("artifact_error_code"))
                and len(response["artifact_error_code"]) <= 160
            )
            lifecycle_terminal = bool(
                (
                    terminal_state == "closed"
                    and error_code in {
                        "lease_expired",
                        "lease_closed",
                    }
                )
                or (
                    terminal_state == "quarantined"
                    and error_code == "worker_containment_failed"
                )
            )
            expected_response_state = (
                "expired"
                if lifecycle_terminal
                and error_code == "lease_expired"
                else terminal_state
            )
            network_policy = response.get("network_policy")
            scope_digest = response.get("scope_digest")
            if (
                terminal_state not in {"closed", "quarantined"}
                or not (artifact_terminal or lifecycle_terminal)
                or response.get("state") != expected_response_state
                or response.get("lease_handle")
                != request.get("lease_handle")
                or not isinstance(scope_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", scope_digest) is None
                or response.get("skill_sha256")
                != request.get("skill_sha256")
                or response.get("script_sha256")
                != request.get("script_sha256")
                or response.get("artifacts") != []
                or response.get("runtime_profile") not in {
                    "base-v1",
                    "browser-automation-v1",
                    SESSION_SANDBOX_RUNTIME_PROFILE,
                }
                or not isinstance(network_policy, dict)
                or set(network_policy) != {"direct", "egress"}
                or network_policy.get("direct") != "disabled"
                or network_policy.get("egress") not in {
                    "none",
                    "policy_proxy",
                    "origin_allowlist_proxy",
                }
                or response.get("sync_token") is not None
                or response.get("sync_pending") is not None
                or (
                    not artifact_terminal
                    and (
                        response.get("artifacts_discarded")
                        is not None
                        or response.get("artifact_error_code")
                        is not None
                    )
                )
            ):
                raise IsolatedSkillExecutorError(
                    "invalid_response",
                    "Executor terminal process receipt is malformed.",
                )
        elif (
            "egress_policy_version" in response
            or "scope_digest" in response
        ):
            network_policy = response.get("network_policy")
            scope_digest = response.get("scope_digest")
            if (
                response.get("lease_handle")
                != request.get("lease_handle")
                or not isinstance(scope_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", scope_digest) is None
                or response.get("skill_sha256")
                != request.get("skill_sha256")
                or response.get("script_sha256")
                != request.get("script_sha256")
                or response.get("state")
                not in {"open", "running", "exited", "closing"}
                or response.get("artifacts") != []
                or response.get("runtime_profile") not in {
                    "base-v1",
                    "browser-automation-v1",
                    SESSION_SANDBOX_RUNTIME_PROFILE,
                }
                or not isinstance(network_policy, dict)
                or set(network_policy) != {"direct", "egress"}
                or network_policy.get("direct") != "disabled"
                or network_policy.get("egress") not in {
                    "none",
                    "policy_proxy",
                    "origin_allowlist_proxy",
                }
            ):
                raise IsolatedSkillExecutorError(
                    "invalid_response",
                    "Executor bound process error receipt is malformed.",
                )
        return dict(response), []

    if operation == "reap_all":
        runtime_profile = response.get("runtime_profile")
        network_policy = response.get("network_policy")
        if (
            runtime_profile not in {
                "base-v1",
                "browser-automation-v1",
                SESSION_SANDBOX_RUNTIME_PROFILE,
            }
            or not isinstance(network_policy, dict)
            or set(network_policy) != {"direct", "egress"}
            or network_policy.get("direct") != "disabled"
            or network_policy.get("egress") not in {
                "none",
                "policy_proxy",
                "origin_allowlist_proxy",
            }
            or isinstance(response.get("reaped_leases"), bool)
            or not isinstance(response.get("reaped_leases"), int)
            or response["reaped_leases"] < 0
            or response.get("worker_processes_empty") is not True
            or response.get("lease_handle") is not None
            or response.get("artifacts") not in (None, [])
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response",
                "Executor startup reap receipt is malformed or unconfined.",
            )
        return dict(response), []

    handle = response.get("lease_handle")
    if (
        not isinstance(handle, str)
        or len(handle) > 128
        or re.fullmatch(r"pl2_[A-Za-z0-9_-]+_[0-9a-f]{32}", handle) is None
        or (
            operation != "open"
            and handle != request.get("lease_handle")
        )
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor returned an invalid or mismatched lease handle.",
        )
    for field_name in ("scope_digest", "skill_sha256", "script_sha256"):
        value = response.get(field_name)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response",
                f"Executor returned an invalid {field_name}.",
            )
    if (
        response.get("skill_sha256") != request.get("skill_sha256")
        or response.get("script_sha256") != request.get("script_sha256")
        or response.get("state") not in {
            "open",
            "running",
            "exited",
            "closing",
            "closed",
        }
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor process content authority or state does not match.",
        )
    runtime_profile = response.get("runtime_profile")
    network_policy = response.get("network_policy")
    if (
        runtime_profile not in {
            "base-v1",
            "browser-automation-v1",
            SESSION_SANDBOX_RUNTIME_PROFILE,
        }
        or not isinstance(network_policy, dict)
        or set(network_policy) != {"direct", "egress"}
        or network_policy.get("direct") != "disabled"
        or network_policy.get("egress") not in {
            "none",
            "policy_proxy",
            "origin_allowlist_proxy",
        }
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor process runtime/network profile receipt is invalid.",
        )
    if operation == "open":
        expected_egress = (
            "origin_allowlist_proxy"
            if request.get("egress_origins")
            else "none"
        )
        if network_policy.get("egress") != expected_egress:
            raise IsolatedSkillExecutorError(
                "invalid_response",
                "Executor process did not attest the requested bounded "
                "network policy.",
            )
    replay = response.get("idempotent_replay")
    if replay is not None and not isinstance(replay, bool):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor idempotency receipt must be boolean.",
        )
    artifacts = _decode_artifacts(response) if operation in {"sync", "close"} else []
    if operation not in {"sync", "close"} and response.get("artifacts") not in (None, []):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Unexpected artifacts in process operation response.",
        )
    if operation in {"sync", "close"}:
        sync_token = response.get("sync_token")
        if (
            not isinstance(sync_token, str)
            or not 32 <= len(sync_token) <= 128
            or re.fullmatch(r"[A-Za-z0-9_-]+", sync_token) is None
            or response.get("sync_pending") is not True
            or response.get("sync_ack_grace_seconds")
            != PROCESS_SYNC_ACK_GRACE_SECONDS
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response",
                "Executor artifact batch is missing its bounded pending-sync receipt.",
            )
    elif operation == "ack":
        if (
            response.get("sync_acknowledged") is not True
            or response.get("acknowledged_operation") not in {"sync", "close"}
            or response.get("sync_token") is not None
            or response.get("sync_pending") is not None
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response",
                "Executor artifact acknowledgement receipt is malformed.",
            )
    if operation == "stdin_close":
        if (
            response.get("stdin_closed") is not True
            or not isinstance(response.get("already_closed"), bool)
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response",
                "Executor stdin-close receipt is malformed.",
            )
    if operation == "read":
        for stream_name in ("stdout", "stderr"):
            encoded = response.get(f"{stream_name}_b64")
            if not isinstance(encoded, str):
                raise IsolatedSkillExecutorError(
                    "invalid_response",
                    f"Executor {stream_name} chunk is malformed.",
                )
            try:
                content = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (UnicodeError, binascii.Error, ValueError) as exc:
                raise IsolatedSkillExecutorError(
                    "invalid_response",
                    f"Executor {stream_name} chunk is invalid base64.",
                ) from exc
            if len(content) > MAX_PROCESS_READ_BYTES:
                raise IsolatedSkillExecutorError(
                    "invalid_response",
                    f"Executor {stream_name} chunk exceeds its bound.",
                )
            for suffix in ("start_offset", "next_offset", "end_offset"):
                value = response.get(f"{stream_name}_{suffix}")
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise IsolatedSkillExecutorError(
                        "invalid_response",
                        f"Executor {stream_name} offsets are malformed.",
                    )
            for suffix in ("data_loss", "truncated", "eof"):
                if not isinstance(response.get(f"{stream_name}_{suffix}"), bool):
                    raise IsolatedSkillExecutorError(
                        "invalid_response",
                        f"Executor {stream_name} flags are malformed.",
                    )
    return dict(response), artifacts


def validate_skill_script_response(
    response: Any,
    *,
    request_id: str,
    request: dict[str, Any] | None = None,
    invocation_mode: str | None = None,
    function_name: str | None = None,
    class_name: str | None = None,
    method_name: str | None = None,
    expected_egress: bool | None = None,
) -> list[tuple[str, bytes, dict[str, Any]]]:
    if not isinstance(response, dict):
        raise IsolatedSkillExecutorError("invalid_response", "Executor response must be an object.")
    if (
        response.get("protocol_version") != PROTOCOL_VERSION
        or response.get("kind") != "skill_script_result"
        or response.get("request_id") != request_id
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor response protocol or request identity does not match."
        )
    status = response.get("status")
    if status not in {"success", "error", "timeout"}:
        raise IsolatedSkillExecutorError("invalid_response", "Executor response status is invalid.")
    if expected_egress is not None:
        expected_policy = (
            "origin_allowlist_proxy" if expected_egress else "none"
        )
        network_policy = response.get("network_policy")
        if (
            response.get("network") != "disabled"
            or response.get("runtime_profile")
            not in {
                "base-v1",
                "browser-automation-v1",
                SESSION_SANDBOX_RUNTIME_PROFILE,
            }
            or not isinstance(network_policy, dict)
            or network_policy != {
                "direct": "disabled",
                "egress": expected_policy,
            }
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response",
                "Executor response did not attest the requested bounded "
                "network policy.",
            )
    if status in {"error", "timeout"} and (
        not isinstance(response.get("error_code"), str)
        or not response["error_code"]
        or not isinstance(response.get("error"), str)
        or not response["error"]
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor Skill error receipt is missing its typed root cause.",
        )
    if invocation_mode is not None:
        if (
            invocation_mode not in {"cli", "function", "instance_method"}
            or response.get("invocation_mode") != invocation_mode
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor response invocation mode does not match the request."
            )
        if invocation_mode == "function" and response.get("function_name") != function_name:
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor response function identity does not match the request."
            )
        if invocation_mode == "instance_method" and (
            response.get("class_name") != class_name
            or response.get("method_name") != method_name
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response",
                "Executor response instance-method identity does not match the request.",
            )
    if request is not None:
        policy_version = int(request.get("egress_policy_version") or 2)
        post_spawn = (
            _one_shot_execution_requires_egress_audit(response)
            if policy_version == 3
            else False
        )
        _validate_terminal_egress_response(
            response,
            policy_version=policy_version,
            budget_scope_sha256=request.get("budget_scope_sha256"),
            call_id_sha256=request.get("call_id_sha256"),
            authority_sha256=(
                _egress_authority_sha256(request)
                if policy_version == 3
                else None
            ),
            require_audit=bool(
                policy_version == 3
                and post_spawn
            ),
        )
    for field, limit in (("stdout", MAX_STDOUT_BYTES), ("stderr", MAX_STDERR_BYTES)):
        value = response.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise IsolatedSkillExecutorError(
                "invalid_response", f"Executor response {field} exceeds its bounded string policy."
            )
    for field in ("stdout_truncated", "stderr_truncated"):
        value = response.get(field)
        if value is not None and not isinstance(value, bool):
            raise IsolatedSkillExecutorError(
                "invalid_response", f"Executor response {field} must be boolean."
            )
    return _decode_artifacts(response)


def validate_session_code_response(
    response: Any,
    *,
    request_id: str,
) -> list[tuple[str, bytes, dict[str, Any]]]:
    if not isinstance(response, dict):
        raise IsolatedSkillExecutorError("invalid_response", "Executor response must be an object.")
    if (
        response.get("protocol_version") != PROTOCOL_VERSION
        or response.get("kind") != "session_code_result"
        or response.get("request_id") != request_id
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor response protocol or request identity does not match."
        )
    if response.get("status") not in {"success", "error", "timeout"}:
        raise IsolatedSkillExecutorError("invalid_response", "Executor response status is invalid.")
    return _decode_artifacts(response)


def validate_declared_command_response(
    response: Any,
    *,
    request: dict[str, Any],
) -> list[tuple[str, bytes, dict[str, Any]]]:
    if not isinstance(response, dict):
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor command response must be an object."
        )
    if (
        response.get("protocol_version") != PROTOCOL_VERSION
        or response.get("kind") != "declared_command_result"
        or response.get("request_id") != request.get("request_id")
        or response.get("executable") != request.get("executable")
        or response.get("cwd") != request.get("cwd")
        or response.get("network") != "disabled"
        or response.get("shell") is not False
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor command protocol, identity, or isolation policy does not match.",
        )
    status = response.get("status")
    if status not in {"success", "error", "timeout"}:
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor command status is invalid."
        )
    expected_egress = (
        "origin_allowlist_proxy"
        if request.get("egress_origins")
        else "none"
    )
    network_policy = response.get("network_policy")
    if (
        response.get("runtime_profile")
        not in {
            "base-v1",
            "browser-automation-v1",
            SESSION_SANDBOX_RUNTIME_PROFILE,
        }
        or not isinstance(network_policy, dict)
        or network_policy != {
            "direct": "disabled",
            "egress": expected_egress,
        }
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor command response did not attest the requested bounded "
            "network policy.",
        )
    policy_version = int(request.get("egress_policy_version") or 2)
    post_spawn = (
        _one_shot_execution_requires_egress_audit(response)
        if policy_version == 3
        else False
    )
    _validate_terminal_egress_response(
        response,
        policy_version=policy_version,
        budget_scope_sha256=request.get("budget_scope_sha256"),
        call_id_sha256=request.get("call_id_sha256"),
        authority_sha256=(
            _egress_authority_sha256(request)
            if policy_version == 3
            else None
        ),
        require_audit=bool(
            policy_version == 3
            and post_spawn
        ),
    )
    if status in {"error", "timeout"} and (
        not isinstance(response.get("error_code"), str)
        or not response["error_code"]
        or not isinstance(response.get("error"), str)
        or not response["error"]
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor command error receipt is missing its typed root cause.",
        )
    invocation = json.dumps(
        [request["executable"], *(request.get("argv") or [])],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_digest = hashlib.sha256(invocation).hexdigest()
    if not hmac.compare_digest(str(response.get("command_sha256") or ""), expected_digest):
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor command receipt does not match the request."
        )
    for field, limit in (("stdout", MAX_STDOUT_BYTES), ("stderr", MAX_STDERR_BYTES)):
        value = response.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise IsolatedSkillExecutorError(
                "invalid_response", f"Executor response {field} exceeds its bound."
            )
    return _decode_artifacts(response)


def validate_runtime_capabilities_response(
    response: Any,
    *,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate protocol identity and the complete declaration/result mapping."""

    if not isinstance(response, dict):
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor capability response must be an object."
        )
    if (
        response.get("protocol_version") != PROTOCOL_VERSION
        or response.get("kind") != "runtime_capabilities_result"
        or response.get("request_id") != request.get("request_id")
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Executor capability protocol or request identity does not match.",
        )
    if response.get("status") == "error":
        code = response.get("error_code")
        message = response.get("error")
        raise IsolatedSkillExecutorError(
            str(code) if isinstance(code, str) and code else "runtime_capability_error",
            str(message)[:2_000]
            if isinstance(message, str) and message
            else "Executor runtime-capability evaluation failed.",
        )
    if response.get("status") != "success" or not isinstance(response.get("valid"), bool):
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor capability status is invalid."
        )
    identity = response.get("runtime_identity")
    expected_identity_fields = {
        "execution_runtime",
        "python_implementation",
        "python_version",
        "platform",
        "network",
        "dependency_install",
        "runtime_build_sha256",
    }
    allowed_identity_fields = expected_identity_fields | {
        "runtime_profile",
        "network_policy",
        "display_backend",
        "headed_browser",
        "x11",
        "execution_identity",
    }
    if (
        not isinstance(identity, dict)
        or not expected_identity_fields.issubset(identity)
        or not set(identity).issubset(allowed_identity_fields)
        or (("runtime_profile" in identity) != ("network_policy" in identity))
        or identity.get("execution_runtime") != "isolated_skill_executor"
        or identity.get("network") != "disabled"
        or identity.get("dependency_install") != "disabled"
        or not isinstance(identity.get("runtime_build_sha256"), str)
        or SHA256_RE.fullmatch(identity["runtime_build_sha256"]) is None
        or any(
            not isinstance(identity.get(field), str)
            or not identity.get(field)
            or len(identity[field]) > 128
            for field in ("python_implementation", "python_version", "platform")
        )
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor runtime identity is invalid."
        )
    if "runtime_profile" in identity:
        network_policy = identity.get("network_policy")
        runtime_profile = identity.get("runtime_profile")
        expected_display = (
            ("wayland-headless", True, False)
            if runtime_profile in {
                "browser-automation-v1",
                SESSION_SANDBOX_RUNTIME_PROFILE,
            }
            else ("none", False, False)
        )
        if (
            runtime_profile not in {
                "base-v1",
                "browser-automation-v1",
                SESSION_SANDBOX_RUNTIME_PROFILE,
            }
            or not isinstance(network_policy, dict)
            or set(network_policy) != {"direct", "egress"}
            or network_policy.get("direct") != "disabled"
            or network_policy.get("egress") not in {
                "none",
                "policy_proxy",
                "origin_allowlist_proxy",
            }
            or (
                identity.get("display_backend"),
                identity.get("headed_browser"),
                identity.get("x11"),
            ) != expected_display
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response",
                "Executor runtime network/profile identity is invalid.",
            )
    execution_identity = identity.get("execution_identity")
    if execution_identity is not None:
        if (
            not isinstance(execution_identity, dict)
            or not {
                "controller_uid",
                "controller_gid",
                "worker_uid",
                "worker_gid",
                "uid_isolated",
                "resource_launcher",
            }.issubset(execution_identity)
            or not set(execution_identity).issubset({
                "controller_uid",
                "controller_gid",
                "worker_uid",
                "worker_gid",
                "uid_isolated",
                "resource_launcher",
                "shared_state_isolated",
            })
            or any(
                isinstance(execution_identity.get(field), bool)
                or not isinstance(execution_identity.get(field), int)
                or execution_identity[field] < 0
                for field in (
                    "controller_uid",
                    "controller_gid",
                    "worker_uid",
                    "worker_gid",
                )
            )
            or not isinstance(execution_identity.get("uid_isolated"), bool)
            or execution_identity.get("resource_launcher") != "prlimit"
            or (
                "shared_state_isolated" in execution_identity
                and not isinstance(
                    execution_identity.get("shared_state_isolated"),
                    bool,
                )
            )
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response",
                "Executor process-identity attestation is invalid.",
            )

    requirement_results = response.get("requirements")
    expected_requirements = request.get("requirements") or []
    if (
        not isinstance(requirement_results, list)
        or len(requirement_results) != len(expected_requirements)
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor requirement results do not match the request."
        )
    allowed_requirement_fields = {
        "requirement",
        "status",
        "satisfied",
        "installed_version",
        "extras_checked",
        "unsatisfied_dependencies",
    }
    for expected, item in zip(expected_requirements, requirement_results):
        if (
            not isinstance(item, dict)
            or not set(item).issubset(allowed_requirement_fields)
            or item.get("requirement") != expected
            or not isinstance(item.get("status"), str)
            or len(item["status"]) > 128
            or not isinstance(item.get("satisfied"), bool)
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor returned an invalid requirement result."
            )
        version = item.get("installed_version")
        if version is not None and (not isinstance(version, str) or len(version) > 256):
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor returned an invalid package version."
            )
        extras = item.get("extras_checked")
        if extras is not None and (
            not isinstance(extras, list)
            or len(extras) > 64
            or any(not isinstance(extra, str) or len(extra) > 128 for extra in extras)
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor returned an invalid extras result."
            )
        dependencies = item.get("unsatisfied_dependencies")
        if dependencies is not None and (
            not isinstance(dependencies, list)
            or len(dependencies) > 20
            or any(
                not isinstance(dependency, dict)
                or set(dependency) != {"requirement", "status"}
                or not isinstance(dependency.get("requirement"), str)
                or len(dependency["requirement"]) > MAX_RUNTIME_DECLARATION_CHARS
                or not isinstance(dependency.get("status"), str)
                or len(dependency["status"]) > 128
                for dependency in dependencies
            )
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor returned invalid dependency details."
            )

    def validate_named_availability(field: str, expected: list[str]) -> list[dict[str, Any]]:
        results = response.get(field)
        if not isinstance(results, list) or len(results) != len(expected):
            raise IsolatedSkillExecutorError(
                "invalid_response", f"Executor {field} results do not match the request."
            )
        for expected_name, item in zip(expected, results):
            if (
                not isinstance(item, dict)
                or set(item) != {"name", "available"}
                or item.get("name") != expected_name
                or not isinstance(item.get("available"), bool)
            ):
                raise IsolatedSkillExecutorError(
                    "invalid_response", f"Executor returned an invalid {field} result."
                )
        return results

    command_results = validate_named_availability(
        "commands", request.get("commands") or []
    )
    environment_results = validate_named_availability(
        "environment_variables", request.get("environment_variables") or []
    )
    platform_results = response.get("platform_groups")
    expected_platforms = request.get("platform_groups") or []
    if not isinstance(platform_results, list) or len(platform_results) != len(expected_platforms):
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor platform results do not match the request."
        )
    for expected_allowed, item in zip(expected_platforms, platform_results):
        if (
            not isinstance(item, dict)
            or set(item) != {"allowed", "current", "satisfied"}
            or item.get("allowed") != expected_allowed
            or item.get("current") != identity.get("platform")
            or not isinstance(item.get("satisfied"), bool)
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor returned an invalid platform result."
            )
    derived_valid = (
        all(item["satisfied"] for item in requirement_results)
        and all(item["available"] for item in command_results)
        and all(item["available"] for item in environment_results)
        and all(item["satisfied"] for item in platform_results)
    )
    if response.get("valid") is not derived_valid:
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor capability validity is internally inconsistent."
        )
    return response


def _ensure_safe_parent(root: Path, relative: str) -> Path:
    current = root
    for component in relative.split("/")[:-1]:
        current = current / component
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except OSError as exc:
                raise IsolatedSkillExecutorError(
                    "artifact_apply_failed", f"Cannot create artifact directory: {component}"
                ) from exc
            continue
        except OSError as exc:
            raise IsolatedSkillExecutorError(
                "artifact_apply_failed", f"Cannot inspect artifact directory: {component}"
            ) from exc
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise IsolatedSkillExecutorError(
                "unsafe_workspace_path", f"Artifact parent is not a real directory: {relative}"
            )
    return current


def _apply_artifact_batch_locked(
    workspace: Path,
    artifacts: list[tuple[str, bytes, dict[str, Any]]],
    *,
    baseline: dict[str, tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    """Validate/stage a batch, then atomically replace each individual file."""

    root = _verified_directory(Path(workspace), field="workspace")
    staged: list[
        tuple[
            Path,
            Path,
            dict[str, Any],
            tuple[int, str] | None,
        ]
    ] = []
    already_applied: set[str] = set()
    replaced: set[str] = set()
    try:
        for relative, content, metadata in artifacts:
            parent = _ensure_safe_parent(root, relative)
            target = parent / relative.split("/")[-1]
            try:
                target_stat = target.lstat()
            except FileNotFoundError:
                target_stat = None
            except OSError as exc:
                raise IsolatedSkillExecutorError(
                    "artifact_apply_failed", f"Cannot inspect artifact target: {relative}"
                ) from exc
            if target_stat is not None and (
                stat.S_ISLNK(target_stat.st_mode)
                or not stat.S_ISREG(target_stat.st_mode)
                or target_stat.st_nlink != 1
            ):
                raise IsolatedSkillExecutorError(
                    "unsafe_workspace_path",
                    f"Artifact target must be an independent regular file: {relative}",
                )
            current_identity: tuple[int, str] | None = None
            if target_stat is not None:
                current = _read_snapshot_file(
                    target,
                    display_path=relative,
                    max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
                )
                current_identity = (
                    len(current),
                    hashlib.sha256(current).hexdigest(),
                )
            desired_identity = (
                metadata["size_bytes"],
                metadata["sha256"],
            )
            # A previous attempt may have atomically replaced an earlier file
            # before a later replace failed. Recognize that exact content as
            # an idempotently applied member of the same authenticated batch.
            if current_identity == desired_identity:
                already_applied.add(relative)
                continue
            if baseline is not None:
                expected = baseline.get(relative)
                change = metadata.get("change")
                if change == "created":
                    if expected is not None or target_stat is not None:
                        raise IsolatedSkillExecutorError(
                            "workspace_concurrent_modification",
                            f"Workspace target appeared after the execution snapshot: {relative}",
                        )
                elif change == "modified":
                    if expected is None or target_stat is None:
                        raise IsolatedSkillExecutorError(
                            "workspace_concurrent_modification",
                            f"Workspace target disappeared after the execution snapshot: {relative}",
                        )
                    if current_identity != expected:
                        raise IsolatedSkillExecutorError(
                            "workspace_concurrent_modification",
                            f"Workspace target changed after the execution snapshot: {relative}",
                        )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".chatds-artifact-", dir=str(parent)
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            staged.append((temporary, target, metadata, current_identity))

        applied: list[dict[str, Any]] = []
        # Staging can involve several bounded files. Re-check the complete
        # target cohort immediately before the first replace so a
        # non-cooperating writer cannot slip between the initial CAS and
        # commit while bytes are being fsynced.
        for _, target, metadata, validated_identity in staged:
            relative = metadata["path"]
            parent = _ensure_safe_parent(root, relative)
            current_target = parent / relative.split("/")[-1]
            if current_target != target:
                raise IsolatedSkillExecutorError(
                    "workspace_concurrent_modification",
                    f"Workspace target path changed before artifact apply: {relative}",
                )
            try:
                target_stat = target.lstat()
            except FileNotFoundError:
                current_identity = None
            except OSError as exc:
                raise IsolatedSkillExecutorError(
                    "artifact_apply_failed",
                    f"Cannot re-inspect artifact target: {relative}",
                ) from exc
            else:
                if (
                    stat.S_ISLNK(target_stat.st_mode)
                    or not stat.S_ISREG(target_stat.st_mode)
                    or target_stat.st_nlink != 1
                ):
                    raise IsolatedSkillExecutorError(
                        "unsafe_workspace_path",
                        f"Artifact target must remain an independent regular file: {relative}",
                    )
                current = _read_snapshot_file(
                    target,
                    display_path=relative,
                    max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
                )
                current_identity = (
                    len(current),
                    hashlib.sha256(current).hexdigest(),
                )
            if current_identity != validated_identity:
                raise IsolatedSkillExecutorError(
                    "workspace_concurrent_modification",
                    f"Workspace target changed during artifact staging: {relative}",
                )

        for temporary, target, metadata, _ in staged:
            try:
                os.replace(temporary, target)
            except OSError as exc:
                raise IsolatedSkillExecutorError(
                    "artifact_apply_failed", f"Cannot atomically apply artifact: {metadata['path']}"
                ) from exc
            replaced.add(metadata["path"])
        for _, _, metadata in artifacts:
            if metadata["path"] not in already_applied | replaced:
                continue
            applied.append({
                "kind": "file",
                "path": metadata["path"],
                "change": metadata["change"],
                "size_bytes": metadata["size_bytes"],
                "sha256": metadata["sha256"],
                "source": "isolated_skill_executor",
            })
        return applied
    finally:
        for temporary, _, _, _ in staged:
            temporary.unlink(missing_ok=True)


def apply_artifacts_atomically(
    workspace: Path,
    artifacts: list[tuple[str, bytes, dict[str, Any]]],
    *,
    baseline: dict[str, tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    """CAS-apply a recoverable artifact batch under the workspace lock.

    When ``baseline`` is supplied, a changed target is rejected. All targets
    are validated and staged before the first replace. Individual file
    replacements are atomic; a multi-file batch is deliberately recoverable
    and idempotent rather than falsely represented as one filesystem
    transaction. An exact retry recognizes already-applied desired bytes and
    completes the authenticated batch.

    The historical function name is retained for compatibility and refers to
    atomic file replacement, not all-or-none batch commit.
    """

    root = _verified_directory(Path(workspace), field="workspace")
    with workspace_mutation_guard(root):
        return _apply_artifact_batch_locked(
            root,
            artifacts,
            baseline=baseline,
        )


async def apply_artifacts_atomically_async(
    workspace: Path,
    artifacts: list[tuple[str, bytes, dict[str, Any]]],
    *,
    baseline: dict[str, tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    """Run the complete CAS/stage/fsync/replace section off the event loop."""

    return await run_sync_cancellation_safe(
        lambda: apply_artifacts_atomically(
            workspace,
            artifacts,
            baseline=baseline,
        )
    )


async def _exchange_process_request(
    payload: dict[str, Any],
    encoded: bytes,
    *,
    socket_path: str,
    timeout: float,
) -> tuple[dict[str, Any], list[tuple[str, bytes, dict[str, Any]]]]:
    last_transport_error: BaseException | None = None
    dispatch_unknown = False
    # One bounded transparent transport retry is safe because the exact same
    # signed request, request_id, and op_id bytes are replayed. The executor
    # serializes the lease and returns its cached operation result instead of
    # dispatching a write/signal/call twice.
    for attempt in range(2):
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    socket_path,
                    limit=MAX_RESPONSE_BYTES + 1,
                ),
                timeout=3,
            )
            # From the first write onward a transport failure cannot prove
            # whether the executor accepted the authenticated operation.
            dispatch_unknown = True
            writer.write(encoded)
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not raw:
                raise IsolatedSkillExecutorError(
                    "executor_unavailable",
                    "Executor closed the process socket without a response.",
                )
            if len(raw) > MAX_RESPONSE_BYTES or not raw.endswith(b"\n"):
                raise IsolatedSkillExecutorError(
                    "invalid_response",
                    "Executor process response exceeds the bounded line protocol.",
                )
            try:
                response = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise IsolatedSkillExecutorError(
                    "invalid_response",
                    "Executor process response is invalid JSON.",
                ) from exc
            return validate_process_lease_response(response, request=payload)
        except asyncio.CancelledError as exc:
            if dispatch_unknown:
                raise _ProcessDispatchCancelled(
                    "Persistent process transport was cancelled after dispatch."
                ) from exc
            raise
        except IsolatedSkillExecutorError as exc:
            projected = (
                IsolatedSkillExecutorError(
                    exc.code,
                    str(exc),
                    dispatch_unknown=True,
                    terminal_lease_state=exc.terminal_lease_state,
                )
                if dispatch_unknown and not exc.dispatch_unknown
                else exc
            )
            if projected.code != "executor_unavailable" or attempt:
                if projected is exc:
                    raise
                raise projected from exc
            last_transport_error = projected
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            last_transport_error = exc
            if attempt:
                break
        except Exception as exc:
            if dispatch_unknown:
                raise IsolatedSkillExecutorError(
                    "executor_unavailable",
                    "Persistent process transport failed after dispatch "
                    f"({type(exc).__name__}).",
                    dispatch_unknown=True,
                ) from exc
            raise
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except asyncio.CancelledError as exc:
                    if dispatch_unknown:
                        raise _ProcessDispatchCancelled(
                            "Persistent process transport was cancelled after dispatch."
                        ) from exc
                    raise
                except (OSError, ConnectionError):
                    pass
                except Exception as exc:
                    if dispatch_unknown:
                        raise IsolatedSkillExecutorError(
                            "executor_unavailable",
                            "Persistent process transport cleanup failed after "
                            f"dispatch ({type(exc).__name__}).",
                            dispatch_unknown=True,
                        ) from exc
                    raise
    exc = last_transport_error
    raise IsolatedSkillExecutorError(
        "executor_unavailable",
        "The isolated process executor remained unavailable after one "
        f"idempotent transport retry ({type(exc).__name__ if exc is not None else 'unknown'}).",
        dispatch_unknown=dispatch_unknown,
    ) from exc


def _require_process_success(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("status") == "success":
        return response
    raise IsolatedSkillExecutorError(
        str(response.get("error_code") or "process_error"),
        str(response.get("error") or "Persistent process operation failed."),
        terminal_lease_state=(
            response.get("terminal_lease_state")
            if isinstance(response.get("terminal_lease_state"), str)
            else None
        ),
    )


def _executor_pool_for_socket(socket_path: str):
    try:
        return get_executor_slot_pool(primary_socket=socket_path)
    except ExecutorSlotPoolError as exc:
        raise IsolatedSkillExecutorError(exc.code, str(exc)) from exc


async def _release_executor_slot(
    reservation: ExecutorSlotReservation,
) -> None:
    """Map pool invariant failures without forgetting the reservation."""

    try:
        await reservation.release()
    except ExecutorSlotPoolError as exc:
        raise IsolatedSkillExecutorError(exc.code, str(exc)) from exc


async def _quarantine_executor_slot(
    reservation: ExecutorSlotReservation,
    reason: str,
) -> None:
    """Quarantine through the stable executor-client error boundary."""

    try:
        await reservation.quarantine(reason)
    except ExecutorSlotPoolError as exc:
        raise IsolatedSkillExecutorError(exc.code, str(exc)) from exc


async def _drain_one_shot_writer_close(
    writer: asyncio.StreamWriter,
) -> tuple[asyncio.CancelledError | None, BaseException | None]:
    """Close a one-shot transport without abandoning cleanup on cancellation.

    A validated executor receipt proves that the child reached a terminal
    controller state, but the reservation is still owned by this coroutine
    until its return value reaches the caller.  Cancellation while awaiting
    ``wait_closed`` used to override that return and strand the physical slot
    in ``reserved`` forever.  Keep the bounded close task runtime-owned and
    report cancellation only after it has reached a terminal outcome so the
    caller can first release or quarantine the reservation.
    """

    cancellation: asyncio.CancelledError | None = None
    cleanup_error: BaseException | None = None
    try:
        writer.close()
    except asyncio.CancelledError as exc:
        cancellation = exc
    except (ConnectionError, OSError):
        return None, None
    except BaseException as exc:
        cleanup_error = exc

    if cleanup_error is None and cancellation is None:
        closing = asyncio.create_task(
            asyncio.wait_for(
                writer.wait_closed(),
                timeout=1.0,
            )
        )
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError as exc:
                cancellation = exc
                continue
        try:
            closing.result()
        except (ConnectionError, OSError, asyncio.TimeoutError):
            pass
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException as exc:
            cleanup_error = exc
    return cancellation, cleanup_error


async def open_isolated_process_lease(
    *,
    owner_scope: ProcessOwnerScope,
    skill_root: Path,
    workspace: Path,
    entrypoint: str,
    args: list[str] | None = None,
    cwd: str = "workspace",
    idle_ttl_seconds: int = 300,
    max_runtime_seconds: int = MAX_PROCESS_RUNTIME_SECONDS,
    class_name: str | None = None,
    factory_name: str | None = None,
    constructor_args: list[Any] | None = None,
    constructor_kwargs: dict[str, Any] | None = None,
    egress_origins: tuple[str, ...] | list[str] | None = None,
    egress_rules: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    private_origins: tuple[str, ...] | list[str] | None = None,
    budget_scope_sha256: str | None = None,
    call_id_sha256: str | None = None,
    socket_path: str = EXECUTOR_SOCKET,
    op_id: str | None = None,
    skill_snapshot: SkillPackageSnapshot | None = None,
    admission_cancel_event: asyncio.Event | None = None,
) -> tuple[IsolatedProcessLease, dict[str, Any]]:
    payload, encoded = build_process_lease_open_request(
        owner_scope=owner_scope,
        skill_root=skill_root,
        workspace=workspace,
        entrypoint=entrypoint,
        args=args,
        cwd=cwd,
        idle_ttl_seconds=idle_ttl_seconds,
        max_runtime_seconds=max_runtime_seconds,
        class_name=class_name,
        factory_name=factory_name,
        constructor_args=constructor_args,
        constructor_kwargs=constructor_kwargs,
        egress_origins=egress_origins,
        egress_rules=egress_rules,
        private_origins=private_origins,
        budget_scope_sha256=budget_scope_sha256,
        call_id_sha256=call_id_sha256,
        op_id=op_id,
        skill_snapshot=skill_snapshot,
    )
    pool = _executor_pool_for_socket(socket_path)
    excluded_sockets: set[str] = set()
    response: dict[str, Any] | None = None
    reservation: ExecutorSlotReservation | None = None
    last_error: IsolatedSkillExecutorError | None = None
    while len(excluded_sockets) < len(pool.socket_paths):
        try:
            reservation = await pool.acquire(
                "persistent",
                excluded_sockets=frozenset(excluded_sockets),
                cancel_event=admission_cancel_event,
            )
        except ExecutorSlotPoolError as exc:
            raise IsolatedSkillExecutorError(exc.code, str(exc)) from exc
        try:
            response, _ = await _exchange_process_request(
                payload,
                encoded,
                socket_path=reservation.socket_path,
                timeout=10,
            )
        except IsolatedSkillExecutorError as exc:
            await _quarantine_executor_slot(
                reservation,
                "process_open_dispatch_unknown"
                if exc.dispatch_unknown
                else "process_open_transport_unavailable",
            )
            if exc.dispatch_unknown:
                raise
            excluded_sockets.add(reservation.socket_path)
            last_error = exc
            reservation = None
            continue
        except BaseException:
            await _quarantine_executor_slot(
                reservation,
                "process_open_cancelled_unknown",
            )
            raise
        if (
            response.get("status") != "success"
            and response.get("error_code") == "worker_busy"
        ):
            await _quarantine_executor_slot(
                reservation,
                "executor_reported_worker_busy",
            )
            excluded_sockets.add(reservation.socket_path)
            last_error = IsolatedSkillExecutorError(
                "worker_busy",
                str(response.get("error") or "Executor worker is busy."),
            )
            reservation = None
            response = None
            continue
        try:
            _require_process_success(response)
        except IsolatedSkillExecutorError as exc:
            if exc.code in {
                "worker_containment_failed",
                "worker_admission_lost",
                "executor_quarantined",
            }:
                await _quarantine_executor_slot(reservation, exc.code)
            else:
                await _release_executor_slot(reservation)
            raise
        break
    if response is None or reservation is None:
        raise last_error or IsolatedSkillExecutorError(
            "executor_unavailable",
            "No healthy executor pool slot accepted the process lease.",
        )
    egress_policy_version = int(
        payload.get("egress_policy_version") or 2
    )
    egress_authority_sha256 = (
        _egress_authority_sha256(payload)
        if egress_policy_version == 3
        else None
    )
    try:
        _validate_terminal_egress_response(
            response,
            policy_version=egress_policy_version,
            budget_scope_sha256=payload.get("budget_scope_sha256"),
            call_id_sha256=payload.get("call_id_sha256"),
            authority_sha256=egress_authority_sha256,
            require_audit=False,
        )
    except IsolatedSkillExecutorError:
        await _quarantine_executor_slot(
            reservation,
            "invalid_egress_audit_response",
        )
        raise
    baseline = {
        item["path"]: (item["size_bytes"], item["sha256"])
        for item in payload["workspace_files"]
    }
    invocation = payload["invocation"]
    lease = IsolatedProcessLease(
        handle=response["lease_handle"],
        skill_sha256=payload["skill_sha256"],
        script_sha256=payload["script_sha256"],
        entrypoint=payload["entrypoint"],
        invocation_mode=invocation["mode"],
        class_name=invocation.get("class_name"),
        factory_name=invocation.get("factory_name"),
        _owner_scope=owner_scope,
        _workspace=Path(workspace),
        _socket_path=reservation.socket_path,
        _baseline=baseline,
        _egress_policy_version=egress_policy_version,
        _budget_scope_sha256=payload.get("budget_scope_sha256"),
        _call_id_sha256=payload.get("call_id_sha256"),
        _egress_authority_sha256=egress_authority_sha256,
        _slot_reservation=reservation,
    )
    return lease, response


async def reap_isolated_executor_leases(
    *,
    socket_path: str = EXECUTOR_SOCKET,
    op_id: str | None = None,
) -> dict[str, Any]:
    """Reap orphan leases before a replacement Harness begins serving."""

    payload, encoded = build_process_reap_request(op_id=op_id)
    response, _ = await _exchange_process_request(
        payload,
        encoded,
        socket_path=socket_path,
        timeout=15,
    )
    return _require_process_success(response)


async def _execute_process_operation(
    lease: IsolatedProcessLease,
    operation: str,
    *,
    op_id: str | None = None,
    extra: dict[str, Any] | None = None,
    timeout: float = 10,
) -> tuple[dict[str, Any], list[tuple[str, bytes, dict[str, Any]]]]:
    payload, encoded = _build_process_operation_request(
        lease,
        operation,
        op_id=op_id,
        extra=extra,
    )
    try:
        response, artifacts = await _exchange_process_request(
            payload,
            encoded,
            socket_path=lease._socket_path,
            timeout=timeout,
        )
        if operation == "ack" and response.get("status") == "success":
            pending_operation = lease._pending_sync_operation
            acknowledged_operation = response.get(
                "acknowledged_operation"
            )
            acknowledged_state = response.get("state")
            if (
                acknowledged_operation != pending_operation
                or (
                    pending_operation == "close"
                    and acknowledged_state != "closed"
                )
                or (
                    pending_operation == "sync"
                    and acknowledged_state
                    not in {"open", "running", "exited"}
                )
            ):
                raise IsolatedSkillExecutorError(
                    "invalid_response",
                    "Executor artifact acknowledgement does not match the "
                    "pending operation and lifecycle state.",
                )
        terminal_audit_required = bool(
            lease._egress_policy_version == 3
            and (
                response.get("terminal_lease_state")
                in {"closed", "quarantined"}
                or (
                    operation == "close"
                    and (
                        response.get("status") == "success"
                        or response.get("terminal_lease_state")
                        in {"closed", "quarantined"}
                        or response.get("state")
                        in {"closing", "closed", "quarantined"}
                    )
                )
                or (
                    operation == "ack"
                    and lease._pending_sync_operation == "close"
                )
            )
        )
        _validate_terminal_egress_response(
            response,
            policy_version=lease._egress_policy_version,
            budget_scope_sha256=lease._budget_scope_sha256,
            call_id_sha256=lease._call_id_sha256,
            authority_sha256=lease._egress_authority_sha256,
            require_audit=terminal_audit_required,
        )
        _require_process_success(response)
    except IsolatedSkillExecutorError as exc:
        if (
            exc.code == "invalid_response"
            and lease._slot_reservation is not None
        ):
            reservation = lease._slot_reservation
            await _quarantine_executor_slot(
                reservation,
                "invalid_egress_audit_response",
            )
            if reservation.terminal:
                lease._slot_reservation = None
            lease.closed = True
            raise IsolatedSkillExecutorError(
                exc.code,
                str(exc),
                terminal_lease_state="quarantined",
            ) from exc
        else:
            await finalize_terminal_process_lease_error(lease, exc)
        raise
    return response, artifacts


async def start_isolated_process_lease(
    lease: IsolatedProcessLease,
    *,
    op_id: str | None = None,
) -> dict[str, Any]:
    response, _ = await _execute_process_operation(lease, "start", op_id=op_id)
    return response


async def write_isolated_process_stdin(
    lease: IsolatedProcessLease,
    data: bytes | str,
    *,
    op_id: str | None = None,
) -> dict[str, Any]:
    if isinstance(data, str):
        try:
            content = data.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise IsolatedSkillExecutorError(
                "invalid_stdin",
                "stdin text must be valid UTF-8.",
            ) from exc
    elif isinstance(data, bytes):
        content = data
    else:
        raise IsolatedSkillExecutorError(
            "invalid_stdin",
            "stdin data must be bytes or text.",
        )
    if not content or len(content) > MAX_PROCESS_STDIN_CHUNK_BYTES:
        raise IsolatedSkillExecutorError(
            "invalid_stdin",
            f"stdin data must contain 1 to {MAX_PROCESS_STDIN_CHUNK_BYTES} bytes.",
        )
    response, _ = await _execute_process_operation(
        lease,
        "write",
        op_id=op_id,
        extra={"stdin_b64": base64.b64encode(content).decode("ascii")},
    )
    return response


async def close_isolated_process_stdin(
    lease: IsolatedProcessLease,
    *,
    op_id: str | None = None,
) -> dict[str, Any]:
    """Deliver EOF without terminating the leased process."""

    if lease.invocation_mode != "cli":
        raise IsolatedSkillExecutorError(
            "invalid_invocation",
            "Explicit stdin EOF is available only for CLI process leases.",
        )
    response, _ = await _execute_process_operation(
        lease,
        "stdin_close",
        op_id=op_id,
    )
    return response


async def call_isolated_process_instance(
    lease: IsolatedProcessLease,
    *,
    method_name: str,
    method_args: list[Any] | None = None,
    method_kwargs: dict[str, Any] | None = None,
    op_id: str | None = None,
) -> dict[str, Any]:
    if lease.invocation_mode not in {"instance", "factory"}:
        raise IsolatedSkillExecutorError(
            "invalid_invocation",
            "Structured calls require a persistent public-object lease.",
        )
    if (
        not isinstance(method_name, str)
        or method_name.startswith("_")
        or PUBLIC_FUNCTION_RE.fullmatch(method_name) is None
    ):
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            "method_name must be one public, non-dotted Python identifier.",
        )
    positional = [] if method_args is None else method_args
    keywords = {} if method_kwargs is None else method_kwargs
    if not isinstance(positional, list) or len(positional) > MAX_FUNCTION_ARGS:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            f"method_args must contain at most {MAX_FUNCTION_ARGS} JSON values.",
        )
    if not isinstance(keywords, dict) or len(keywords) > MAX_FUNCTION_KWARGS:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            f"method_kwargs must contain at most {MAX_FUNCTION_KWARGS} JSON values.",
        )
    for key in keywords:
        if (
            not isinstance(key, str)
            or key.startswith("_")
            or PUBLIC_FUNCTION_RE.fullmatch(key) is None
        ):
            raise IsolatedSkillExecutorError(
                "invalid_function_call",
                "method_kwargs keys must be public Python identifiers.",
            )
    _validate_function_json(positional, field="method_args")
    _validate_function_json(keywords, field="method_kwargs")
    try:
        encoded = json.dumps(
            {"args": positional, "kwargs": keywords},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            "Method arguments must be finite valid JSON.",
        ) from exc
    if len(encoded) > MAX_PROCESS_CALL_BYTES:
        raise IsolatedSkillExecutorError(
            "invalid_function_call",
            "Method call JSON exceeds the atomic process-call byte limit.",
        )
    response, _ = await _execute_process_operation(
        lease,
        "call",
        op_id=op_id,
        extra={
            "method_name": method_name,
            "method_args": positional,
            "method_kwargs": keywords,
        },
    )
    return response


async def read_isolated_process_output(
    lease: IsolatedProcessLease,
    *,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
    max_bytes: int = MAX_PROCESS_READ_BYTES,
    wait_ms: int = 0,
    op_id: str | None = None,
) -> dict[str, Any]:
    for field_name, value in (
        ("stdout_offset", stdout_offset),
        ("stderr_offset", stderr_offset),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise IsolatedSkillExecutorError(
                "invalid_stream_offset",
                f"{field_name} must be a non-negative integer.",
            )
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_PROCESS_READ_BYTES
    ):
        raise IsolatedSkillExecutorError(
            "invalid_read_limit",
            f"max_bytes must be between 1 and {MAX_PROCESS_READ_BYTES}.",
        )
    if (
        isinstance(wait_ms, bool)
        or not isinstance(wait_ms, int)
        or not 0 <= wait_ms <= MAX_PROCESS_READ_WAIT_MS
    ):
        raise IsolatedSkillExecutorError(
            "invalid_read_wait",
            f"wait_ms must be between 0 and {MAX_PROCESS_READ_WAIT_MS}.",
        )
    response, _ = await _execute_process_operation(
        lease,
        "read",
        op_id=op_id,
        extra={
            "stdout_offset": stdout_offset,
            "stderr_offset": stderr_offset,
            "max_bytes": max_bytes,
            "wait_ms": wait_ms,
        },
        timeout=wait_ms / 1_000 + 10,
    )
    result = dict(response)
    for stream_name in ("stdout", "stderr"):
        result[f"{stream_name}_bytes"] = base64.b64decode(
            response[f"{stream_name}_b64"].encode("ascii"),
            validate=True,
        )
    return result


async def _apply_process_artifacts(
    lease: IsolatedProcessLease,
    response: dict[str, Any],
    artifacts: list[tuple[str, bytes, dict[str, Any]]],
) -> dict[str, Any]:
    applied = (
        await apply_artifacts_atomically_async(
            lease._workspace,
            artifacts,
            baseline=lease._baseline,
        )
        if artifacts
        else []
    )
    for _, _, metadata in artifacts:
        lease._baseline[metadata["path"]] = (
            metadata["size_bytes"],
            metadata["sha256"],
        )
    result = dict(response)
    result["artifacts"] = applied
    result["workspace_applied"] = True
    return result


async def _prepare_pending_process_sync(
    lease: IsolatedProcessLease,
    *,
    operation: str,
    op_id: str | None,
    apply_artifacts: bool,
) -> None:
    requested_apply = bool(apply_artifacts)
    if lease._pending_sync_operation is None:
        try:
            prepare_op_id = (
                str(uuid.UUID(op_id))
                if op_id is not None
                else str(uuid.uuid4())
            )
        except (ValueError, AttributeError) as exc:
            raise IsolatedSkillExecutorError(
                "invalid_request_id",
                "op_id must be a UUID string.",
            ) from exc
        lease._pending_sync_operation = operation
        lease._pending_sync_prepare_op_id = prepare_op_id
        lease._pending_sync_ack_op_id = str(uuid.uuid4())
        lease._pending_sync_apply_artifacts = requested_apply
    elif lease._pending_sync_operation != operation:
        raise IsolatedSkillExecutorError(
            "sync_ack_required",
            "A previous artifact batch is still pending local apply/ack.",
        )
    elif lease._pending_sync_apply_artifacts is not requested_apply:
        raise IsolatedSkillExecutorError(
            "pending_sync_intent_mismatch",
            "The pending artifact transaction is bound to a different "
            "apply_artifacts intent.",
        )
    if lease._pending_sync_response is not None:
        return
    prepare_op_id = lease._pending_sync_prepare_op_id
    if prepare_op_id is None:
        raise IsolatedSkillExecutorError(
            "invalid_lease_state",
            "The pending artifact prepare operation lost its idempotency identity.",
        )
    # Retain prepare_op_id before transport. If both exact transport attempts
    # lose their responses after server dispatch, a later lifecycle retry
    # replays the same operation and recovers the cached sync token.
    response, artifacts = await _execute_process_operation(
        lease,
        operation,
        op_id=prepare_op_id,
    )
    lease._pending_sync_response = dict(response)
    lease._pending_sync_artifacts = artifacts
    lease._pending_sync_applied = None


async def _finish_pending_process_sync(
    lease: IsolatedProcessLease,
    *,
    execution_authority_check: Callable[[], None] | None = None,
    apply_artifacts: bool = True,
) -> dict[str, Any]:
    operation = lease._pending_sync_operation
    response = lease._pending_sync_response
    artifacts = lease._pending_sync_artifacts
    ack_op_id = lease._pending_sync_ack_op_id
    bound_apply_artifacts = lease._pending_sync_apply_artifacts
    if (
        operation not in {"sync", "close"}
        or response is None
        or artifacts is None
        or ack_op_id is None
        or bound_apply_artifacts is None
    ):
        raise IsolatedSkillExecutorError(
            "invalid_lease_state",
            "The pending artifact transaction is incomplete.",
        )
    if bound_apply_artifacts is not bool(apply_artifacts):
        raise IsolatedSkillExecutorError(
            "pending_sync_intent_mismatch",
            "The pending artifact transaction is bound to a different "
            "apply_artifacts intent.",
        )

    if lease._pending_sync_applied is None:
        if execution_authority_check is not None:
            execution_authority_check()
        if bound_apply_artifacts:
            applied_result = await _apply_process_artifacts(
                lease,
                response,
                artifacts,
            )
            lease._pending_sync_applied = list(
                applied_result["artifacts"]
            )
        else:
            lease._pending_sync_applied = []

    ack_response, _ = await _execute_process_operation(
        lease,
        "ack",
        op_id=ack_op_id,
        extra={"sync_token": response["sync_token"]},
    )
    result = dict(response)
    result.pop("sync_token", None)
    result["artifacts"] = list(lease._pending_sync_applied)
    result["workspace_applied"] = bound_apply_artifacts
    result["sync_pending"] = False
    result["sync_acknowledged"] = True
    result["acknowledged_operation"] = ack_response["acknowledged_operation"]
    result["state"] = ack_response["state"]

    lease._pending_sync_operation = None
    lease._pending_sync_prepare_op_id = None
    lease._pending_sync_response = None
    lease._pending_sync_artifacts = None
    lease._pending_sync_ack_op_id = None
    lease._pending_sync_applied = None
    lease._pending_sync_apply_artifacts = None
    if operation == "close":
        lease.closed = True
        lease._closed_response = dict(result)
        await _release_closed_process_slot(lease)
    return result


async def _finish_pending_process_sync_for_session_cleanup(
    lease: IsolatedProcessLease,
    *,
    execution_authority_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Acknowledge one prepared batch without applying still-pending bytes.

    This is deliberately private and is reachable only from the trusted
    session-runtime cleanup path.  The prepare replay and ACK still use the
    exact lease owner capability, operation ids, and sync token.  Only the
    Harness-local workspace apply is abandoned.  A batch that was already
    applied before deletion linearized is merely ACKed with its original
    intent; it is never represented as discarded retroactively.
    """

    operation = lease._pending_sync_operation
    bound_apply_artifacts = lease._pending_sync_apply_artifacts
    if (
        operation not in {"sync", "close"}
        or bound_apply_artifacts is None
    ):
        raise IsolatedSkillExecutorError(
            "invalid_lease_state",
            "Session cleanup requires one prepared sync or close transaction.",
        )

    # If prepare lost its response, replay the exact original transaction
    # before changing the local apply decision.  The executor's idempotency
    # cache returns the same sync token and artifact batch.
    await _prepare_pending_process_sync(
        lease,
        operation=operation,
        op_id=None,
        apply_artifacts=bound_apply_artifacts,
    )

    if lease._pending_sync_applied is None:
        # No workspace batch has crossed the mutation lock yet.  Session
        # deletion owns a durable tombstone, so applying it now would both
        # violate the fence and prevent the physical lease from closing.
        lease._pending_sync_apply_artifacts = False
        finish_apply_artifacts = False
    else:
        # The batch crossed the workspace lock before deletion published its
        # marker.  Do not replay it, but preserve the truthful close receipt.
        finish_apply_artifacts = bool(
            lease._pending_sync_apply_artifacts
        )

    return await _finish_pending_process_sync(
        lease,
        execution_authority_check=execution_authority_check,
        apply_artifacts=finish_apply_artifacts,
    )


async def sync_isolated_process_artifacts(
    lease: IsolatedProcessLease,
    *,
    op_id: str | None = None,
    execution_authority_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if lease._pending_sync_operation == "close":
        raise IsolatedSkillExecutorError(
            "lease_closing",
            "The process lease has a pending close transaction.",
        )
    await _prepare_pending_process_sync(
        lease,
        operation="sync",
        op_id=op_id,
        apply_artifacts=True,
    )
    return await _finish_pending_process_sync(
        lease,
        execution_authority_check=execution_authority_check,
    )


async def signal_isolated_process(
    lease: IsolatedProcessLease,
    selected_signal: str,
    *,
    op_id: str | None = None,
) -> dict[str, Any]:
    if selected_signal not in {"interrupt", "terminate", "kill"}:
        raise IsolatedSkillExecutorError(
            "invalid_signal",
            "selected_signal must be interrupt, terminate, or kill.",
        )
    response, _ = await _execute_process_operation(
        lease,
        "signal",
        op_id=op_id,
        extra={"signal": selected_signal},
    )
    return response


async def _release_closed_process_slot(
    lease: IsolatedProcessLease,
) -> None:
    """Finish a closed lease's pool transition before forgetting affinity."""

    reservation = lease._slot_reservation
    if reservation is None:
        return
    try:
        if not reservation.terminal:
            await _release_executor_slot(reservation)
    finally:
        # ``ExecutorSlotReservation.release`` drains its pool transition before
        # re-delivering cancellation.  Clear the stale affinity even on that
        # cancellation path, but retain it after an actual internal failure so
        # an exact close retry can finish the transition.
        if (
            reservation.terminal
            and lease._slot_reservation is reservation
        ):
            lease._slot_reservation = None


async def close_isolated_process_lease(
    lease: IsolatedProcessLease,
    *,
    op_id: str | None = None,
    execution_authority_check: Callable[[], None] | None = None,
    apply_artifacts: bool = True,
    discard_pending_artifacts: bool = False,
) -> dict[str, Any]:
    """Close one lease, optionally abandoning an unapplied prepared batch.

    ``discard_pending_artifacts`` is an internal session-deletion capability,
    not a model-facing option.  It still performs the authenticated executor
    ACK, but guarantees that no new workspace apply is attempted.
    """

    if discard_pending_artifacts and apply_artifacts:
        raise IsolatedSkillExecutorError(
            "invalid_cleanup_intent",
            "Discarding pending artifacts requires apply_artifacts=False.",
        )
    if lease.closed:
        if lease._closed_response is not None:
            await _release_closed_process_slot(lease)
            return dict(lease._closed_response)
        raise IsolatedSkillExecutorError(
            "lease_closed",
            "The process lease is already closed.",
        )
    if (
        discard_pending_artifacts
        and lease._pending_sync_operation in {"sync", "close"}
    ):
        pending_operation = lease._pending_sync_operation
        result = await _finish_pending_process_sync_for_session_cleanup(
            lease,
            execution_authority_check=execution_authority_check,
        )
        if pending_operation == "close":
            return result
    if lease._pending_sync_operation == "sync":
        await _prepare_pending_process_sync(
            lease,
            operation="sync",
            op_id=None,
            apply_artifacts=True,
        )
        await _finish_pending_process_sync(
            lease,
            execution_authority_check=execution_authority_check,
            apply_artifacts=True,
        )
    await _prepare_pending_process_sync(
        lease,
        operation="close",
        op_id=op_id,
        apply_artifacts=apply_artifacts,
    )
    return await _finish_pending_process_sync(
        lease,
        execution_authority_check=execution_authority_check,
        apply_artifacts=apply_artifacts,
    )


def probe_isolated_runtime_capabilities(
    *,
    requirements: list[str] | tuple[str, ...] | None = None,
    commands: list[str] | tuple[str, ...] | None = None,
    environment_variables: list[str] | tuple[str, ...] | None = None,
    platform_groups: list[list[str]] | tuple[tuple[str, ...], ...] | None = None,
    socket_path: str = EXECUTOR_SOCKET,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Synchronously query the sidecar used by activation-plan compilation."""

    payload, request = build_runtime_capabilities_request(
        requirements=requirements,
        commands=commands,
        environment_variables=environment_variables,
        platform_groups=platform_groups,
    )
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 10:
        raise IsolatedSkillExecutorError(
            "invalid_timeout", "Runtime capability timeout must be between 0.1 and 10 seconds."
        )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(float(timeout))
            connection.connect(socket_path)
            connection.sendall(request)
            chunks = bytearray()
            while b"\n" not in chunks:
                chunk = connection.recv(min(64 * 1024, MAX_RESPONSE_BYTES + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise IsolatedSkillExecutorError(
                        "invalid_response",
                        "Executor capability response exceeds the bounded protocol.",
                    )
    except IsolatedSkillExecutorError:
        raise
    except (OSError, TimeoutError) as exc:
        raise IsolatedSkillExecutorError(
            "executor_unavailable",
            f"The isolated Skill executor capability endpoint is unavailable ({type(exc).__name__}).",
        ) from exc
    if not chunks or b"\n" not in chunks:
        raise IsolatedSkillExecutorError(
            "executor_unavailable", "Executor capability endpoint closed without a response."
        )
    raw, trailing = bytes(chunks).split(b"\n", 1)
    if trailing.strip():
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor capability endpoint returned multiple responses."
        )
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor capability endpoint returned invalid JSON."
        ) from exc
    return validate_runtime_capabilities_response(response, request=payload)


async def _exchange_pooled_one_shot(
    encoded: bytes,
    *,
    socket_path: str,
    response_timeout: float,
    validate_response: Callable[
        [dict[str, Any]],
        list[tuple[str, bytes, dict[str, Any]]],
    ],
    execution_authority_check: Callable[[], None] | None,
) -> tuple[
    dict[str, Any],
    list[tuple[str, bytes, dict[str, Any]]],
    ExecutorSlotReservation,
]:
    """Dispatch once, retrying only receipts that prove no child ran."""

    pool = _executor_pool_for_socket(socket_path)
    excluded_sockets: set[str] = set()
    last_error: IsolatedSkillExecutorError | None = None
    while len(excluded_sockets) < len(pool.socket_paths):
        try:
            reservation = await pool.acquire(
                "transient",
                excluded_sockets=frozenset(excluded_sockets),
            )
        except ExecutorSlotPoolError as exc:
            raise IsolatedSkillExecutorError(exc.code, str(exc)) from exc
        writer: asyncio.StreamWriter | None = None
        dispatch_unknown = False
        trusted_terminal_response = False
        try:
            if execution_authority_check is not None:
                execution_authority_check()
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(
                        reservation.socket_path,
                        limit=MAX_RESPONSE_BYTES + 1,
                    ),
                    timeout=3,
                )
            except (OSError, asyncio.TimeoutError, ValueError) as exc:
                await _quarantine_executor_slot(
                    reservation,
                    "executor_connect_unavailable",
                )
                excluded_sockets.add(reservation.socket_path)
                last_error = IsolatedSkillExecutorError(
                    "executor_unavailable",
                    "An executor pool slot was unavailable before dispatch "
                    f"({type(exc).__name__}).",
                )
                continue
            # Once a write is attempted, a missing or malformed response
            # cannot prove that the untrusted child was never dispatched.
            dispatch_unknown = True
            writer.write(encoded)
            await writer.drain()
            raw = await asyncio.wait_for(
                reader.readline(),
                timeout=response_timeout,
            )
            if not raw:
                raise IsolatedSkillExecutorError(
                    "executor_unavailable",
                    "Executor closed the socket without a response.",
                    dispatch_unknown=True,
                )
            if len(raw) > MAX_RESPONSE_BYTES or not raw.endswith(b"\n"):
                raise IsolatedSkillExecutorError(
                    "invalid_response",
                    "Executor response exceeds the bounded line protocol.",
                    dispatch_unknown=True,
                )
            try:
                response = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise IsolatedSkillExecutorError(
                    "invalid_response",
                    "Executor returned invalid JSON.",
                    dispatch_unknown=True,
                ) from exc
            try:
                artifacts = validate_response(response)
            except IsolatedSkillExecutorError as exc:
                await _quarantine_executor_slot(
                    reservation,
                    "executor_response_validation_failed",
                )
                raise IsolatedSkillExecutorError(
                    exc.code,
                    str(exc),
                    dispatch_unknown=True,
                ) from exc
            # Validation authenticates and correlates the bounded executor
            # receipt. From here onward the child/controller transaction is
            # terminal even though this coroutine still owns the pool
            # reservation until it can return it to its caller.
            trusted_terminal_response = True
            if (
                response.get("status") == "error"
                and response.get("error_code")
                == "worker_containment_failed"
            ):
                # The executor has finished the child but deliberately retained
                # admission because its controller-owned tree could not be
                # proven absent. Never publish this slot as free: authenticated
                # reap + homogeneous attestation is the only rejoin path.
                await _quarantine_executor_slot(
                    reservation,
                    "worker_containment_failed",
                )
                raise IsolatedSkillExecutorError(
                    "worker_containment_failed",
                    str(
                        response.get("error")
                        or "Executor could not prove one-shot containment."
                    ),
                    dispatch_unknown=True,
                )
            if (
                response.get("status") == "error"
                and response.get("error_code") == "worker_busy"
            ):
                # The authenticated typed receipt is emitted before runner
                # dispatch.  It is the sole post-write cross-slot retry case.
                await _quarantine_executor_slot(
                    reservation,
                    "executor_reported_worker_busy",
                )
                excluded_sockets.add(reservation.socket_path)
                last_error = IsolatedSkillExecutorError(
                    "worker_busy",
                    str(response.get("error") or "Executor worker is busy."),
                )
                continue
            return response, artifacts, reservation
        except asyncio.CancelledError:
            if dispatch_unknown:
                await _quarantine_executor_slot(
                    reservation,
                    "one_shot_cancelled_after_dispatch",
                )
            else:
                await _release_executor_slot(reservation)
            raise
        except IsolatedSkillExecutorError:
            if not reservation.terminal:
                if dispatch_unknown:
                    await _quarantine_executor_slot(
                        reservation,
                        "one_shot_dispatch_state_unknown",
                    )
                else:
                    await _release_executor_slot(reservation)
            raise
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            if dispatch_unknown:
                await _quarantine_executor_slot(
                    reservation,
                    "one_shot_transport_state_unknown",
                )
                raise IsolatedSkillExecutorError(
                    "executor_unavailable",
                    "The isolated executor response was lost after dispatch "
                    f"({type(exc).__name__}).",
                    dispatch_unknown=True,
                ) from exc
            await _quarantine_executor_slot(
                reservation,
                "executor_connect_unavailable",
            )
            excluded_sockets.add(reservation.socket_path)
            last_error = IsolatedSkillExecutorError(
                "executor_unavailable",
                "An executor pool slot was unavailable before dispatch "
                f"({type(exc).__name__}).",
            )
        except BaseException:
            if dispatch_unknown:
                await _quarantine_executor_slot(
                    reservation,
                    "one_shot_dispatch_state_unknown",
                )
            else:
                await _release_executor_slot(reservation)
            raise
        finally:
            if writer is not None:
                cleanup_cancellation, cleanup_error = (
                    await _drain_one_shot_writer_close(writer)
                )
                if (
                    (
                        cleanup_cancellation is not None
                        or cleanup_error is not None
                    )
                    and not reservation.terminal
                ):
                    # The response is returned together with the reservation.
                    # If cleanup prevents that hand-off, this coroutine must
                    # publish the terminal slot transition itself.
                    if trusted_terminal_response:
                        await _release_executor_slot(reservation)
                    elif dispatch_unknown:
                        await _quarantine_executor_slot(
                            reservation,
                            "one_shot_dispatch_state_unknown",
                        )
                    else:
                        await _release_executor_slot(reservation)
                if cleanup_cancellation is not None:
                    raise cleanup_cancellation
                if cleanup_error is not None:
                    raise cleanup_error
    raise last_error or IsolatedSkillExecutorError(
        "executor_unavailable",
        "No healthy executor pool slot accepted the one-shot execution.",
    )


async def execute_isolated_skill_script(
    *,
    skill_root: Path,
    workspace: Path,
    entrypoint: str,
    args: list[str] | None = None,
    timeout: int = 120,
    cwd: str = "workspace",
    function_name: str | None = None,
    function_args: list[Any] | None = None,
    function_kwargs: dict[str, Any] | None = None,
    class_name: str | None = None,
    method_name: str | None = None,
    constructor_args: list[Any] | None = None,
    constructor_kwargs: dict[str, Any] | None = None,
    method_args: list[Any] | None = None,
    method_kwargs: dict[str, Any] | None = None,
    expected_skill_sha256: str | None = None,
    egress_origins: tuple[str, ...] | list[str] | None = None,
    egress_rules: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    private_origins: tuple[str, ...] | list[str] | None = None,
    budget_scope_sha256: str | None = None,
    call_id_sha256: str | None = None,
    socket_path: str = EXECUTOR_SOCKET,
    apply_artifacts: bool = True,
    execution_authority_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Execute a verified Skill snapshot and optionally apply valid artifacts."""

    payload, request = build_skill_script_request(
        skill_root=skill_root,
        workspace=workspace,
        entrypoint=entrypoint,
        args=args,
        timeout=timeout,
        cwd=cwd,
        function_name=function_name,
        function_args=function_args,
        function_kwargs=function_kwargs,
        class_name=class_name,
        method_name=method_name,
        constructor_args=constructor_args,
        constructor_kwargs=constructor_kwargs,
        method_args=method_args,
        method_kwargs=method_kwargs,
        expected_skill_sha256=expected_skill_sha256,
        egress_origins=egress_origins,
        egress_rules=egress_rules,
        private_origins=private_origins,
        budget_scope_sha256=budget_scope_sha256,
        call_id_sha256=call_id_sha256,
    )
    reservation: ExecutorSlotReservation | None = None
    try:
        response, artifacts, reservation = await _exchange_pooled_one_shot(
            request,
            socket_path=socket_path,
            response_timeout=timeout + 10,
            validate_response=lambda candidate: (
                validate_skill_script_response(
                    candidate,
                    request_id=payload["request_id"],
                    request=payload,
                    invocation_mode=payload["invocation"]["mode"],
                    function_name=payload["invocation"].get("name"),
                    class_name=payload["invocation"].get("class_name"),
                    method_name=payload["invocation"].get("method_name"),
                    expected_egress=bool(payload["egress_origins"]),
                )
            ),
            execution_authority_check=execution_authority_check,
        )
        baseline = {
            item["path"]: (item["size_bytes"], item["sha256"])
            for item in payload["workspace_files"]
        }
        if execution_authority_check is not None:
            execution_authority_check()
        applied = (
            await apply_artifacts_atomically_async(
                Path(workspace),
                artifacts,
                baseline=baseline,
            )
            if apply_artifacts and artifacts
            else []
        )
        result = dict(response)
        result["artifacts"] = applied if apply_artifacts else [
            {
                "kind": "file",
                "path": metadata["path"],
                "change": metadata["change"],
                "size_bytes": metadata["size_bytes"],
                "sha256": metadata["sha256"],
                "source": "isolated_skill_executor",
            }
            for _, _, metadata in artifacts
        ]
        result["workspace_applied"] = bool(apply_artifacts)
        return result
    except IsolatedSkillExecutorError:
        raise
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        raise IsolatedSkillExecutorError(
            "executor_unavailable",
            f"The isolated Skill executor is unavailable ({type(exc).__name__}).",
        ) from exc
    finally:
        if reservation is not None:
            await _release_executor_slot(reservation)


async def execute_isolated_declared_command(
    *,
    skill_root: Path,
    workspace: Path,
    executable: str,
    argv: list[str] | None = None,
    cwd: str = "workspace",
    timeout: int = 120,
    egress_origins: tuple[str, ...] | list[str] | None = None,
    egress_rules: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    private_origins: tuple[str, ...] | list[str] | None = None,
    budget_scope_sha256: str | None = None,
    call_id_sha256: str | None = None,
    socket_path: str = EXECUTOR_SOCKET,
    apply_artifacts: bool = True,
    execution_authority_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Execute one compiled PATH command through the no-shell sidecar."""
    payload, request = build_declared_command_request(
        skill_root=skill_root,
        workspace=workspace,
        executable=executable,
        argv=argv,
        cwd=cwd,
        timeout=timeout,
        egress_origins=egress_origins,
        egress_rules=egress_rules,
        private_origins=private_origins,
        budget_scope_sha256=budget_scope_sha256,
        call_id_sha256=call_id_sha256,
    )
    reservation: ExecutorSlotReservation | None = None
    try:
        response, artifacts, reservation = await _exchange_pooled_one_shot(
            request,
            socket_path=socket_path,
            response_timeout=timeout + 10,
            validate_response=lambda candidate: (
                validate_declared_command_response(
                    candidate,
                    request=payload,
                )
            ),
            execution_authority_check=execution_authority_check,
        )
        baseline = {
            item["path"]: (item["size_bytes"], item["sha256"])
            for item in payload["workspace_files"]
        }
        command_succeeded = response.get("status") == "success"
        if execution_authority_check is not None:
            execution_authority_check()
        applied = (
            await apply_artifacts_atomically_async(
                Path(workspace),
                artifacts,
                baseline=baseline,
            )
            if apply_artifacts and command_succeeded and artifacts else []
        )
        result = dict(response)
        result["artifacts"] = applied if apply_artifacts and command_succeeded else [
            {
                "kind": "file",
                "path": metadata["path"],
                "change": metadata["change"],
                "size_bytes": metadata["size_bytes"],
                "sha256": metadata["sha256"],
                "source": "isolated_declared_command_executor",
            }
            for _, _, metadata in artifacts
        ]
        if apply_artifacts and command_succeeded:
            for receipt in result["artifacts"]:
                receipt["source"] = "isolated_declared_command_executor"
        if not command_succeeded and artifacts:
            result["discarded_artifact_count"] = len(artifacts)
        result["workspace_applied"] = bool(apply_artifacts and command_succeeded)
        return result
    except IsolatedSkillExecutorError:
        raise
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        raise IsolatedSkillExecutorError(
            "executor_unavailable",
            f"The isolated declared-command executor is unavailable ({type(exc).__name__}).",
        ) from exc
    finally:
        if reservation is not None:
            await _release_executor_slot(reservation)


async def execute_isolated_session_code(
    *,
    workspace: Path,
    code: str,
    timeout: int = 30,
    skills_root: Path | None = None,
    socket_path: str = EXECUTOR_SOCKET,
    apply_artifacts: bool = True,
    execution_authority_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run model-authored Python only in the network-disabled sidecar."""

    payload, request = build_session_code_request(
        workspace=workspace,
        code=code,
        timeout=timeout,
        skills_root=skills_root,
    )
    reservation: ExecutorSlotReservation | None = None
    try:
        response, artifacts, reservation = await _exchange_pooled_one_shot(
            request,
            socket_path=socket_path,
            response_timeout=timeout + 10,
            validate_response=lambda candidate: (
                validate_session_code_response(
                    candidate,
                    request_id=payload["request_id"],
                )
            ),
            execution_authority_check=execution_authority_check,
        )
        baseline = {
            item["path"]: (item["size_bytes"], item["sha256"])
            for item in payload["workspace_files"]
        }
        if execution_authority_check is not None:
            execution_authority_check()
        applied = (
            await apply_artifacts_atomically_async(
                Path(workspace),
                artifacts,
                baseline=baseline,
            )
            if apply_artifacts and artifacts
            else []
        )
        result = dict(response)
        result["artifacts"] = applied if apply_artifacts else [
            {
                "kind": "file",
                "path": metadata["path"],
                "change": metadata["change"],
                "size_bytes": metadata["size_bytes"],
                "sha256": metadata["sha256"],
                "source": "isolated_session_code_executor",
            }
            for _, _, metadata in artifacts
        ]
        if apply_artifacts:
            for receipt in result["artifacts"]:
                receipt["source"] = "isolated_session_code_executor"
        result["workspace_applied"] = bool(apply_artifacts)
        return result
    except IsolatedSkillExecutorError:
        raise
    except (OSError, asyncio.TimeoutError, ValueError) as exc:
        raise IsolatedSkillExecutorError(
            "executor_unavailable",
            f"The isolated session-code executor is unavailable ({type(exc).__name__}).",
        ) from exc
    finally:
        if reservation is not None:
            await _release_executor_slot(reservation)


def validate_legacy_code_response(
    response: Any,
) -> list[tuple[str, bytes, dict[str, Any]]]:
    """Validate the compatibility compute receipt before releasing its slot."""

    if not isinstance(response, dict):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Legacy compute executor returned a non-object response.",
        )
    status = response.get("status")
    if status not in {"success", "error", "timeout"}:
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Legacy compute executor returned an invalid status.",
        )
    for field_name, maximum in (
        ("output", MAX_STDOUT_BYTES + MAX_STDERR_BYTES + 64),
        ("error", MAX_STDERR_BYTES + 1_024),
        ("error_code", 128),
    ):
        value = response.get(field_name)
        if value is not None and (
            not isinstance(value, str)
            or len(value) > maximum
        ):
            raise IsolatedSkillExecutorError(
                "invalid_response",
                f"Legacy compute executor returned an invalid {field_name}.",
            )
    if (
        "network" in response
        and response.get("network") != "disabled"
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Legacy compute executor returned an invalid network receipt.",
        )
    if "exit_code" in response and (
        isinstance(response.get("exit_code"), bool)
        or not isinstance(response.get("exit_code"), int)
    ):
        raise IsolatedSkillExecutorError(
            "invalid_response",
            "Legacy compute executor returned an invalid exit code.",
        )
    return []


async def execute_isolated_legacy_code(
    *,
    code: str,
    timeout: int = 30,
    socket_path: str = EXECUTOR_SOCKET,
    execution_authority_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run compatibility compute through the same homogeneous slot pool."""

    if not isinstance(code, str) or not code.strip():
        raise IsolatedSkillExecutorError(
            "invalid_code",
            "Legacy compute code must be a non-empty string.",
        )
    try:
        encoded_code = code.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise IsolatedSkillExecutorError(
            "invalid_code",
            "Legacy compute code must be valid UTF-8.",
        ) from exc
    if len(encoded_code) > MAX_CODE_BYTES:
        raise IsolatedSkillExecutorError(
            "code_limit_exceeded",
            f"Legacy compute code exceeds the {MAX_CODE_BYTES}-byte limit.",
        )
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= MAX_SKILL_TIMEOUT
    ):
        raise IsolatedSkillExecutorError(
            "invalid_timeout",
            "Legacy compute timeout is outside the bounded executor policy.",
        )
    request = (
        json.dumps(
            {"code": code, "timeout": timeout},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(request) > MAX_REQUEST_BYTES:
        raise IsolatedSkillExecutorError(
            "request_limit_exceeded",
            "Legacy compute request exceeds the bounded executor protocol.",
        )

    reservation: ExecutorSlotReservation | None = None
    try:
        response, _artifacts, reservation = (
            await _exchange_pooled_one_shot(
                request,
                socket_path=socket_path,
                response_timeout=timeout + 10,
                validate_response=validate_legacy_code_response,
                execution_authority_check=execution_authority_check,
            )
        )
        return response
    finally:
        if reservation is not None:
            await _release_executor_slot(reservation)
