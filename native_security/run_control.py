"""Durable, engine-neutral control mailbox for one native Agent run.

The Web/Session boundary writes immutable requests into a root-owned run
directory.  A thin native runner adapter acknowledges a request only after it
has handed the exact command to the upstream runtime.  The mailbox therefore
records delivery, not an optimistic HTTP acknowledgement.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Literal


RUN_CONTROL_SCHEMA = "chatds.native-run-control.v1"
RUN_CONTROL_RECEIPT_SCHEMA = "chatds.native-run-control-receipt.v1"
RUN_CONTROL_ACTIONS = frozenset({"interrupt", "followup", "steer"})
RUN_CONTROL_STATUSES = frozenset({"delivered", "rejected"})
MAX_RUN_CONTROLS = 4096
MAX_RUN_CONTROL_TEXT_CHARS = 2_000_000
MAX_RUN_CONTROL_BYTES = 8 * 1024 * 1024
SAFE_CONTROL_ID = re.compile(r"^[0-9a-f]{32}$")


class RunControlError(ValueError):
    """A static, non-secret run-control contract failure."""


def _canonical_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RunControlError("run_control_json_invalid") from exc
    if len(encoded) > MAX_RUN_CONTROL_BYTES:
        raise RunControlError("run_control_too_large")
    return encoded


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def build_run_control(
    *,
    control_id: str,
    seq: int,
    action: Literal["interrupt", "followup", "steer"] | str,
    text: str | None = None,
) -> dict[str, Any]:
    action = str(action)
    value: dict[str, Any] = {
        "schema": RUN_CONTROL_SCHEMA,
        "control_id": str(control_id),
        "seq": seq,
        "action": action,
        "text": text,
    }
    validate_run_control(value)
    return value


def validate_run_control(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "control_id", "seq", "action", "text",
    }:
        raise RunControlError("run_control_schema_invalid")
    control_id = value.get("control_id")
    seq = value.get("seq")
    action = value.get("action")
    text = value.get("text")
    if (
        value.get("schema") != RUN_CONTROL_SCHEMA
        or not isinstance(control_id, str)
        or SAFE_CONTROL_ID.fullmatch(control_id) is None
        or type(seq) is not int
        or not 1 <= seq <= MAX_RUN_CONTROLS
        or action not in RUN_CONTROL_ACTIONS
    ):
        raise RunControlError("run_control_schema_invalid")
    if action == "interrupt":
        if text is not None:
            raise RunControlError("run_control_text_invalid")
    elif (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > MAX_RUN_CONTROL_TEXT_CHARS
        or "\x00" in text
    ):
        raise RunControlError("run_control_text_invalid")
    _canonical_bytes(value)
    return dict(value)


def build_run_control_receipt(
    request: dict[str, Any],
    *,
    status: Literal["delivered", "rejected"] | str,
    code: str | None = None,
) -> dict[str, Any]:
    request = validate_run_control(request)
    value = {
        "schema": RUN_CONTROL_RECEIPT_SCHEMA,
        "control_id": request["control_id"],
        "seq": request["seq"],
        "action": request["action"],
        "request_sha256": canonical_sha256(request),
        "status": str(status),
        "code": code,
        "recorded_at_unix_ms": int(time.time() * 1000),
    }
    validate_run_control_receipt(value, request=request)
    return value


def validate_run_control_receipt(
    value: object,
    *,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "control_id", "seq", "action", "request_sha256",
        "status", "code", "recorded_at_unix_ms",
    }:
        raise RunControlError("run_control_receipt_schema_invalid")
    code = value.get("code")
    recorded_at = value.get("recorded_at_unix_ms")
    if (
        value.get("schema") != RUN_CONTROL_RECEIPT_SCHEMA
        or not isinstance(value.get("control_id"), str)
        or SAFE_CONTROL_ID.fullmatch(str(value["control_id"])) is None
        or type(value.get("seq")) is not int
        or not 1 <= int(value["seq"]) <= MAX_RUN_CONTROLS
        or value.get("action") not in RUN_CONTROL_ACTIONS
        or not isinstance(value.get("request_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(value["request_sha256"])) is None
        or value.get("status") not in RUN_CONTROL_STATUSES
        or (code is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", str(code)) is None)
        or type(recorded_at) is not int
        or recorded_at < 0
    ):
        raise RunControlError("run_control_receipt_schema_invalid")
    if value.get("status") == "delivered" and code is not None:
        raise RunControlError("run_control_receipt_schema_invalid")
    if request is not None:
        request = validate_run_control(request)
        if (
            value.get("control_id") != request["control_id"]
            or value.get("seq") != request["seq"]
            or value.get("action") != request["action"]
            or value.get("request_sha256") != canonical_sha256(request)
        ):
            raise RunControlError("run_control_receipt_mismatch")
    _canonical_bytes(value)
    return dict(value)


def _atomic_create_json(
    path: Path,
    value: object,
    *,
    mode: int = 0o600,
    parent_mode: int = 0o700,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=parent_mode)
    os.chmod(path.parent, parent_mode)
    payload = _canonical_bytes(value) + b"\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        mode,
    )
    try:
        if owner_uid is not None or owner_gid is not None:
            os.fchown(
                descriptor,
                -1 if owner_uid is None else owner_uid,
                -1 if owner_gid is None else owner_gid,
            )
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        info = os.lstat(path)
        if not path.is_file() or path.is_symlink() or info.st_nlink != 1:
            raise RunControlError("run_control_object_unsafe")
        data = path.read_bytes()
        if not data or len(data) > MAX_RUN_CONTROL_BYTES:
            raise RunControlError("run_control_object_size_invalid")
        value = json.loads(data)
    except RunControlError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RunControlError("run_control_object_invalid") from exc
    if not isinstance(value, dict):
        raise RunControlError("run_control_object_invalid")
    return value


def request_path(control_root: Path, control_id: str) -> Path:
    if SAFE_CONTROL_ID.fullmatch(control_id) is None:
        raise RunControlError("run_control_id_invalid")
    return control_root / "requests" / f"{control_id}.json"


def receipt_path(control_root: Path, control_id: str) -> Path:
    if SAFE_CONTROL_ID.fullmatch(control_id) is None:
        raise RunControlError("run_control_id_invalid")
    return control_root / "receipts" / f"{control_id}.json"


def read_run_control(path: Path) -> dict[str, Any]:
    return validate_run_control(_read_json(path))


def read_run_control_receipt(
    path: Path,
    *,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return validate_run_control_receipt(_read_json(path), request=request)


def enqueue_run_control(
    control_root: Path,
    request: dict[str, Any],
    *,
    mode: int = 0o600,
    parent_mode: int = 0o700,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> bool:
    """Create an immutable request; return True for an exact replay."""

    request = validate_run_control(request)
    path = request_path(control_root, request["control_id"])
    if path.exists():
        if read_run_control(path) != request:
            raise RunControlError("run_control_id_conflict")
        return True
    try:
        _atomic_create_json(
            path,
            request,
            mode=mode,
            parent_mode=parent_mode,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
    except FileExistsError:
        if read_run_control(path) != request:
            raise RunControlError("run_control_id_conflict")
        return True
    return False


def write_run_control_receipt(
    control_root: Path,
    request: dict[str, Any],
    *,
    status: Literal["delivered", "rejected"] | str,
    code: str | None = None,
) -> dict[str, Any]:
    request = validate_run_control(request)
    receipt = build_run_control_receipt(request, status=status, code=code)
    path = receipt_path(control_root, request["control_id"])
    if path.exists():
        existing = read_run_control_receipt(path, request=request)
        # Timestamps are deliberately excluded from idempotency comparison.
        comparable = {key: value for key, value in receipt.items() if key != "recorded_at_unix_ms"}
        prior = {key: value for key, value in existing.items() if key != "recorded_at_unix_ms"}
        if comparable != prior:
            raise RunControlError("run_control_receipt_conflict")
        return existing
    try:
        _atomic_create_json(path, receipt)
    except FileExistsError:
        return read_run_control_receipt(path, request=request)
    return receipt


def list_run_controls(control_root: Path) -> list[dict[str, Any]]:
    directory = control_root / "requests"
    try:
        paths = list(directory.iterdir())
    except FileNotFoundError:
        return []
    if len(paths) > MAX_RUN_CONTROLS:
        raise RunControlError("run_control_count_exceeded")
    values: list[dict[str, Any]] = []
    for path in paths:
        if path.name.startswith("."):
            continue
        if not path.name.endswith(".json") or SAFE_CONTROL_ID.fullmatch(path.stem) is None:
            raise RunControlError("run_control_filename_invalid")
        value = read_run_control(path)
        if value["control_id"] != path.stem:
            raise RunControlError("run_control_filename_mismatch")
        values.append(value)
    values.sort(key=lambda item: (item["seq"], item["control_id"]))
    if len({item["seq"] for item in values}) != len(values):
        raise RunControlError("run_control_seq_conflict")
    return values


def next_run_control_seq(control_root: Path) -> int:
    controls = list_run_controls(control_root)
    seq = (controls[-1]["seq"] + 1) if controls else 1
    if seq > MAX_RUN_CONTROLS:
        raise RunControlError("run_control_count_exceeded")
    return seq
