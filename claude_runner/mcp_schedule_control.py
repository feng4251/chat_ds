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
import os
import re
import sys
from datetime import datetime
from typing import Any

try:
    from claude_runner.schedule_spec import (
        ScheduleSpecError,
        resolve_schedule_spec,
    )
except ModuleNotFoundError:  # Local source tests use the Backend source copy.
    from backend.schedule_spec import ScheduleSpecError, resolve_schedule_spec


MAX_NAME_CHARS = 128
MAX_PROMPT_CHARS = 40_000
MAX_SCHEDULE_CHARS = 256
MAX_TIMEZONE_CHARS = 64
MAX_RUNS = 10_000
MAX_PLATFORM_CAPABILITIES = 16
_SAFE_CAPABILITY = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
SCHEDULE_CAPABILITY_ALIASES_ENV = (
    "CHATDS_SCHEDULE_CAPABILITY_ALIASES_JSON"
)
_DEFAULT_PLATFORM_CAPABILITY_ALIASES = {
    "web_search": "web_search",
    "market_quote": "market_quote",
    "cronjob": "cronjob",
}
_ALIASES_UNSET = object()


def normalize_schedule_capability_aliases(
    value: object,
) -> dict[str, str]:
    """Validate a compiler-owned model-visible -> platform I/O map."""

    if not isinstance(value, dict) or len(value) > 256:
        raise ValueError("invalid_schedule_capability_aliases")
    normalized: dict[str, str] = {}
    for alias, canonical in value.items():
        if not isinstance(alias, str) or _SAFE_CAPABILITY.fullmatch(alias) is None:
            raise ValueError("invalid_schedule_capability_aliases")
        if (
            not isinstance(canonical, str)
            or _SAFE_CAPABILITY.fullmatch(canonical) is None
        ):
            raise ValueError("invalid_schedule_capability_aliases")
        normalized[alias] = canonical
    return dict(sorted(normalized.items()))


def schedule_capability_aliases_from_environment() -> dict[str, str]:
    raw = os.environ.get(SCHEDULE_CAPABILITY_ALIASES_ENV)
    if raw is None:
        return dict(_DEFAULT_PLATFORM_CAPABILITY_ALIASES)
    if len(raw.encode("utf-8")) > 32_768:
        raise ValueError("invalid_schedule_capability_aliases")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_schedule_capability_aliases") from exc
    return normalize_schedule_capability_aliases(value)


def normalize_schedule_create(
    arguments: object,
    *,
    capability_aliases: object = _ALIASES_UNSET,
) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {
        "name", "prompt", "schedule", "timezone", "max_runs",
        "expires_at", "platform_capabilities", "delete_after_run",
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
    platform_capabilities = arguments.get("platform_capabilities")
    if platform_capabilities is not None and (
        not isinstance(platform_capabilities, list)
        or len(platform_capabilities) > MAX_PLATFORM_CAPABILITIES
        or any(
            not isinstance(value, str)
            or _SAFE_CAPABILITY.fullmatch(value) is None
            for value in platform_capabilities
        )
        or len(set(platform_capabilities)) != len(platform_capabilities)
    ):
        raise ValueError("invalid_schedule_platform_capabilities")
    aliases = (
        schedule_capability_aliases_from_environment()
        if capability_aliases is _ALIASES_UNSET
        else normalize_schedule_capability_aliases(capability_aliases)
    )
    if platform_capabilities is not None:
        canonical_capabilities: list[str] = []
        observed: set[str] = set()
        for alias in platform_capabilities:
            if alias not in aliases:
                raise ValueError("invalid_schedule_platform_capabilities")
            canonical = aliases[alias]
            if canonical not in observed:
                observed.add(canonical)
                canonical_capabilities.append(canonical)
        platform_capabilities = canonical_capabilities
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
        "platform_capabilities": platform_capabilities,
        "delete_after_run": delete_after_run,
    }


def _accepted_receipt(
    arguments: object,
    *,
    now: datetime | None = None,
) -> str:
    request = normalize_schedule_create(arguments)
    # Recoverable semantic errors must be visible while the model can still
    # correct the tool call. The Backend repeats this exact validation at the
    # authoritative terminal transaction as a TOCTOU defense.
    resolve_schedule_spec(
        request["schedule"],
        request["timezone"],
        expires_at=request.get("expires_at"),
        now=now,
    )
    encoded = json.dumps(
        request, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return json.dumps({
        "schema": "chatds.schedule.accepted.v1",
        "status": "accepted_pending_terminal_commit",
        "request_sha256": hashlib.sha256(encoded).hexdigest(),
        "message": (
            "The exact schedule and its first eligible occurrence were "
            "validated and accepted by the Turn controller. "
            "It becomes active only after the authoritative ChatDS terminal commit."
        ),
    }, ensure_ascii=False, separators=(",", ":"))


_PLATFORM_CAPABILITY_ITEMS: dict[str, Any] = {
    "type": "string",
    "enum": sorted(schedule_capability_aliases_from_environment()),
}


TOOLS = [{
    "name": "schedule_create",
    "description": (
        "Create a persistent ChatDS scheduled task for the current Session. "
        "Use this instead of Claude CronCreate: the Claude process is "
        "per-Turn and cannot own background work. Preserve user-specified "
        "clock boundaries exactly; do not add jitter or shift minutes. Use an "
        "explicit IANA timezone. For a bounded request, set max_runs and/or "
        "expires_at so a recurring cron cannot silently continue forever. "
        "max_runs alone is sufficient for a count-bounded recurring request; "
        "omit expires_at unless the user also specified a clock boundary. "
        "The tool validates the first eligible occurrence against expires_at; "
        "if it rejects a stale or inconsistent boundary, correct the request "
        "and call it again."
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
            "platform_capabilities": {
                "type": "array", "maxItems": MAX_PLATFORM_CAPABILITIES,
                "items": _PLATFORM_CAPABILITY_ITEMS,
                "uniqueItems": True,
                "description": (
                    "Optional subset of compiler-published platform I/O "
                    "capabilities. Native Claude file, shell, Skill, and "
                    "agent tools are engine-owned and are not selected here."
                ),
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
        except ScheduleSpecError as exc:
            rejection = json.dumps({
                "schema": "chatds.schedule.rejected.v1",
                "status": "rejected",
                "code": exc.code,
                "message": str(exc),
                "correction_required": True,
            }, ensure_ascii=False, separators=(",", ":"))
            _reply(identifier, {
                "content": [{"type": "text", "text": rejection}],
                "isError": True,
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
