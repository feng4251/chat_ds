"""Durable approval-receipt helpers shared by the supervisor and tests.

This module intentionally has no Docker, FastAPI, or provider dependency.  It
only validates and appends controller-owned JSONL receipts; the native harness
continues to own approval prompting and settlement.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterator


MAX_CONTROL_DECISION_LINE_BYTES = 64 * 1024
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def lower_native_question_answer(
    native_data: object,
    answer: object,
) -> tuple[list[str], str | None]:
    """Lower one browser answer into DSH's exact native answer vocabulary."""

    if not isinstance(native_data, dict) or not isinstance(answer, str):
        raise ValueError("native_question_request_invalid")
    question = native_data.get("question")
    raw_options = native_data.get("options")
    if (
        not isinstance(question, str)
        or not question
        or len(question) > 4_000
        or not isinstance(raw_options, list)
        or not 2 <= len(raw_options) <= 4
    ):
        raise ValueError("native_question_request_invalid")
    labels: list[str] = []
    for row in raw_options:
        if not isinstance(row, dict):
            raise ValueError("native_question_request_invalid")
        label = row.get("label")
        if (
            not isinstance(label, str)
            or not label
            or len(label) > 256
            or label in labels
        ):
            raise ValueError("native_question_request_invalid")
        labels.append(label)
    if answer in labels:
        return [answer], None
    if native_data.get("multi_select") is True:
        parts = answer.split(", ")
        if (
            len(parts) == len(set(parts))
            and 1 <= len(parts) <= 4
            and all(part in labels for part in parts)
        ):
            return parts, None
    return [], answer or None


def _jsonl_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Yield complete, bounded JSON objects and ignore damaged audit rows."""

    if not path.exists():
        return
    try:
        with path.open("rb") as stream:
            for raw in stream:
                if len(raw) > MAX_CONTROL_DECISION_LINE_BYTES or not raw.endswith(b"\n"):
                    continue
                try:
                    value = json.loads(raw)
                except (UnicodeError, ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def answerable_control_request(
    path: Path,
    request_id: str,
) -> tuple[int, dict[str, Any]] | None:
    """Return the latest root request bound to one Web-answerable waiter."""

    found: tuple[int, dict[str, Any]] | None = None
    for envelope in _jsonl_rows(path):
        event = envelope.get("event")
        native = event.get("session_event") if isinstance(event, dict) else None
        data = native.get("data") if isinstance(native, dict) else None
        seq = envelope.get("seq")
        answerable = (
            native.get("type") in {
                "chatds/approval/requested",
                "chatds/question/requested",
            }
        ) if isinstance(native, dict) else False
        if (
            type(seq) is int
            and seq > 0
            and isinstance(event, dict)
            and isinstance(native, dict)
            and event.get("type") == "deepseek.session.event"
            and int(event.get("delegation_depth") or 0) <= 0
            and answerable
            and isinstance(data, dict)
            and data.get("request_id") == request_id
        ):
            found = (seq * 1_000_000, dict(native))
    return found


def approval_request_seq(path: Path, request_id: str) -> int | None:
    """Return the projected sequence of one root, answerable control ask."""

    found = answerable_control_request(path, request_id)
    return found[0] if found is not None else None


def existing_control_decision(path: Path, request_id: str) -> dict[str, Any] | None:
    """Return the latest durable decision for ``request_id`` if one exists."""

    found: dict[str, Any] | None = None
    for row in _jsonl_rows(path):
        if row.get("request_id") == request_id:
            found = row
    return found


def append_control_decision(path: Path, row: dict[str, Any]) -> None:
    """Append and fsync one bounded, canonical decision receipt."""

    request_id = row.get("request_id")
    request_seq = row.get("request_seq")
    decision = row.get("decision")
    selected = row.get("selected")
    custom = row.get("custom")
    if (
        bool(set(row) - {
            "request_id", "request_seq", "decision", "selected", "custom",
        })
        or not isinstance(request_id, str)
        or SAFE_REQUEST_ID.fullmatch(request_id) is None
        or type(request_seq) is not int
        or request_seq < 1
        or decision not in {"allow", "deny"}
        or selected is not None
        and (
            not isinstance(selected, list)
            or len(selected) > 4
            or any(
                not isinstance(value, str) or len(value) > 4_000
                for value in selected
            )
        )
        or custom is not None
        and (not isinstance(custom, str) or len(custom) > 4_000)
    ):
        raise ValueError("invalid control decision")
    payload = json.dumps(
        row,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode() + b"\n"
    if len(payload) > MAX_CONTROL_DECISION_LINE_BYTES:
        raise ValueError("control decision is too large")
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0),
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
