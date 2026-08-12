#!/usr/bin/env python3
"""Receipt-only MCP adapter for ChatDS-owned persistent schedules.

The per-Turn container never owns a background scheduler.  This server only
validates a typed request and returns an accepted-pending receipt.  The Runner
binds that receipt to the native tool call and the Backend commits the exact
write under the current user/Session authority at the root terminal boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from typing import Any


MAX_NAME_CHARS = 128
MAX_PROMPT_CHARS = 40_000
MAX_SCHEDULE_CHARS = 256
MAX_TIMEZONE_CHARS = 64
MAX_RUNS = 10_000
MAX_ENABLED_TOOLS = 64
_SAFE_TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


def normalize_schedule_create(arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {
        "name", "prompt", "schedule", "timezone", "max_runs",
        "expires_at", "enabled_tools", "delete_after_run",
    }:
        raise ValueError("invalid_schedule_request")
    name = arguments.get("name")
    prompt = arguments.get("prompt")
    schedule = arguments.get("schedule")
    timezone = arguments.get("timezone", "UTC")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
        raise ValueError("invalid_schedule_name")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError("invalid_schedule_prompt")
    if not isinstance(schedule, str) or not schedule.strip() or len(schedule) > MAX_SCHEDULE_CHARS:
        raise ValueError("invalid_schedule_expression")
    if not isinstance(timezone, str) or not timezone.strip() or len(timezone) > MAX_TIMEZONE_CHARS:
        raise ValueError("invalid_schedule_timezone")
    max_runs = arguments.get("max_runs")
    if (
        max_runs is not None
        and (
            isinstance(max_runs, bool)
            or not isinstance(max_runs, int)
            or not 1 <= max_runs <= MAX_RUNS
        )
    ):
        raise ValueError("invalid_schedule_max_runs")
    expires_at = arguments.get("expires_at")
    if expires_at is not None and (
        not isinstance(expires_at, str)
        or not expires_at.strip()
        or len(expires_at) > 64
    ):
        raise ValueError("invalid_schedule_expiry")
    enabled_tools = arguments.get("enabled_tools")
    if enabled_tools is not None and (
        not isinstance(enabled_tools, list)
        or len(enabled_tools) > MAX_ENABLED_TOOLS
        or any(
            not isinstance(value, str) or _SAFE_TOOL.fullmatch(value) is None
            for value in enabled_tools
        )
        or len(set(enabled_tools)) != len(enabled_tools)
    ):
        raise ValueError("invalid_schedule_tools")
    delete_after_run = arguments.get("delete_after_run", False)
    if type(delete_after_run) is not bool:
        raise ValueError("invalid_schedule_delete_policy")
    return {
        "name": name.strip(),
        "prompt": prompt.strip(),
        "schedule": schedule.strip(),
        "timezone": timezone.strip(),
        "max_runs": max_runs,
        "expires_at": expires_at.strip() if isinstance(expires_at, str) else None,
        "enabled_tools": enabled_tools,
        "delete_after_run": delete_after_run,
    }


def _accepted_receipt(arguments: object) -> str:
    request = normalize_schedule_create(arguments)
    encoded = json.dumps(
        request, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return json.dumps({
        "schema": "chatds.schedule.accepted.v1",
        "status": "accepted_pending_terminal_commit",
        "request_sha256": hashlib.sha256(encoded).hexdigest(),
        "message": (
            "The exact schedule request was accepted by the Turn controller. "
            "It becomes active only after the authoritative ChatDS terminal commit."
        ),
    }, ensure_ascii=False, separators=(",", ":"))


TOOLS = [{
    "name": "schedule_create",
    "description": (
        "Create a persistent ChatDS scheduled task for the current Session. "
        "Use this instead of Claude CronCreate: the Claude process is "
        "per-Turn and cannot own background work. Preserve user-specified "
        "clock boundaries exactly; do not add jitter or shift minutes. Use an "
        "explicit IANA timezone. For a bounded request, set max_runs and/or "
        "expires_at so a recurring cron cannot silently continue forever."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": MAX_NAME_CHARS},
            "prompt": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS},
            "schedule": {
                "type": "string", "minLength": 1, "maxLength": MAX_SCHEDULE_CHARS,
                "description": "Five-field cron, duration, or absolute ISO-8601 time.",
            },
            "timezone": {"type": "string", "minLength": 1, "maxLength": MAX_TIMEZONE_CHARS},
            "max_runs": {"type": "integer", "minimum": 1, "maximum": MAX_RUNS},
            "expires_at": {
                "type": "string",
                "description": "Timezone-aware ISO-8601 upper execution boundary.",
            },
            "enabled_tools": {
                "type": "array", "maxItems": MAX_ENABLED_TOOLS,
                "items": {"type": "string"}, "uniqueItems": True,
            },
            "delete_after_run": {"type": "boolean", "default": False},
        },
        "required": ["name", "prompt", "schedule", "timezone"],
        "additionalProperties": False,
    },
}]


def _reply(identifier: object, result: object = None, error: dict | None = None) -> None:
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier}
    response["result" if error is None else "error"] = result if error is None else error
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    identifier = message.get("id")
    if method == "initialize":
        params = message.get("params")
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        _reply(identifier, {
            "protocolVersion": requested or "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "chatds-schedule", "version": "1.0.0"},
        })
    elif method == "ping":
        _reply(identifier, {})
    elif method == "tools/list":
        _reply(identifier, {"tools": TOOLS})
    elif method == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        arguments = params.get("arguments") if isinstance(params, dict) else None
        if name != "schedule_create":
            _reply(identifier, error={"code": -32602, "message": "invalid_tool_call"})
            return
        try:
            receipt = _accepted_receipt(arguments)
            _reply(identifier, {
                "content": [{"type": "text", "text": receipt}],
                "isError": False,
            })
        except (TypeError, ValueError) as exc:
            _reply(identifier, {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            })
    elif identifier is not None:
        _reply(identifier, error={"code": -32601, "message": "method_not_found"})


def main() -> int:
    for line in sys.stdin:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                _handle(value)
        except (ValueError, TypeError):
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
