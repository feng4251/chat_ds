"""Minimal, dependency-free bootstrap for the isolated Claude Turn runtime.

The production ENTRYPOINT executes this file with ``python -I``.  Keeping this
boundary on the standard library means a broken or incomplete installed Turn
runtime can still publish one bounded, machine-owned terminal instead of
disappearing before the durable event ledger exists.
"""

from __future__ import annotations

import json
import os
import re
import runpy
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable


IMAGE_SELF_TEST_ARGUMENT = "--chatds-image-self-test"
_RUN_LEDGER = re.compile(
    r"^/state/control/runs/(?P<run_id>[0-9a-f]{32})/events\.jsonl$"
)
_RUNTIME_MODULE = "claude_runner.runner_entrypoint"
_SELF_TEST_MODULE = "claude_runner.image_selftest"


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, (ImportError, ModuleNotFoundError, SyntaxError)):
        return "runner_runtime_import_failed"
    return "runner_bootstrap_failed"


def _existing_terminal(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        metadata = path.stat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 64 * 1024 * 1024
        ):
            return True
        for raw_line in path.read_bytes().splitlines():
            envelope = json.loads(raw_line)
            event = envelope.get("event") if isinstance(envelope, dict) else None
            if (
                isinstance(event, dict)
                and event.get("type") == "chatds.supervisor.terminal"
            ):
                return True
    except (OSError, ValueError, TypeError):
        return True
    return False


def _append_bootstrap_terminal(path: Path, *, code: str) -> bool:
    """Append one fixed terminal only to the controller-owned run ledger."""

    match = _RUN_LEDGER.fullmatch(path.as_posix())
    if match is None or code not in {
        "runner_runtime_import_failed",
        "runner_bootstrap_failed",
    }:
        return False
    try:
        if _existing_terminal(path):
            return False
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        seq = 1
        if path.exists():
            lines = path.read_bytes().splitlines()
            if lines:
                last = json.loads(lines[-1])
                seq = int(last.get("seq") or 0) + 1
        envelope: dict[str, Any] = {
            "seq": seq,
            "received_at_unix_ms": int(time.time() * 1000),
            "channel": "bootstrap",
            "event": {
                "type": "chatds.supervisor.terminal",
                "status": "failed",
                "error": code,
                "error_code": code,
                "error_stage": "bootstrap_import",
                "exit_code": 70,
            },
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            payload = json.dumps(
                envelope,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise OSError("short bootstrap ledger write")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True
    except (OSError, ValueError, TypeError, OverflowError):
        return False


def _emit_fatal(code: str) -> None:
    sys.stderr.write(json.dumps(
        {"type": "chatds.runner.fatal", "code": code},
        separators=(",", ":"),
    ) + "\n")
    sys.stderr.flush()


def main(
    argv: list[str] | None = None,
    *,
    module_runner: Callable[..., dict[str, Any]] = runpy.run_module,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    module = (
        _SELF_TEST_MODULE
        if arguments == [IMAGE_SELF_TEST_ARGUMENT]
        else _RUNTIME_MODULE
    )
    try:
        module_runner(module, run_name="__main__")
    except SystemExit as exc:
        value = exc.code
        return int(value) if isinstance(value, int) else (0 if value is None else 1)
    except Exception as exc:
        code = _failure_code(exc)
        ledger = os.environ.get("CHATDS_EVENT_LEDGER", "")
        if ledger:
            _append_bootstrap_terminal(Path(ledger), code=code)
        _emit_fatal(code)
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
