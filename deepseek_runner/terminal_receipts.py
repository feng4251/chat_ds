"""Dependency-free durable terminal receipts for the DeepSeek Supervisor."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


SAFE_ERROR_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def append_terminal(
    path: Path,
    status: str,
    error: str | None,
    *,
    error_stage: str | None = None,
) -> None:
    """Append at most one authoritative terminal with a stable failure stage."""

    if error_stage is not None and SAFE_ERROR_STAGE.fullmatch(error_stage) is None:
        raise RuntimeError("deepseek_terminal_error_stage_invalid")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line).get("event", {})
            except (ValueError, TypeError):
                continue
            if event.get("type") == "chatds.supervisor.terminal":
                return
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seq = int(json.loads(lines[-1]).get("seq") or 0) + 1 if lines else 1
    event = {
        "type": "chatds.supervisor.terminal",
        "status": status,
        "error": error,
    }
    if error_stage is not None:
        event["error_stage"] = error_stage
    envelope = {
        "seq": seq,
        "received_at_unix_ms": int(time.time() * 1000),
        "channel": "supervisor",
        "event": event,
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        payload = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode() + b"\n"
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            offset += os.write(fd, view[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
