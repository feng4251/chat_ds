"""Session-wide execution revocation for durable conversation deletion."""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from tools.execution_fence import (
    ChildExecutionFence,
    ExecutionAuthorityRevoked,
    bounded_cancel_tasks,
)
from tools.workspace_lock import (
    require_session_workspace_active,
)


@dataclass(frozen=True, slots=True)
class SessionExecutionToken:
    key: tuple[str, str]
    token: str


@dataclass(slots=True)
class _Entry:
    task: asyncio.Task
    fence: ChildExecutionFence


_LOCK = threading.Lock()
_GENERATIONS: dict[tuple[str, str], int] = {}
_ENTRIES: dict[tuple[str, str], dict[str, _Entry]] = {}


def _workspace_coordinate(user_id: str, session_id: str) -> Path:
    # Import dynamically so tests that replace the sandbox root exercise the
    # same durable fence coordinate as the file tools.
    from tools import path_security

    safe_user = path_security._safe_component(
        user_id,
        field="user_id",
    )
    safe_session = path_security._safe_component(
        session_id,
        field="session_id",
    )
    return (
        Path(path_security.SANDBOX_ROOT)
        / safe_user
        / safe_session
        / "workspace"
    )


def register_session_execution(
    user_id: str,
    session_id: str,
    fence: ChildExecutionFence,
) -> SessionExecutionToken:
    """Register the current root/child against one session generation."""

    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Session execution registration requires a task.")
    if not isinstance(fence, ChildExecutionFence):
        raise TypeError("Session execution requires a runtime-owned fence.")
    key = (str(user_id), str(session_id))
    with _LOCK:
        observed_generation = _GENERATIONS.get(key, 0)
    workspace = _workspace_coordinate(*key)
    require_session_workspace_active(workspace)
    token = uuid.uuid4().hex
    with _LOCK:
        if _GENERATIONS.get(key, 0) != observed_generation:
            raise ExecutionAuthorityRevoked(
                "Session execution generation changed during registration."
            )
        _ENTRIES.setdefault(key, {})[token] = _Entry(
            task=task,
            fence=fence,
        )
    try:
        # Close the publish-vs-register race. If deletion linearized after the
        # first check, unregister before any provider/tool dispatch.
        require_session_workspace_active(workspace)
    except BaseException:
        unregister_session_execution(
            SessionExecutionToken(key=key, token=token)
        )
        fence.revoke("session_deleted_during_registration")
        raise
    return SessionExecutionToken(key=key, token=token)


def unregister_session_execution(
    registration: SessionExecutionToken | None,
) -> None:
    if registration is None:
        return
    with _LOCK:
        entries = _ENTRIES.get(registration.key)
        if entries is None:
            return
        entries.pop(registration.token, None)
        if not entries:
            _ENTRIES.pop(registration.key, None)


async def revoke_session_executions(
    user_id: str,
    session_id: str,
    *,
    grace_seconds: float = 5.0,
) -> dict[str, int | bool]:
    """Atomically revoke, close, cancel, and boundedly drain one session."""

    key = (str(user_id), str(session_id))
    with _LOCK:
        _GENERATIONS[key] = _GENERATIONS.get(key, 0) + 1
        entries = list(_ENTRIES.get(key, {}).values())
    fences = {entry.fence for entry in entries}
    tasks = {
        entry.task
        for entry in entries
        if entry.task is not asyncio.current_task()
        and not entry.task.done()
    }
    for fence in fences:
        fence.revoke("session_deleted")

    close_tasks = {
        asyncio.create_task(
            fence.close_registered_resources(
                grace_seconds=max(0.05, min(float(grace_seconds), 30.0)),
            )
        )
        for fence in fences
    }
    close_failures = 0
    unacknowledged_resources = 0
    if close_tasks:
        close_results = await asyncio.gather(
            *close_tasks,
            return_exceptions=True,
        )
        for result in close_results:
            if isinstance(result, BaseException):
                close_failures += 1
                unacknowledged_resources += 1
                continue
            unacknowledged_resources += int(
                result.unacknowledged_resource_count
            )
            if (
                result.fence_coverage_proven is not True
                or result.cancellation_unacknowledged
                or result.unacknowledged_resource_count
            ):
                close_failures += 1
    residual = await bounded_cancel_tasks(
        tasks,
        grace_seconds=max(0.05, min(float(grace_seconds), 30.0)),
    )
    return {
        # Residual tasks are still fenced and supervised: they cannot cross a
        # provider/tool/commit boundary after revocation.
        "success": close_failures == 0,
        "registered_count": len(entries),
        "fence_count": len(fences),
        "cancelled_count": len(tasks) - len(residual),
        "residual_count": len(residual),
        "close_failure_count": close_failures,
        "unacknowledged_resource_count": unacknowledged_resources,
    }


def session_execution_snapshot(
    user_id: str,
    session_id: str,
) -> dict[str, int]:
    key = (str(user_id), str(session_id))
    with _LOCK:
        entries = list(_ENTRIES.get(key, {}).values())
        generation = _GENERATIONS.get(key, 0)
    return {
        "generation": generation,
        "registered_count": len(entries),
        "active_task_count": sum(
            1 for entry in entries if not entry.task.done()
        ),
    }
