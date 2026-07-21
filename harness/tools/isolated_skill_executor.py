"""Client for the network-disabled session-code and Skill-execution protocol.

This module is intentionally lower level than the public Skill tools.  Its
caller must first resolve and authorize an installed Skill root.  The client
then snapshots only that exact root and the current session workspace, sends
content-addressed regular files over a bounded Unix socket request, validates
the complete response, and atomically replaces each returned workspace file.
Skill Python may run as a CLI or as one strictly data-described public
top-level function call; neither form accepts model-authored wrapper code.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import socket
import stat
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any


EXECUTOR_SOCKET = os.environ.get(
    "EXECUTOR_SOCKET", "/run/chat-ds-executor/executor.sock"
)
PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024 * 1024
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
MAX_WORKSPACE_FILE_BYTES = 8 * 1024 * 1024
MAX_WORKSPACE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_FILES = 512
MAX_OUTPUT_FILE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_TOTAL_BYTES = 24 * 1024 * 1024
MAX_SNAPSHOT_ENTRIES = 4_096
MAX_RUNTIME_REQUIREMENTS = 80
MAX_RUNTIME_COMMANDS = 80
MAX_RUNTIME_ENVIRONMENT_VARIABLES = 80
MAX_RUNTIME_PLATFORM_GROUPS = 32
MAX_RUNTIME_PLATFORMS_PER_GROUP = 32
MAX_RUNTIME_DECLARATION_CHARS = 512

SUPPORTED_EXTENSIONS = frozenset({".py", ".sh", ".bash", ".js", ".mjs"})
PUBLIC_FUNCTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
RUNTIME_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
RUNTIME_ENVIRONMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
RUNTIME_PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
WORKSPACE_SKIP_DIRS = frozenset({
    ".chatds",
    "debug",
    "cache",
    "__pycache__",
    ".pytest_cache",
    ".git",
})


class IsolatedSkillExecutorError(ValueError):
    """A stable local snapshot, transport, or response validation failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
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
    request_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Build one bounded, content-addressed protocol request."""

    safe_entrypoint = _safe_relative_path(entrypoint, field="entrypoint")
    if PurePosixPath(safe_entrypoint).suffix not in SUPPORTED_EXTENSIONS:
        raise IsolatedSkillExecutorError(
            "unsupported_script_type",
            "entrypoint must end in .py, .sh, .bash, .js, or .mjs.",
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
    workspace_files = _snapshot_tree(
        Path(workspace),
        field="workspace",
        max_files=MAX_WORKSPACE_FILES,
        max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
        max_total_bytes=MAX_WORKSPACE_TOTAL_BYTES,
        skip_dirs=WORKSPACE_SKIP_DIRS,
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


def build_declared_command_request(
    *,
    skill_root: Path,
    workspace: Path,
    executable: str,
    argv: list[str] | None = None,
    cwd: str = "workspace",
    timeout: int = 120,
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


def validate_skill_script_response(
    response: Any,
    *,
    request_id: str,
    invocation_mode: str | None = None,
    function_name: str | None = None,
    class_name: str | None = None,
    method_name: str | None = None,
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
    if response.get("status") not in {"success", "error", "timeout"}:
        raise IsolatedSkillExecutorError("invalid_response", "Executor response status is invalid.")
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
    if response.get("status") not in {"success", "error", "timeout"}:
        raise IsolatedSkillExecutorError(
            "invalid_response", "Executor command status is invalid."
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
    }
    if (
        not isinstance(identity, dict)
        or set(identity) != expected_identity_fields
        or identity.get("execution_runtime") != "isolated_skill_executor"
        or identity.get("network") != "disabled"
        or identity.get("dependency_install") != "disabled"
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


def apply_artifacts_atomically(
    workspace: Path,
    artifacts: list[tuple[str, bytes, dict[str, Any]]],
    *,
    baseline: dict[str, tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    """Validate all targets, stage all bytes, then atomically replace each file.

    When ``baseline`` is supplied, refuse to overwrite a target that changed
    after the request snapshot.  This prevents concurrent session workers from
    silently clobbering one another's artifacts.
    """

    root = _verified_directory(Path(workspace), field="workspace")
    staged: list[tuple[Path, Path, dict[str, Any]]] = []
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
                    current = _read_snapshot_file(
                        target,
                        display_path=relative,
                        max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
                    )
                    current_identity = (len(current), hashlib.sha256(current).hexdigest())
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
            staged.append((temporary, target, metadata))

        applied: list[dict[str, Any]] = []
        for temporary, target, metadata in staged:
            try:
                os.replace(temporary, target)
            except OSError as exc:
                raise IsolatedSkillExecutorError(
                    "artifact_apply_failed", f"Cannot atomically apply artifact: {metadata['path']}"
                ) from exc
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
        for temporary, _, _ in staged:
            temporary.unlink(missing_ok=True)


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
    socket_path: str = EXECUTOR_SOCKET,
    apply_artifacts: bool = True,
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
    )
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(
                socket_path,
                limit=MAX_RESPONSE_BYTES + 1,
            ),
            timeout=3,
        )
        writer.write(request)
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout + 10)
        if not raw:
            raise IsolatedSkillExecutorError(
                "executor_unavailable", "Executor closed the socket without a response."
            )
        if len(raw) > MAX_RESPONSE_BYTES or not raw.endswith(b"\n"):
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor response exceeds the bounded line protocol."
            )
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor returned invalid JSON."
            ) from exc
        artifacts = validate_skill_script_response(
            response,
            request_id=payload["request_id"],
            invocation_mode=payload["invocation"]["mode"],
            function_name=payload["invocation"].get("name"),
            class_name=payload["invocation"].get("class_name"),
            method_name=payload["invocation"].get("method_name"),
        )
        baseline = {
            item["path"]: (item["size_bytes"], item["sha256"])
            for item in payload["workspace_files"]
        }
        applied = (
            apply_artifacts_atomically(Path(workspace), artifacts, baseline=baseline)
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
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def execute_isolated_declared_command(
    *,
    skill_root: Path,
    workspace: Path,
    executable: str,
    argv: list[str] | None = None,
    cwd: str = "workspace",
    timeout: int = 120,
    socket_path: str = EXECUTOR_SOCKET,
    apply_artifacts: bool = True,
) -> dict[str, Any]:
    """Execute one compiled PATH command through the no-shell sidecar."""
    payload, request = build_declared_command_request(
        skill_root=skill_root,
        workspace=workspace,
        executable=executable,
        argv=argv,
        cwd=cwd,
        timeout=timeout,
    )
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path, limit=MAX_RESPONSE_BYTES + 1),
            timeout=3,
        )
        writer.write(request)
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout + 10)
        if not raw:
            raise IsolatedSkillExecutorError(
                "executor_unavailable", "Executor closed the socket without a response."
            )
        if len(raw) > MAX_RESPONSE_BYTES or not raw.endswith(b"\n"):
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor response exceeds the bounded line protocol."
            )
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor returned invalid JSON."
            ) from exc
        artifacts = validate_declared_command_response(response, request=payload)
        baseline = {
            item["path"]: (item["size_bytes"], item["sha256"])
            for item in payload["workspace_files"]
        }
        command_succeeded = response.get("status") == "success"
        applied = (
            apply_artifacts_atomically(Path(workspace), artifacts, baseline=baseline)
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
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def execute_isolated_session_code(
    *,
    workspace: Path,
    code: str,
    timeout: int = 30,
    skills_root: Path | None = None,
    socket_path: str = EXECUTOR_SOCKET,
    apply_artifacts: bool = True,
) -> dict[str, Any]:
    """Run model-authored Python only in the network-disabled sidecar."""

    payload, request = build_session_code_request(
        workspace=workspace,
        code=code,
        timeout=timeout,
        skills_root=skills_root,
    )
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(
                socket_path,
                limit=MAX_RESPONSE_BYTES + 1,
            ),
            timeout=3,
        )
        writer.write(request)
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout + 10)
        if not raw:
            raise IsolatedSkillExecutorError(
                "executor_unavailable", "Executor closed the socket without a response."
            )
        if len(raw) > MAX_RESPONSE_BYTES or not raw.endswith(b"\n"):
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor response exceeds the bounded line protocol."
            )
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise IsolatedSkillExecutorError(
                "invalid_response", "Executor returned invalid JSON."
            ) from exc
        artifacts = validate_session_code_response(
            response,
            request_id=payload["request_id"],
        )
        baseline = {
            item["path"]: (item["size_bytes"], item["sha256"])
            for item in payload["workspace_files"]
        }
        applied = (
            apply_artifacts_atomically(Path(workspace), artifacts, baseline=baseline)
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
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
