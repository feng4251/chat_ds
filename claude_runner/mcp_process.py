#!/usr/bin/env python3
"""Bounded stdio MCP for interactive processes inside one Claude Turn.

The server owns every child process and its stdin for the lifetime of the MCP
connection.  It deliberately exposes no shell string and cleans all children
when Claude closes the server, so persistent Skill CLIs have typed lifecycle
receipts instead of relying on background-Bash notification timing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


MAX_PROCESSES = 8
MAX_ARGV_ITEMS = 64
MAX_ARG_CHARS = 8_192
MAX_TOTAL_ARG_BYTES = 64 * 1024
MAX_WRITE_CHARS = 256 * 1024
MAX_READ_BYTES = 256 * 1024
PROCESS_ID = re.compile(r"^[0-9a-f]{24}$")
EXECUTABLE_ROOTS = (
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/bin"),
    Path("/opt/chatds-browser-runtime"),
)
WORKING_ROOTS = (Path("/workspace"), Path("/skill-view/plugin"))


class ProcessToolError(RuntimeError):
    pass


class ProcessRegistry:
    def __init__(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="chatds-process-"))
        os.chmod(self._root, 0o700)
        self._rows: dict[str, dict[str, Any]] = {}

    def open(self, argv: object, cwd: object = "/workspace") -> dict[str, Any]:
        if len(self._rows) >= MAX_PROCESSES:
            raise ProcessToolError("process_limit_reached")
        normalized = _argv(argv)
        normalized[0] = _executable(normalized[0])
        working = _working_directory(cwd)
        process_id = uuid.uuid4().hex[:24]
        log_path = self._root / f"{process_id}.log"
        log = log_path.open("w+b", buffering=0)
        try:
            process = subprocess.Popen(
                normalized,
                cwd=working,
                env=_child_environment(),
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            log.close()
            log_path.unlink(missing_ok=True)
            raise ProcessToolError("process_start_failed") from exc
        self._rows[process_id] = {
            "process": process,
            "log": log,
            "path": log_path,
        }
        return {
            "process_id": process_id,
            "status": "running" if process.poll() is None else "exited",
            "exit_code": process.poll(),
        }

    def write(
        self,
        process_id: object,
        text: object,
        append_newline: object = True,
    ) -> dict[str, Any]:
        row = self._row(process_id)
        process: subprocess.Popen[bytes] = row["process"]
        if process.poll() is not None:
            return self._status(str(process_id))
        if not isinstance(text, str) or len(text) > MAX_WRITE_CHARS:
            raise ProcessToolError("process_input_invalid")
        if not isinstance(append_newline, bool):
            raise ProcessToolError("process_input_invalid")
        payload = (text + ("\n" if append_newline else "")).encode("utf-8")
        try:
            if process.stdin is None:
                raise BrokenPipeError
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ProcessToolError("process_stdin_closed") from exc
        return {**self._status(str(process_id)), "written_bytes": len(payload)}

    def read(
        self,
        process_id: object,
        offset: object = 0,
        max_bytes: object = 65_536,
    ) -> dict[str, Any]:
        row = self._row(process_id)
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= MAX_READ_BYTES
        ):
            raise ProcessToolError("process_read_range_invalid")
        path: Path = row["path"]
        try:
            size = path.stat().st_size
            with path.open("rb") as stream:
                stream.seek(min(offset, size))
                payload = stream.read(max_bytes)
        except OSError as exc:
            raise ProcessToolError("process_output_unavailable") from exc
        next_offset = min(offset, size) + len(payload)
        return {
            **self._status(str(process_id)),
            "output": payload.decode("utf-8", errors="replace"),
            "offset": min(offset, size),
            "next_offset": next_offset,
            "available_bytes": size,
            "truncated": next_offset < size,
        }

    def close(self, process_id: object) -> dict[str, Any]:
        row = self._row(process_id)
        process: subprocess.Popen[bytes] = row["process"]
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        _terminate_process_group(process)
        return self._status(str(process_id))

    def cleanup(self) -> None:
        for process_id in tuple(self._rows):
            row = self._rows[process_id]
            process: subprocess.Popen[bytes] = row["process"]
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            _terminate_process_group(process)
            try:
                row["log"].close()
            except OSError:
                pass
        shutil.rmtree(self._root, ignore_errors=True)

    def _row(self, process_id: object) -> dict[str, Any]:
        if not isinstance(process_id, str) or PROCESS_ID.fullmatch(process_id) is None:
            raise ProcessToolError("process_id_invalid")
        try:
            return self._rows[process_id]
        except KeyError as exc:
            raise ProcessToolError("process_not_found") from exc

    def _status(self, process_id: str) -> dict[str, Any]:
        process: subprocess.Popen[bytes] = self._rows[process_id]["process"]
        exit_code = process.poll()
        return {
            "process_id": process_id,
            "status": "running" if exit_code is None else "exited",
            "exit_code": exit_code,
        }


def _argv(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_ARGV_ITEMS
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > MAX_ARG_CHARS
            or "\x00" in item
            for item in value
        )
    ):
        raise ProcessToolError("process_argv_invalid")
    if sum(len(item.encode("utf-8")) for item in value) > MAX_TOTAL_ARG_BYTES:
        raise ProcessToolError("process_argv_invalid")
    return list(value)


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _executable(value: str) -> str:
    candidate = value if "/" in value else shutil.which(value)
    if not candidate:
        raise ProcessToolError("process_executable_invalid")
    try:
        resolved = Path(candidate).resolve(strict=True)
        mode = os.stat(resolved).st_mode
    except OSError as exc:
        raise ProcessToolError("process_executable_invalid") from exc
    if not _within(resolved, EXECUTABLE_ROOTS) or not stat.S_ISREG(mode) or not os.access(resolved, os.X_OK):
        raise ProcessToolError("process_executable_invalid")
    return str(resolved)


def _working_directory(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ProcessToolError("process_cwd_invalid")
    path = Path(value)
    if not path.is_absolute():
        path = Path("/workspace") / path
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProcessToolError("process_cwd_invalid") from exc
    if not _within(resolved, WORKING_ROOTS) or not resolved.is_dir():
        raise ProcessToolError("process_cwd_invalid")
    return str(resolved)


def _child_environment() -> dict[str, str]:
    blocked = re.compile(
        r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|ANTHROPIC)",
        re.IGNORECASE,
    )
    return {
        key: value for key, value in os.environ.items()
        if blocked.search(key) is None
    }


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + 2.0
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


TOOLS = ({
    "name": "process_open",
    "description": "Start an interactive argv process owned by this Turn.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_ARGV_ITEMS},
            "cwd": {"type": "string", "default": "/workspace"},
        },
        "required": ["argv"],
        "additionalProperties": False,
    },
}, {
    "name": "process_write",
    "description": "Write text to a managed process stdin.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "process_id": {"type": "string"},
            "text": {"type": "string"},
            "append_newline": {"type": "boolean", "default": True},
        },
        "required": ["process_id", "text"],
        "additionalProperties": False,
    },
}, {
    "name": "process_read",
    "description": "Read bounded combined output and process status.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "process_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_READ_BYTES, "default": 65536},
        },
        "required": ["process_id"],
        "additionalProperties": False,
    },
}, {
    "name": "process_close",
    "description": "Close stdin and terminate an owned interactive process.",
    "inputSchema": {
        "type": "object",
        "properties": {"process_id": {"type": "string"}},
        "required": ["process_id"],
        "additionalProperties": False,
    },
})


def _reply(identifier: object, result: object = None, error: dict[str, Any] | None = None) -> None:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier}
    value["error" if error is not None else "result"] = error if error is not None else result
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _handle(registry: ProcessRegistry, message: dict[str, Any]) -> None:
    method = message.get("method")
    identifier = message.get("id")
    if method == "initialize":
        params = message.get("params")
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        _reply(identifier, {
            "protocolVersion": requested or "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "chatds-process", "version": "1.0.0"},
        })
        return
    if method == "ping":
        _reply(identifier, {})
        return
    if method == "tools/list":
        _reply(identifier, {"tools": list(TOOLS)})
        return
    if method != "tools/call":
        if identifier is not None:
            _reply(identifier, error={"code": -32601, "message": "method_not_found"})
        return
    params = message.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    arguments = params.get("arguments") if isinstance(params, dict) else None
    if not isinstance(arguments, dict):
        _reply(identifier, error={"code": -32602, "message": "invalid_tool_call"})
        return
    try:
        if name == "process_open":
            result = registry.open(arguments.get("argv"), arguments.get("cwd", "/workspace"))
        elif name == "process_write":
            result = registry.write(arguments.get("process_id"), arguments.get("text"), arguments.get("append_newline", True))
        elif name == "process_read":
            result = registry.read(arguments.get("process_id"), arguments.get("offset", 0), arguments.get("max_bytes", 65536))
        elif name == "process_close":
            result = registry.close(arguments.get("process_id"))
        else:
            raise ProcessToolError("process_tool_invalid")
        _reply(identifier, {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, separators=(",", ":"))}],
            "structuredContent": result,
            "isError": False,
        })
    except ProcessToolError as exc:
        _reply(identifier, {
            "content": [{"type": "text", "text": str(exc)}],
            "isError": True,
        })


def main() -> int:
    registry = ProcessRegistry()
    try:
        for line in sys.stdin:
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                _handle(registry, value)
    finally:
        registry.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
