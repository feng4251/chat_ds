"""Tools backed by the authenticated internal session control plane."""

from __future__ import annotations

import json
from typing import Any

import httpx

from config import settings


def _headers() -> dict[str, str]:
    return {"X-Internal-Token": settings.internal_api_token}


async def _request(method: str, path: str, **kwargs) -> str:
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.request(
                method,
                settings.backend_internal_url.rstrip("/") + path,
                headers={**_headers(), **kwargs.pop("headers", {})},
                **kwargs,
            )
        data: Any
        try:
            data = response.json()
        except ValueError:
            data = {"error": response.text[:1000]}
        if response.status_code >= 400:
            return json.dumps({
                "error": data.get("detail") if isinstance(data, dict) else data,
                "status_code": response.status_code,
            }, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({
            "error": f"Backend control plane unavailable: {type(exc).__name__}: {exc}"
        }, ensure_ascii=False)


async def sessions_list(
    search: str = "",
    limit: int = 20,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    return await _request(
        "GET",
        "/internal/sessions",
        params={
            "user_id": user_id,
            "current_session_id": session_id,
            "search": search or None,
            "limit": max(1, min(int(limit), 100)),
        },
    )


async def sessions_history(
    target_session_id: str,
    limit: int = 30,
    include_tools: bool = False,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    del session_id
    return await _request(
        "GET",
        f"/internal/sessions/{target_session_id}/history",
        params={
            "user_id": user_id,
            "limit": max(1, min(int(limit), 200)),
            "include_tools": include_tools,
        },
    )


async def sessions_fork(
    title: str = "",
    include_messages: bool = True,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    return await _request(
        "POST",
        f"/internal/sessions/{session_id}/fork",
        params={
            "user_id": user_id,
            "source_session_id": session_id,
        },
        json={"title": title or None, "include_messages": include_messages},
    )


async def sessions_send(
    target_session_id: str,
    content: str,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    return await _request(
        "POST",
        f"/internal/sessions/{target_session_id}/messages",
        params={
            "user_id": user_id,
            "source_session_id": session_id,
        },
        json={"content": content},
    )


async def session_status(
    target_session_id: str = "",
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    target = target_session_id or session_id
    return await _request(
        "GET",
        f"/internal/sessions/{target}/status",
        params={"user_id": user_id},
    )


async def get_goal(
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    return await _request(
        "GET",
        f"/internal/sessions/{session_id}/goal",
        params={"user_id": user_id},
    )


async def create_goal(
    objective: str,
    token_budget: int | None = None,
    note: str = "",
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    return await _request(
        "POST",
        f"/internal/sessions/{session_id}/goal",
        params={"user_id": user_id},
        json={
            "action": "create",
            "objective": objective,
            "token_budget": token_budget,
            "note": note or None,
        },
    )


async def update_goal(
    status: str,
    note: str = "",
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    if status not in {"complete", "blocked"}:
        return json.dumps({"error": "status must be complete or blocked"})
    return await _request(
        "POST",
        f"/internal/sessions/{session_id}/goal",
        params={"user_id": user_id},
        json={"action": status, "note": note or None},
    )


async def cronjob(
    action: str,
    name: str = "",
    prompt: str = "",
    schedule: str = "",
    job_id: str = "",
    timezone: str = "UTC",
    model_id: str = "",
    enabled_tools: list[str] | None = None,
    enabled: bool | None = None,
    delete_after_run: bool = False,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    if action == "list":
        return await _request(
            "GET",
            "/internal/schedules",
            params={"user_id": user_id, "conversation_id": session_id},
        )
    if action == "create":
        if not name or not prompt or not schedule:
            return json.dumps({"error": "name, prompt, and schedule are required"})
        return await _request(
            "POST",
            "/internal/schedules",
            params={
                "user_id": user_id,
                "source_session_id": session_id,
            },
            json={
                "name": name,
                "prompt": prompt,
                "schedule": schedule,
                "conversation_id": session_id,
                "timezone": timezone,
                "model_id": model_id or None,
                "enabled_tools": enabled_tools,
                "delete_after_run": delete_after_run,
            },
        )
    if not job_id:
        return json.dumps({"error": "job_id is required for this action"})
    if action == "remove":
        return await _request(
            "DELETE", f"/internal/schedules/{job_id}",
            params={
                "user_id": user_id,
                "source_session_id": session_id,
            },
        )
    if action == "trigger":
        return await _request(
            "POST", f"/internal/schedules/{job_id}/run",
            params={
                "user_id": user_id,
                "source_session_id": session_id,
            },
        )
    if action in {"pause", "resume"}:
        return await _request(
            "PATCH", f"/internal/schedules/{job_id}",
            params={
                "user_id": user_id,
                "source_session_id": session_id,
            },
            json={"enabled": action == "resume"},
        )
    if action == "update":
        payload = {
            key: value for key, value in {
                "name": name or None,
                "prompt": prompt or None,
                "schedule": schedule or None,
                "timezone": timezone or None,
                "model_id": model_id or None,
                "enabled_tools": enabled_tools,
                "enabled": enabled,
                "delete_after_run": delete_after_run,
            }.items() if value is not None
        }
        return await _request(
            "PATCH", f"/internal/schedules/{job_id}",
            params={
                "user_id": user_id,
                "source_session_id": session_id,
            },
            json=payload,
        )
    return json.dumps({"error": f"Unknown action: {action}"})


SESSIONS_LIST_SCHEMA = {
    "name": "sessions_list",
    "description": "List this user's sessions with title, preview, usage, and goal state.",
    "parameters": {
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "Optional title search."},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
        },
    },
}

SESSIONS_HISTORY_SCHEMA = {
    "name": "sessions_history",
    "description": "Read a bounded, user-scoped transcript from another session.",
    "parameters": {
        "type": "object",
        "properties": {
            "target_session_id": {"type": "string"},
            "limit": {"type": "integer", "default": 30, "minimum": 1, "maximum": 200},
            "include_tools": {"type": "boolean", "default": False},
        },
        "required": ["target_session_id"],
    },
}

SESSIONS_FORK_SCHEMA = {
    "name": "sessions_fork",
    "description": "Fork the current session, copying its workspace and optionally its transcript.",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "include_messages": {"type": "boolean", "default": True},
        },
    },
}

SESSIONS_SEND_SCHEMA = {
    "name": "sessions_send",
    "description": (
        "Queue a user-visible context message into another session owned by the "
        "same user. This does not start a model run in the target session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_session_id": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["target_session_id", "content"],
    },
}

SESSION_STATUS_SCHEMA = {
    "name": "session_status",
    "description": "Inspect model, tools, goal, message count, and usage for a session.",
    "parameters": {
        "type": "object",
        "properties": {
            "target_session_id": {
                "type": "string",
                "description": "Defaults to the current session.",
            },
        },
    },
}

GET_GOAL_SCHEMA = {
    "name": "get_goal",
    "description": "Read the current session's durable goal and token usage.",
    "parameters": {"type": "object", "properties": {}},
}

CREATE_GOAL_SCHEMA = {
    "name": "create_goal",
    "description": (
        "Create a durable session goal only when the user explicitly asks to set "
        "or pursue a persistent goal."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "objective": {"type": "string"},
            "token_budget": {"type": "integer", "minimum": 1},
            "note": {"type": "string"},
        },
        "required": ["objective"],
    },
}

UPDATE_GOAL_SCHEMA = {
    "name": "update_goal",
    "description": "Mark the current goal complete or genuinely blocked.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["complete", "blocked"]},
            "note": {"type": "string"},
        },
        "required": ["status"],
    },
}

CRONJOB_SCHEMA = {
    "name": "cronjob",
    "description": (
        "Create and manage scheduled tasks scoped to the current session workspace. "
        "Actions: create, list, update, pause, resume, trigger, remove."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "update", "pause", "resume", "trigger", "remove"],
            },
            "name": {"type": "string"},
            "prompt": {"type": "string"},
            "schedule": {
                "type": "string",
                "description": "Examples: 30m, every 2h, ISO timestamp, or cron expression.",
            },
            "job_id": {"type": "string"},
            "timezone": {"type": "string", "default": "UTC"},
            "model_id": {"type": "string"},
            "enabled_tools": {"type": "array", "items": {"type": "string"}},
            "enabled": {"type": "boolean"},
            "delete_after_run": {"type": "boolean", "default": False},
        },
        "required": ["action"],
    },
}
