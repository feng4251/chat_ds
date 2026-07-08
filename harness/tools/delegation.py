"""Isolated-context subtask delegation using the same session workspace."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from config import settings
from tools.context import ToolContext
from tools.registry import get_metadata


_BLOCKED_CHILD_TOOLS = {
    "delegate_task",
    "clarify",
    "memory",
    "cronjob",
    "create_goal",
    "update_goal",
    "sessions_fork",
    "sessions_send",
}


def _tool_allowed_in_child(name: str, *, parallel_child: bool) -> bool:
    if name in _BLOCKED_CHILD_TOOLS:
        return False
    metadata = get_metadata(name) or {}
    if metadata.get("allow_in_child") is False:
        return False
    if parallel_child and metadata.get("allow_in_parallel_child") is False:
        return False
    if metadata.get("mutates_global_state"):
        return False
    if metadata.get("requires_user_visibility"):
        return False
    return True


async def _run_child(
    task: dict[str, Any],
    context: ToolContext,
    index: int,
) -> dict:
    from agent_loop import run_stream

    goal = str(task.get("goal") or "").strip()
    extra = str(task.get("context") or "").strip()
    if not goal:
        return {"index": index, "status": "error", "error": "goal is required"}
    requested_tools = task.get("tools")
    parallel_child = bool(context.depth == 0)
    if isinstance(requested_tools, list):
        tools = [
            str(name) for name in requested_tools
            if str(name) in context.enabled_tools and _tool_allowed_in_child(str(name), parallel_child=parallel_child)
        ]
    else:
        tools = [
            name for name in context.enabled_tools
            if _tool_allowed_in_child(name, parallel_child=parallel_child)
        ]
    child_run_id = uuid.uuid4().hex
    parent_run_id = context.run_id
    root_run_id = context.root_run_id or context.run_id or child_run_id
    child_depth = int(context.depth or 0) + 1
    agent_name = str(task.get("agent_name") or f"delegate-{index + 1}").strip()
    workspace_scope = str(task.get("workspace_scope") or "shared_session")
    prompt = (
        "[Delegated Task]\n"
        f"Goal: {goal}\n\n"
        + (f"Context supplied by the parent:\n{extra}\n\n" if extra else "")
        + "Work independently. Use the shared session workspace when needed. "
          "Return a concise report with findings, changes, verification, and blockers. "
          "Do not ask the user questions and do not modify persistent memory or goals."
    )
    content = ""
    reasoning = ""
    tool_events: list[str] = []
    usage: dict[str, int] = {}
    error = None

    async def forward_event(event: dict) -> None:
        sink = context.event_sink
        if sink is None:
            return
        maybe = sink(event)
        if hasattr(maybe, "__await__"):
            await maybe

    spawn_event = {
        "type": "agent_event",
        "event_type": "agent.spawned",
        "run_id": child_run_id,
        "root_run_id": root_run_id,
        "parent_run_id": parent_run_id,
        "agent_kind": "delegate",
        "agent_name": agent_name,
        "depth": child_depth,
        "workspace_scope": workspace_scope,
        "seq": 0,
        "payload": {
            "index": index,
            "goal": goal,
            "requested_tools": requested_tools or [],
            "effective_tools": tools,
        },
    }
    await forward_event(spawn_event)

    async for event in run_stream(
        context.model_id,
        [{"role": "user", "content": prompt}],
        tools,
        user_id=context.user_id,
        session_id=context.session_id,
        max_iterations=max(1, min(
            int(task.get("max_iterations") or settings.delegation_max_iterations),
            30,
        )),
        provider_override=context.provider_config,
        fallback_overrides=list(context.fallback_configs),
        source="delegate",
        enabled_user_skills=list(context.enabled_user_skills),
        run_id=child_run_id,
        root_run_id=root_run_id,
        parent_run_id=parent_run_id,
        agent_kind="delegate",
        agent_name=agent_name,
        depth=child_depth,
        workspace_scope=workspace_scope,
        event_schema="chatds.agent.v2",
        event_sink=context.event_sink,
    ):
        if event["type"] == "delta":
            content += event.get("content", "")
        elif event["type"] == "reasoning_delta":
            reasoning += event.get("content", "")
        elif event["type"] == "tool_progress":
            tool_events.append(event.get("msg", ""))
        elif event["type"] == "usage":
            usage = {
                "input_tokens": int(event.get("input_tokens", 0) or 0),
                "output_tokens": int(event.get("output_tokens", 0) or 0),
                "total_tokens": int(event.get("total_tokens", 0) or 0),
            }
        elif event["type"] == "error":
            error = event.get("msg", "Unknown child error")
    return {
        "index": index,
        "goal": goal,
        "child_run_id": child_run_id,
        "agent_name": agent_name,
        "agent_kind": "delegate",
        "workspace_scope": workspace_scope,
        "status": "error" if error else "completed",
        "summary": content[-2000:] if content else "",
        "result": content,
        "result_excerpt": content[:2000] if content else "",
        "reasoning_summary": reasoning[-1000:] if reasoning else "",
        "tool_events": tool_events[-30:],
        "usage": usage,
        "error": error,
    }


async def delegate_task(
    goal: str = "",
    context_text: str = "",
    tasks: list[dict] | None = None,
    tools: list[str] | None = None,
    max_iterations: int | None = None,
    agent_name: str | None = None,
    workspace_scope: str = "shared_session",
    context: ToolContext | None = None,
) -> str:
    if context is None:
        return json.dumps({"error": "Runtime tool context is required."})
    batch = list(tasks or [])
    if goal:
        batch.insert(0, {
            "goal": goal,
            "context": context_text,
            "tools": tools,
            "max_iterations": max_iterations,
            "agent_name": agent_name,
            "workspace_scope": workspace_scope,
        })
    if not batch:
        return json.dumps({"error": "Provide goal or tasks."})
    max_concurrent = max(1, min(settings.delegation_max_concurrent, 6))
    if len(batch) > max_concurrent:
        return json.dumps({
            "error": f"At most {max_concurrent} delegated tasks may run in one batch."
        })
    results = await asyncio.gather(*[
        _run_child(task, context, index)
        for index, task in enumerate(batch)
    ])
    return json.dumps({"results": results}, ensure_ascii=False)


DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "Delegate one task or a small parallel batch to fresh-context child agents. "
        "Children share the current session workspace but cannot delegate, clarify, "
        "edit memory/goals, or schedule more work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "Single delegated objective."},
            "context_text": {
                "type": "string",
                "description": "All context the fresh child needs.",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional subset of parent-granted tools.",
            },
            "max_iterations": {"type": "integer", "minimum": 1, "maximum": 30},
            "agent_name": {
                "type": "string",
                "description": "Optional display name for the child agent.",
            },
            "workspace_scope": {
                "type": "string",
                "enum": ["shared_session"],
                "description": "Workspace mode for the child agent; currently shared_session.",
            },
            "tasks": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "context": {"type": "string"},
                        "tools": {"type": "array", "items": {"type": "string"}},
                        "agent_name": {"type": "string"},
                        "workspace_scope": {"type": "string", "enum": ["shared_session"]},
                        "max_iterations": {"type": "integer", "minimum": 1, "maximum": 30},
                    },
                    "required": ["goal"],
                },
            },
        },
    },
}
