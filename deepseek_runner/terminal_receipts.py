"""Dependency-free durable terminal receipts for the DeepSeek Supervisor."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


SAFE_ERROR_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def _scan_ledger(path: Path) -> tuple[bool, str | None, int, dict | None]:
    """Return terminal presence/status and the highest durable sequence.

    Event ledgers can be hundreds of megabytes after a long native Turn.  A
    controller receipt must therefore use a bounded streaming scan rather
    than materializing the entire lossless ledger in supervisor memory.
    """

    if not path.exists():
        return False, None, 0, None
    terminal_seen = False
    status: str | None = None
    last_sequence = 0
    terminal_envelope: dict | None = None
    try:
        with path.open("rb") as stream:
            for line in stream:
                try:
                    envelope = json.loads(line)
                except (UnicodeError, ValueError, TypeError):
                    continue
                if not isinstance(envelope, dict):
                    continue
                sequence = envelope.get("seq")
                if isinstance(sequence, int) and sequence > last_sequence:
                    last_sequence = sequence
                event = envelope.get("event")
                if not isinstance(event, dict):
                    continue
                if event.get("type") != "chatds.supervisor.terminal":
                    continue
                terminal_seen = True
                candidate = str(event.get("status") or "")
                if candidate in TERMINAL_STATUSES and status is None:
                    status = candidate
                    terminal_envelope = envelope
    except OSError:
        return False, None, 0, None
    return terminal_seen, status, last_sequence, terminal_envelope


def terminal_status(path: Path) -> str | None:
    """Read one authoritative terminal status with bounded memory."""

    return _scan_ledger(path)[1]


def terminal_receipt(path: Path) -> tuple[dict | None, int]:
    """Return the first valid terminal envelope and ledger high-water mark."""

    _seen, _status, last_sequence, envelope = _scan_ledger(path)
    return envelope, last_sequence


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
    terminal_seen, _status, last_sequence, _envelope = _scan_ledger(path)
    if terminal_seen:
        return
    seq = last_sequence + 1
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
