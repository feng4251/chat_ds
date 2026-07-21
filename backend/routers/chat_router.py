import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional, TypeVar
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings

logger = logging.getLogger(__name__)
from database import get_db, async_session
from models import (
    Artifact,
    User,
    Conversation,
    Message,
    CustomModelConfig,
    AgentRun,
    AgentRunEvent,
    TaskItem,
)
from schemas import ChatRequest
from workspace import ensure_workspace, safe_workspace_path, serialize_json_list, workspace_file_metadata
from hooks import emit_event
from native_tools import DEFAULT_NATIVE_TOOLS
from model_routing import (
    canonical_agent_model_id,
    filter_agentic_fallback_model_ids,
)
from auth import get_current_user

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Background tasks (keep references so they don't get GC'd before completion).
_background_tasks: set[asyncio.Task] = set()


class _ConversationTurnState:
    """In-process serialization and projection barrier for one conversation."""

    __slots__ = ("lock", "references", "projection_tasks")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.references = 0
        self.projection_tasks: set[asyncio.Task] = set()


class _ConversationTurnLease:
    """Single-release token for a retained conversation turn state."""

    __slots__ = ("state", "released")

    def __init__(self, state: _ConversationTurnState) -> None:
        self.state = state
        self.released = False


class _ConversationProjectionBarrierError(RuntimeError):
    pass


# A turn owns its conversation lock from before history is read until its
# terminal assistant/run projection is durable.  References include lock
# waiters, which prevents a release/wait race from replacing the state object.
_conversation_turn_states: dict[str, _ConversationTurnState] = {}


def _cleanup_conversation_turn_state(
    conv_id: str,
    state: _ConversationTurnState,
) -> None:
    if state.references or state.lock.locked() or state.projection_tasks:
        return
    if _conversation_turn_states.get(conv_id) is state:
        _conversation_turn_states.pop(conv_id, None)


async def _drain_conversation_projection_tasks(
    conv_id: str,
    state: _ConversationTurnState,
) -> None:
    """Wait for terminal projections left running by a disconnected client."""

    while True:
        pending = [task for task in state.projection_tasks if not task.done()]
        if not pending:
            return
        # The projection belongs to the conversation, not to the next request.
        # Shielding it ensures cancellation of that waiter cannot cancel the
        # durable write it is waiting for.
        results = await asyncio.gather(
            *(asyncio.shield(task) for task in pending),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise _ConversationProjectionBarrierError(
                    "A prior terminal conversation projection failed."
                ) from result
            if result is not True:
                raise _ConversationProjectionBarrierError(
                    "A prior terminal conversation projection was not durable."
                )


async def _acquire_conversation_turn(conv_id: str) -> _ConversationTurnLease:
    state = _conversation_turn_states.get(conv_id)
    if state is None:
        state = _ConversationTurnState()
        _conversation_turn_states[conv_id] = state
    state.references += 1
    acquired = False
    try:
        await state.lock.acquire()
        acquired = True
        # A disconnected predecessor releases the turn lock after registering
        # its shielded projection.  Drain that projection before reading any
        # history for this turn.
        await _drain_conversation_projection_tasks(conv_id, state)
        return _ConversationTurnLease(state)
    except BaseException:
        if acquired:
            state.lock.release()
        state.references -= 1
        _cleanup_conversation_turn_state(conv_id, state)
        raise


def _release_conversation_turn(
    conv_id: str,
    lease: _ConversationTurnLease,
) -> None:
    if lease.released:
        return
    lease.released = True
    state = lease.state
    if state.lock.locked():
        state.lock.release()
    state.references = max(0, state.references - 1)
    _cleanup_conversation_turn_state(conv_id, state)


def _track_conversation_projection(
    conv_id: str,
    operation: Awaitable[bool],
) -> asyncio.Task:
    state = _conversation_turn_states.get(conv_id)
    if state is None:
        # Keep standalone/internal callers safe as well.  Ordinary chat turns
        # already retained this state before arriving here.
        state = _ConversationTurnState()
        _conversation_turn_states[conv_id] = state
    task = asyncio.create_task(operation)
    state.projection_tasks.add(task)
    _background_tasks.add(task)

    def finish(completed: asyncio.Task) -> None:
        _background_tasks.discard(completed)
        state.projection_tasks.discard(completed)
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            error = None
        if error is not None:
            logger.error(
                "Uncaught terminal conversation projection failure conv=%s",
                conv_id,
                exc_info=(type(error), error, error.__traceback__),
            )
        _cleanup_conversation_turn_state(conv_id, state)

    task.add_done_callback(finish)
    return task

# SQLite permits only one writer at a time.  Agent events arrive in bursts and
# used to create one unconstrained writer task per event, which made both the
# child-run bootstrap row and the task/event projections race each other.  Keep
# ordering and backpressure local to a conversation while still allowing
# unrelated sessions to persist independently.
_agent_event_persist_locks: dict[str, asyncio.Lock] = {}
_agent_event_persist_tasks: dict[
    tuple[str, str], set[asyncio.Task]
] = {}
_SQLITE_PERSIST_MAX_ATTEMPTS = 4
_SQLITE_PERSIST_RETRY_BASE_SECONDS = 0.05
_PersistResult = TypeVar("_PersistResult")


def _agent_event_persist_lock(conv_id: str) -> asyncio.Lock:
    lock = _agent_event_persist_locks.get(conv_id)
    if lock is None:
        lock = asyncio.Lock()
        _agent_event_persist_locks[conv_id] = lock
    return lock


def _cleanup_agent_event_persist_lock(conv_id: str, lock: asyncio.Lock) -> None:
    if lock.locked():
        return
    if any(
        key[0] == conv_id and any(not task.done() for task in tasks)
        for key, tasks in _agent_event_persist_tasks.items()
    ):
        return
    if _agent_event_persist_locks.get(conv_id) is lock:
        _agent_event_persist_locks.pop(conv_id, None)


def _retryable_sqlite_persist_error(exc: BaseException) -> bool:
    message = str(exc).casefold()
    if isinstance(exc, OperationalError):
        return "database is locked" in message or "database table is locked" in message
    if isinstance(exc, IntegrityError):
        # A competing process may have committed the natural run/task/artifact
        # key after this transaction read its initial snapshot.  Retrying starts
        # a new transaction, whose ordinary idempotence checks then see it.
        return "unique constraint failed" in message
    return False


async def _run_sqlite_persist_with_retry(
    operation: Callable[[], Awaitable[_PersistResult]],
    *,
    description: str,
) -> _PersistResult:
    for attempt in range(1, _SQLITE_PERSIST_MAX_ATTEMPTS + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if (
                not _retryable_sqlite_persist_error(exc)
                or attempt >= _SQLITE_PERSIST_MAX_ATTEMPTS
            ):
                raise
            delay = _SQLITE_PERSIST_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Retrying %s after transient SQLite contention "
                "(attempt %s/%s, delay %.2fs): %s",
                description,
                attempt,
                _SQLITE_PERSIST_MAX_ATTEMPTS,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"Unreachable SQLite retry state for {description}")


def _event_payload(event: dict) -> dict:
    return event.get("payload") if isinstance(event.get("payload"), dict) else {}


def _event_key(event: dict, run_id: str) -> str:
    return f"{run_id}:{event.get('event_type') or 'unknown'}:{int(event.get('seq') or 0)}"


def _agent_event_terminal_status(
    agent_events: list[dict],
    *,
    run_id: str | None = None,
) -> tuple[str | None, str | None]:
    terminal_type = None
    terminal_error = None
    for event in sorted(agent_events or [], key=lambda item: int(item.get("seq") or 0)):
        if run_id is not None and str(event.get("run_id") or "") != run_id:
            continue
        event_type = str(event.get("event_type") or "")
        if event_type == "run.completed":
            terminal_type = event_type
            terminal_error = None
        elif event_type == "run.failed":
            terminal_type = event_type
            payload = _event_payload(event)
            terminal_error = str(payload.get("error") or "Agent run failed.")[:4000]
        elif event_type == "run.cancelled":
            terminal_type = event_type
            terminal_error = None
    if terminal_type == "run.failed":
        return "failed", terminal_error or "Agent run failed."
    if terminal_type == "run.completed":
        return "succeeded", None
    if terminal_type == "run.cancelled":
        return "cancelled", None
    return None, None


def _normalized_usage(value: object) -> dict[str, int]:
    """Return one non-negative, internally consistent usage snapshot."""

    payload = value if isinstance(value, dict) else {}
    normalized: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        try:
            normalized[key] = max(0, int(payload.get(key, 0) or 0))
        except (TypeError, ValueError, OverflowError):
            normalized[key] = 0
    normalized["total_tokens"] = max(
        normalized["total_tokens"],
        normalized["input_tokens"] + normalized["output_tokens"],
    )
    return normalized


def _reconciled_root_run_usage(
    usage: object,
    agent_events: list[dict],
    *,
    run_id: str,
) -> dict[str, int]:
    """Reconcile cumulative usage using events owned by the root run only.

    Event delivery and the standalone usage chunk are independent stream
    records.  A terminal event can therefore be durable even when the usage
    chunk that would normally follow it is lost.  Values are cumulative, so
    element-wise maxima are idempotent and cannot double-count replays.  Child
    run totals are deliberately excluded from the root conversation total.
    """

    reconciled = _normalized_usage(usage)
    for event in sorted(
        agent_events or [],
        key=lambda item: int(item.get("seq") or 0),
    ):
        if str(event.get("run_id") or "") != run_id:
            continue
        event_type = str(event.get("event_type") or "")
        payload = _event_payload(event)
        if event_type == "usage.updated":
            candidate = _normalized_usage(payload)
        elif event_type in {"run.completed", "run.failed", "run.cancelled"}:
            candidate = _normalized_usage(payload.get("usage"))
        else:
            continue
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            reconciled[key] = max(reconciled[key], candidate[key])
    reconciled["total_tokens"] = max(
        reconciled["total_tokens"],
        reconciled["input_tokens"] + reconciled["output_tokens"],
    )
    return reconciled


def _merge_monotonic_run_usage(run: AgentRun, value: object) -> None:
    """Project one cumulative per-run snapshot without allowing regressions."""

    current = _normalized_usage({
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
    })
    candidate = _normalized_usage(value)
    run.input_tokens = max(current["input_tokens"], candidate["input_tokens"])
    run.output_tokens = max(current["output_tokens"], candidate["output_tokens"])
    run.total_tokens = max(
        current["total_tokens"],
        candidate["total_tokens"],
        run.input_tokens + run.output_tokens,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _project_artifact_event(
    s: AsyncSession,
    *,
    conv_id: str,
    user_id: str,
    root_run_id: str,
    run_id: str,
    event: dict,
    payload: dict,
) -> None:
    if event.get("event_type") != "artifact.created":
        return
    source_event_key = str(payload.get("source_event_key") or _event_key(event, run_id))
    exists = (await s.execute(
        select(Artifact.id).where(
            Artifact.conversation_id == conv_id,
            Artifact.user_id == user_id,
            Artifact.source_event_key == source_event_key,
        )
    )).scalar_one_or_none()
    if exists:
        return
    path = str(payload.get("path") or "").strip() or None
    title = str(payload.get("title") or (Path(path).name if path else "artifact"))[:256]
    mime_type = payload.get("mime_type")
    preview_kind = payload.get("preview_kind")
    size_bytes = int(payload.get("size_bytes") or payload.get("size") or 0)
    sha256 = payload.get("sha256")
    if path:
        try:
            file_path = safe_workspace_path(user_id, conv_id, path, must_exist=True)
            if file_path.is_file():
                stat = file_path.stat()
                meta = workspace_file_metadata(file_path)
                mime_type = meta.get("mime_type") or mime_type
                preview_kind = meta.get("preview_kind") or preview_kind
                size_bytes = stat.st_size
                sha256 = _file_sha256(file_path)
        except (FileNotFoundError, ValueError, OSError):
            pass
    s.add(Artifact(
        id=uuid.uuid4().hex,
        user_id=user_id,
        conversation_id=conv_id,
        run_id=run_id,
        root_run_id=str(event.get("root_run_id") or root_run_id or run_id),
        parent_run_id=event.get("parent_run_id"),
        kind=str(payload.get("kind") or "file")[:32],
        title=title,
        path=path,
        mime_type=str(mime_type)[:128] if mime_type else None,
        preview_kind=str(preview_kind)[:32] if preview_kind else None,
        size_bytes=max(0, size_bytes),
        sha256=str(sha256)[:64] if sha256 else None,
        source_tool_name=event.get("tool_name") or payload.get("source_tool_name") or payload.get("tool_name"),
        source_tool_call_id=event.get("tool_call_id") or payload.get("source_tool_call_id") or payload.get("tool_call_id"),
        source_event_key=source_event_key,
        summary=str(payload.get("summary")) if payload.get("summary") else None,
        metadata_json=json.dumps(payload, ensure_ascii=False),
    ))


def _task_key_for_event(event: dict, payload: dict, run_id: str) -> str | None:
    event_type = str(event.get("event_type") or "")
    if event_type.startswith("verifier."):
        verifier_kind = str(payload.get("verifier_kind") or "generic")
        target_run_id = str(payload.get("target_run_id") or run_id)
        return str(payload.get("task_key") or payload.get("verifier_key") or f"verifier:{target_run_id}:{verifier_kind}")
    if event_type in {
        "agent.spawned",
        "run.started",
        "run.completed",
        "run.failed",
        "run.cancelled",
    }:
        return f"run:{run_id}"
    return None


def _project_task_event(
    s: AsyncSession,
    *,
    task_items: dict[str, TaskItem],
    conv_id: str,
    user_id: str,
    root_run_id: str,
    run_id: str,
    event: dict,
    payload: dict,
) -> None:
    task_key = _task_key_for_event(event, payload, run_id)
    if not task_key:
        return
    event_type = str(event.get("event_type") or "")
    now = datetime.utcnow()
    agent_kind = str(event.get("agent_kind") or "primary")
    if event_type.startswith("verifier."):
        kind = "verification"
        title = str(payload.get("verifier_kind") or "Verifier")[:256]
    else:
        kind = "delegate" if agent_kind == "delegate" else "primary"
        title = str(event.get("agent_name") or payload.get("goal") or agent_kind or "Run")[:256]
    task = task_items.get(task_key)
    if task is None:
        task = TaskItem(
            id=uuid.uuid4().hex,
            user_id=user_id,
            conversation_id=conv_id,
            run_id=run_id,
            root_run_id=str(event.get("root_run_id") or root_run_id or run_id),
            parent_run_id=event.get("parent_run_id"),
            task_key=task_key,
            kind=kind,
            title=title,
            status="running",
            agent_name=event.get("agent_name"),
            metadata_json=json.dumps(payload, ensure_ascii=False),
        )
        s.add(task)
        task_items[task_key] = task
    task.run_id = run_id
    task.root_run_id = str(event.get("root_run_id") or task.root_run_id or root_run_id or run_id)
    task.parent_run_id = event.get("parent_run_id") or task.parent_run_id
    task.kind = kind
    task.title = title or task.title
    task.agent_name = event.get("agent_name") or task.agent_name
    task.metadata_json = json.dumps(payload, ensure_ascii=False)
    task.updated_at = now
    if event_type in {"agent.spawned", "run.started", "verifier.requested"}:
        task.status = "running"
        task.ended_at = None
        if payload.get("goal"):
            task.summary = str(payload.get("goal"))[:4000]
    elif event_type == "run.completed":
        task.status = "succeeded"
        task.summary = str(payload.get("finish_reason") or "completed")[:4000]
        task.ended_at = now
    elif event_type == "run.failed":
        task.status = "failed"
        task.error = str(payload.get("error") or "Unknown error")
        task.ended_at = now
    elif event_type == "run.cancelled":
        task.status = "cancelled"
        task.error = None
        task.summary = str(
            payload.get("terminal_reason") or "task_cancelled"
        )[:4000]
        task.ended_at = now
    elif event_type == "verifier.completed":
        verdict = str(payload.get("verdict") or "inconclusive")
        task.status = "succeeded" if verdict == "pass" else "failed" if verdict == "fail" else "blocked"
        task.summary = str(payload.get("reason") or verdict)[:4000]
        task.error = task.summary if task.status == "failed" else None
        task.ended_at = now
    elif event_type == "verifier.failed":
        task.status = "failed"
        task.error = str(payload.get("error") or payload.get("reason") or "Verifier failed")
        task.ended_at = now


async def _persist_agent_events(
    s: AsyncSession,
    *,
    conv_id: str,
    user_id: str | None,
    root_run_id: str,
    requested_model_id: str,
    resolved_model_id: str,
    events: list[dict],
) -> None:
    if not user_id or not events:
        return
    existing_runs = {
        run.id: run
        for run in (await s.execute(
            select(AgentRun).where(AgentRun.conversation_id == conv_id)
        )).scalars().all()
    }
    event_run_ids = {
        str(event.get("run_id") or "")
        for event in events
        if event.get("run_id")
    }
    persisted_event_keys: set[tuple[str, str, int]] = set()
    if event_run_ids:
        rows = (await s.execute(
            select(AgentRunEvent.run_id, AgentRunEvent.event_type, AgentRunEvent.seq).where(
                AgentRunEvent.conversation_id == conv_id,
                AgentRunEvent.run_id.in_(event_run_ids),
            )
        )).all()
        persisted_event_keys = {
            (str(row[0]), str(row[1]), int(row[2] or 0))
            for row in rows
        }
    task_items = {
        task.task_key: task
        for task in (await s.execute(
            select(TaskItem).where(
                TaskItem.conversation_id == conv_id,
                TaskItem.user_id == user_id,
            )
        )).scalars().all()
    }
    for event in events:
        run_id = str(event.get("run_id") or "")
        if not run_id:
            continue
        payload = _event_payload(event)
        event_type = str(event.get("event_type") or "unknown")
        if event_type in {"agent.delta", "agent.reasoning_delta"}:
            continue
        seq = int(event.get("seq") or 0)
        event_key = (run_id, event_type, seq)
        event_already_persisted = event_key in persisted_event_keys
        if event_already_persisted and event_type.startswith("debug."):
            continue
        if run_id not in existing_runs:
            child_run = AgentRun(
                id=run_id,
                user_id=user_id,
                conversation_id=conv_id,
                parent_run_id=event.get("parent_run_id"),
                root_run_id=event.get("root_run_id") or root_run_id,
                agent_kind=str(event.get("agent_kind") or "delegate"),
                agent_name=event.get("agent_name"),
                depth=int(event.get("depth") or 0),
                workspace_scope=str(event.get("workspace_scope") or "shared_session"),
                source="delegate" if event.get("agent_kind") == "delegate" else "chat",
                requested_model_id=str(payload.get("model_id") or requested_model_id),
                resolved_model_id=resolved_model_id,
                status="running",
            )
            s.add(child_run)
            existing_runs[run_id] = child_run
        run = existing_runs[run_id]
        run.parent_run_id = event.get("parent_run_id") or run.parent_run_id
        run.root_run_id = event.get("root_run_id") or run.root_run_id or root_run_id
        run.agent_kind = str(event.get("agent_kind") or run.agent_kind or "delegate")
        run.agent_name = event.get("agent_name") or run.agent_name
        run.depth = int(event.get("depth") if event.get("depth") is not None else run.depth or 0)
        run.workspace_scope = str(event.get("workspace_scope") or run.workspace_scope or "shared_session")
        if event.get("event_type") == "agent.spawned":
            run.effective_tools = json.dumps(payload.get("effective_tools") or [], ensure_ascii=False)
            run.requested_tools = json.dumps(payload.get("requested_tools") or [], ensure_ascii=False)
        elif event.get("event_type") == "run.started":
            run.status = "running"
            if payload.get("model_id"):
                run.requested_model_id = str(payload.get("model_id"))
            if payload.get("enabled_tools") is not None:
                run.effective_tools = json.dumps(payload.get("enabled_tools"), ensure_ascii=False)
        elif event.get("event_type") == "usage.updated":
            _merge_monotonic_run_usage(run, payload)
            run.resolved_model_id = str(payload.get("model") or run.resolved_model_id or resolved_model_id)
        elif event.get("event_type") == "model.switch":
            run.resolved_model_id = str(payload.get("to_model") or run.resolved_model_id or resolved_model_id)
        elif event.get("event_type") == "run.completed":
            run.status = "succeeded"
            run.finish_reason = str(payload.get("finish_reason") or "stop")
            usage_payload = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            _merge_monotonic_run_usage(run, usage_payload)
            run.ended_at = datetime.utcnow()
        elif event.get("event_type") == "run.failed":
            run.status = "failed"
            run.error = str(payload.get("error") or "Unknown error")
            usage_payload = (
                payload.get("usage")
                if isinstance(payload.get("usage"), dict)
                else {}
            )
            _merge_monotonic_run_usage(run, usage_payload)
            run.ended_at = datetime.utcnow()
        elif event.get("event_type") == "run.cancelled":
            run.status = "cancelled"
            run.finish_reason = str(
                payload.get("finish_reason")
                or payload.get("terminal_reason")
                or "task_cancelled"
            )
            usage_payload = (
                payload.get("usage")
                if isinstance(payload.get("usage"), dict)
                else {}
            )
            _merge_monotonic_run_usage(run, usage_payload)
            run.error = None
            run.ended_at = datetime.utcnow()
        await _project_artifact_event(
            s,
            conv_id=conv_id,
            user_id=user_id,
            root_run_id=root_run_id,
            run_id=run_id,
            event=event,
            payload=payload,
        )
        _project_task_event(
            s,
            task_items=task_items,
            conv_id=conv_id,
            user_id=user_id,
            root_run_id=root_run_id,
            run_id=run_id,
            event=event,
            payload=payload,
        )
        if not event_already_persisted:
            s.add(AgentRunEvent(
                id=uuid.uuid4().hex,
                run_id=run_id,
                conversation_id=conv_id,
                user_id=user_id,
                parent_run_id=event.get("parent_run_id"),
                seq=seq,
                event_type=event_type,
                payload=json.dumps(payload, ensure_ascii=False),
                tool_name=event.get("tool_name") or payload.get("tool_name"),
                tool_call_id=event.get("tool_call_id") or payload.get("tool_call_id"),
            ))
            persisted_event_keys.add(event_key)


def _should_persist_agent_event_immediately(event_type: str) -> bool:
    if not settings.agent_event_immediate_persist:
        return False
    if event_type.startswith("debug."):
        return True
    if not settings.agent_debug_trace:
        return False
    if event_type in {"agent.delta", "agent.reasoning_delta"}:
        return False
    return event_type.startswith((
        "run.",
        "usage.",
        "model.",
        "tool.",
        "artifact.",
        "verifier.",
        "agent.spawned",
    ))


async def _persist_agent_event_once(
    *,
    conv_id: str,
    user_id: str,
    root_run_id: str,
    requested_model_id: str,
    resolved_model_id: str,
    event: dict,
) -> bool:
    async with async_session() as s:
        try:
            run_id = str(event.get("run_id") or "")
            event_type = str(event.get("event_type") or "")
            seq = int(event.get("seq"))
            exists = (await s.execute(
                select(AgentRunEvent.id).where(
                    AgentRunEvent.conversation_id == conv_id,
                    AgentRunEvent.run_id == run_id,
                    AgentRunEvent.event_type == event_type,
                    AgentRunEvent.seq == seq,
                )
            )).scalar_one_or_none()
            if exists:
                return True
            await _persist_agent_events(
                s,
                conv_id=conv_id,
                user_id=user_id,
                root_run_id=root_run_id,
                requested_model_id=requested_model_id,
                resolved_model_id=resolved_model_id,
                events=[event],
            )
            await s.commit()
            return True
        except BaseException:
            await s.rollback()
            raise


async def _persist_agent_event_immediate(
    *,
    conv_id: str,
    user_id: str,
    root_run_id: str,
    requested_model_id: str,
    resolved_model_id: str,
    event: dict,
) -> bool:
    run_id = str(event.get("run_id") or "")
    event_type = str(event.get("event_type") or "")
    raw_seq = event.get("seq")
    try:
        seq = int(raw_seq)
    except (TypeError, ValueError):
        return False
    if not user_id or not run_id or not event_type or raw_seq is None or seq < 0:
        return False
    if not _should_persist_agent_event_immediately(event_type):
        return False
    lock = _agent_event_persist_lock(conv_id)
    try:
        async with lock:
            return await _run_sqlite_persist_with_retry(
                lambda: _persist_agent_event_once(
                    conv_id=conv_id,
                    user_id=user_id,
                    root_run_id=root_run_id,
                    requested_model_id=requested_model_id,
                    resolved_model_id=resolved_model_id,
                    event=event,
                ),
                description=(
                    f"agent event conv={conv_id} run={run_id} "
                    f"type={event_type} seq={seq}"
                ),
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Immediate agent event persist failed after retries "
            "conv=%s run=%s type=%s seq=%s",
            conv_id,
            run_id,
            event_type,
            seq,
        )
        return False
    finally:
        _cleanup_agent_event_persist_lock(conv_id, lock)


def _spawn_agent_event_immediate_persist(
    *,
    conv_id: str,
    user_id: str,
    root_run_id: str,
    requested_model_id: str,
    resolved_model_id: str,
    event: dict,
) -> asyncio.Task:
    task = asyncio.create_task(_persist_agent_event_immediate(
        conv_id=conv_id,
        user_id=user_id,
        root_run_id=root_run_id,
        requested_model_id=requested_model_id,
        resolved_model_id=resolved_model_id,
        event=event,
    ))
    key = (conv_id, root_run_id)
    pending = _agent_event_persist_tasks.setdefault(key, set())
    pending.add(task)
    _background_tasks.add(task)

    def finish(completed: asyncio.Task) -> None:
        _background_tasks.discard(completed)
        tracked = _agent_event_persist_tasks.get(key)
        if tracked is not None:
            tracked.discard(completed)
            if not tracked:
                _agent_event_persist_tasks.pop(key, None)
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            error = None
        if error is not None:
            logger.error(
                "Uncaught immediate agent event persist task failure "
                "conv=%s root_run=%s",
                conv_id,
                root_run_id,
                exc_info=(type(error), error, error.__traceback__),
            )
        lock = _agent_event_persist_locks.get(conv_id)
        if lock is not None:
            _cleanup_agent_event_persist_lock(conv_id, lock)

    task.add_done_callback(finish)
    return task


async def _drain_agent_event_immediate_persists(
    conv_id: str,
    root_run_id: str,
) -> None:
    key = (conv_id, root_run_id)
    while True:
        tasks = [
            task
            for task in _agent_event_persist_tasks.get(key, set())
            if not task.done()
        ]
        if not tasks:
            return
        await asyncio.gather(
            *(asyncio.shield(task) for task in tasks),
            return_exceptions=True,
        )


async def _next_message_created_at(
    db: AsyncSession,
    conv_id: str,
) -> datetime:
    """Return a strictly increasing timestamp for new messages in a session."""

    latest = (await db.execute(
        select(func.max(Message.created_at)).where(
            Message.conversation_id == conv_id
        )
    )).scalar_one_or_none()
    now = datetime.utcnow()
    if latest is not None and now <= latest:
        return latest + timedelta(microseconds=1)
    return now


async def _assert_no_unprojected_primary_turn(
    db: AsyncSession,
    conv_id: str,
) -> None:
    """Fail closed if a prior chat turn exhausted terminal persistence."""

    latest_run = (await db.execute(
        select(AgentRun.id, AgentRun.status, AgentRun.ended_at).where(
            AgentRun.conversation_id == conv_id,
            AgentRun.source == "chat",
            AgentRun.parent_run_id.is_(None),
        ).order_by(AgentRun.started_at.desc(), AgentRun.id.desc()).limit(1)
    )).one_or_none()
    latest_message_role = (await db.execute(
        select(Message.role).where(
            Message.conversation_id == conv_id
        ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1)
    )).scalar_one_or_none()
    unprojected = bool(
        latest_run is not None
        and (
            latest_run.status == "running"
            or (
                latest_message_role == "user"
                and latest_run.status != "cancelled"
                and latest_run.ended_at is not None
            )
        )
    )
    if unprojected:
        raise HTTPException(
            status_code=503,
            detail=(
                "A previous conversation turn has no durable terminal "
                "projection; this turn was not started."
            ),
        )


async def _persist_stream_projection_once(
    conv_id: str,
    model_id: str,
    content: str,
    reasoning: str,
    tool_progress: str,
    run_id: str,
    resolved_model_id: str,
    usage: dict,
    finish_reason: str,
    error_message: str | None,
    agent_events: list[dict],
) -> tuple[str | None, str | None]:
    async with async_session() as s:
        assistant_message = None
        event_user_id = None
        try:
            terminal_status, _ = _agent_event_terminal_status(
                agent_events, run_id=run_id
            )
            reconciled_usage = _reconciled_root_run_usage(
                usage,
                agent_events,
                run_id=run_id,
            )
            input_tokens = reconciled_usage["input_tokens"]
            output_tokens = reconciled_usage["output_tokens"]
            total_tokens = reconciled_usage["total_tokens"]
            if content or reasoning:
                assistant_message = Message(
                    conversation_id=conv_id,
                    role="assistant",
                    content=content,
                    reasoning=reasoning or None,
                    tool_progress=tool_progress or None,
                    model_id=resolved_model_id or model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    created_at=await _next_message_created_at(s, conv_id),
                )
                s.add(assistant_message)
            conv = (await s.execute(
                select(Conversation).where(Conversation.id == conv_id)
            )).scalar_one_or_none()
            if conv:
                event_user_id = conv.user_id
                conv.input_tokens += input_tokens
                conv.output_tokens += output_tokens
                conv.total_tokens += total_tokens
                if (
                    conv.goal_token_budget
                    and conv.goal_status == "active"
                    and (
                        conv.total_tokens - conv.goal_started_tokens
                        >= conv.goal_token_budget
                    )
                ):
                    conv.goal_status = "budget_limited"
                    conv.goal_note = "Goal token budget reached."
            await _persist_agent_events(
                s,
                conv_id=conv_id,
                user_id=event_user_id,
                root_run_id=run_id,
                requested_model_id=model_id,
                resolved_model_id=resolved_model_id or model_id,
                events=agent_events,
            )
            run = (await s.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )).scalar_one_or_none()
            if run:
                run.root_run_id = run.root_run_id or run.id
                run.agent_kind = run.agent_kind or "primary"
                run.agent_name = run.agent_name or "primary"
                run.depth = run.depth or 0
                run.workspace_scope = run.workspace_scope or "shared_session"
                run.resolved_model_id = resolved_model_id or model_id
                if terminal_status == "cancelled":
                    run.status = "cancelled"
                    run.finish_reason = "task_cancelled"
                    run.error = None
                else:
                    run.status = "failed" if error_message else "succeeded"
                    run.finish_reason = finish_reason
                    run.error = error_message
                run.tool_events = json.dumps(
                    tool_progress.splitlines() if tool_progress else [],
                    ensure_ascii=False,
                )
                run.input_tokens = input_tokens
                run.output_tokens = output_tokens
                run.total_tokens = total_tokens
                run.ended_at = datetime.utcnow()
            await s.commit()
            return (
                assistant_message.id if assistant_message is not None else None,
                str(event_user_id) if event_user_id else None,
            )
        except BaseException:
            await s.rollback()
            raise


async def _persist_after_stream(
    conv_id: str,
    model_id: str,
    content: str,
    reasoning: str,
    tool_progress: str,
    first_user_content: str,
    run_id: str,
    resolved_model_id: str,
    usage: dict,
    finish_reason: str,
    error_message: str | None,
    agent_events: list[dict] | None = None,
) -> bool:
    """Durably project one completed stream after its live event writers drain."""

    complete_events = list(agent_events or [])
    terminal_status, terminal_error = _agent_event_terminal_status(
        complete_events,
        run_id=run_id,
    )
    if terminal_status == "failed" and not error_message:
        error_message = terminal_error
    elif terminal_status == "cancelled":
        error_message = None
        finish_reason = "task_cancelled"
    elif terminal_status is None and not error_message:
        error_message = "Harness stream ended without a terminal run event."

    # Every live projection for this root run was registered before the stream
    # entered its finally block.  Await them first so the complete ordered
    # event list below is a true terminal backfill rather than another racing
    # SQLite writer.
    await _drain_agent_event_immediate_persists(conv_id, run_id)
    lock = _agent_event_persist_lock(conv_id)
    try:
        async with lock:
            assistant_message_id, event_user_id = (
                await _run_sqlite_persist_with_retry(
                    lambda: _persist_stream_projection_once(
                        conv_id,
                        model_id,
                        content,
                        reasoning,
                        tool_progress,
                        run_id,
                        resolved_model_id,
                        usage,
                        finish_reason,
                        error_message,
                        complete_events,
                    ),
                    description=(
                        f"terminal stream projection conv={conv_id} run={run_id}"
                    ),
                )
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Terminal stream projection failed after retries conv=%s run=%s",
            conv_id,
            run_id,
        )
        return False
    finally:
        _cleanup_agent_event_persist_lock(conv_id, lock)

    if assistant_message_id is not None and event_user_id:
        try:
            await emit_event(
                event_user_id,
                "message.created",
                {
                    "conversation_id": conv_id,
                    "message_id": assistant_message_id,
                    "role": "assistant",
                    "model_id": resolved_model_id or model_id,
                    "source": "chat",
                },
                conv_id,
            )
        except Exception:
            logger.exception(
                "Post-persist message event failed conv=%s run=%s",
                conv_id,
                run_id,
            )
    async with async_session() as s:
        try:
            cnt = (await s.execute(
                select(func.count(Message.id)).where(Message.conversation_id == conv_id)
            )).scalar() or 0
            logger.info("title check: conv=%s cnt=%s has_first=%s", conv_id, cnt, bool(first_user_content))
            if cnt <= 2 and first_user_content:
                await _generate_title(conv_id, first_user_content, content, s)
        except Exception as e:
            logger.exception("title generation failed: %s", e)
    return True


def _spawn_persist(
    conv_id: str,
    model_id: str,
    content: str,
    reasoning: str,
    tool_progress: str,
    first_user_content: str,
    run_id: str,
    resolved_model_id: str,
    usage: dict,
    finish_reason: str,
    error_message: str | None,
    agent_events: list[dict] | None = None,
):
    t = asyncio.create_task(_persist_after_stream(
        conv_id, model_id, content, reasoning, tool_progress, first_user_content,
        run_id, resolved_model_id, usage, finish_reason, error_message, agent_events,
    ))
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)


def _spawn_persist_then_emit(
    *,
    user_id: str,
    conv_id: str,
    model_id: str,
    content: str,
    reasoning: str,
    tool_progress: str,
    first_user_content: str,
    run_id: str,
    resolved_model_id: str,
    usage: dict,
    finish_reason: str,
    error_message: str | None,
    agent_events: list[dict] | None = None,
) -> asyncio.Task:
    async def persist_then_emit() -> bool:
        reconciled_usage = _reconciled_root_run_usage(
            usage,
            agent_events or [],
            run_id=run_id,
        )
        terminal_status, terminal_error = _agent_event_terminal_status(
            agent_events or [],
            run_id=run_id,
        )
        effective_error = error_message
        if terminal_status == "failed" and not effective_error:
            effective_error = terminal_error or "Agent run failed."
        elif terminal_status == "cancelled":
            effective_error = None
        persisted = await _persist_after_stream(
            conv_id, model_id, content, reasoning, tool_progress, first_user_content,
            run_id, resolved_model_id, usage, finish_reason, effective_error, agent_events,
        )
        if not persisted:
            raise RuntimeError(
                f"Terminal stream projection was not durable conv={conv_id} run={run_id}"
            )
        try:
            await emit_event(
                user_id,
                (
                    "run.cancelled"
                    if terminal_status == "cancelled"
                    else "run.failed"
                    if terminal_status == "failed" or effective_error
                    else "run.completed"
                ),
                {
                    "conversation_id": conv_id,
                    "run_id": run_id,
                    "model_id": resolved_model_id,
                    "usage": reconciled_usage,
                    "error": effective_error,
                },
                conv_id,
            )
        except Exception:
            # The database projection is the history barrier.  A best-effort
            # notification failure must not make a durable turn look missing.
            logger.exception(
                "Post-projection terminal event failed conv=%s run=%s",
                conv_id,
                run_id,
            )
        return True

    return _track_conversation_projection(conv_id, persist_then_emit())


BUILTIN = {
    # 10.10.132.2 GLM-5.2 (303872 ctx) — 主模型
    "deepseek_v4_pro": {
        "api_model": "AgentModel",
        "base_url": settings.deepseek_pro_base_url,
        "api_key": settings.deepseek_pro_api_key,
        "is_multimodal": False,
        "max_tokens": 262144,
        "display_name": "GLM-5.2 (主模型)",
        "is_default": True,
        "agentic_auxiliary_only": False,
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
        "capabilities": ["text", "tools", "reasoning"],
        "provider": "builtin",
        "protocol": "openai",
        "context_length": 303872,
    },
    # 10.10.132.128 Qwen3-5 (397B, multimodal) — 多模态识别
    "qwen3_5": {
        "api_model": "qwen3_5",
        "base_url": settings.qwen3_5_base_url,
        "api_key": settings.qwen3_5_api_key,
        "is_multimodal": True,
        "max_tokens": 65536,
        "display_name": "Qwen3-5 (397B 多模态)",
        "is_default": False,
        "agentic_auxiliary_only": True,
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": False,
        "capabilities": ["text", "vision", "tools"],
        "provider": "builtin",
        "protocol": "openai",
        "context_length": 262144,
    },
}

# Backward-compatible alias for existing conversations
BUILTIN["AgentModel"] = BUILTIN["deepseek_v4_pro"]

DEFAULT_CUSTOM_MAX_TOKENS = 32768


async def resolve_model(model_id: str, cur_user: User, db: AsyncSession):
    """Return (base_url, api_key, is_multimodal, max_tokens, api_model)."""
    model_id = canonical_agent_model_id(model_id)
    if model_id in BUILTIN:
        c = BUILTIN[model_id]
        return c["base_url"], c["api_key"], c["is_multimodal"], c["max_tokens"], c["api_model"]
    r = await db.execute(
        select(CustomModelConfig).where(
            CustomModelConfig.user_id == cur_user.id,
            CustomModelConfig.model_id == model_id,
        )
    )
    cm = r.scalar_one_or_none()
    if cm:
        return cm.base_url, cm.api_key, cm.is_multimodal, DEFAULT_CUSTOM_MAX_TOKENS, cm.model_id
    raise HTTPException(400, f"Unknown model: {model_id}")


async def resolve_model_config(model_id: str, cur_user: User, db: AsyncSession) -> dict:
    model_id = canonical_agent_model_id(model_id)
    if model_id in BUILTIN:
        cfg = BUILTIN[model_id]
        return {
            "id": model_id,
            "base_url": cfg["base_url"].rstrip("/"),
            "api_key": cfg["api_key"],
            "api_model": cfg["api_model"],
            "provider": cfg.get("provider", "builtin"),
            "protocol": cfg.get("protocol", "openai"),
            "is_multimodal": cfg["is_multimodal"],
            "context_length": cfg.get("context_length", 262144),
            "agentic_auxiliary_only": cfg.get(
                "agentic_auxiliary_only", False
            ),
            "supports_thinking_toggle": cfg.get(
                "supports_thinking_toggle", False
            ),
            "thinking_enabled_by_default": cfg.get(
                "thinking_enabled_by_default", True
            ),
        }
    custom = (await db.execute(
        select(CustomModelConfig).where(
            CustomModelConfig.user_id == cur_user.id,
            CustomModelConfig.model_id == model_id,
        )
    )).scalar_one_or_none()
    if custom is None:
        raise HTTPException(400, f"Unknown model: {model_id}")
    try:
        extra_headers = json.loads(custom.extra_headers) if custom.extra_headers else {}
    except json.JSONDecodeError:
        extra_headers = {}
    return {
        "id": custom.model_id,
        "base_url": custom.base_url.rstrip("/"),
        "api_key": custom.api_key,
        "api_model": custom.model_id,
        "provider": custom.provider,
        "protocol": "anthropic" if custom.provider == "anthropic" else "openai",
        "is_multimodal": custom.is_multimodal,
        "context_length": 128000,
        "extra_headers": extra_headers,
    }


@router.get("/models")
async def get_models(cur_user=Depends(get_current_user), db=Depends(get_db)):
    # Pull builtin models from harness
    models: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.harness_url}/v1/models")
            if r.status_code == 200:
                data = r.json()
                for m in data.get("data", []):
                    mid = m["id"]
                    cfg = BUILTIN.get(mid, {})
                    models.append({
                        "id": mid,
                        "name": cfg.get("display_name", mid),
                        "provider": "builtin",
                        "is_multimodal": cfg.get("is_multimodal", False),
                        "is_default": cfg.get("is_default", False),
                        "capabilities": cfg.get("capabilities", ["text"]),
                    })
    except Exception:
        pass  # fall back to BUILTIN
    if not models:
        models = [
            {"id": mid, "name": cfg["display_name"], "provider": "builtin",
             "is_multimodal": cfg["is_multimodal"], "is_default": cfg.get("is_default", False),
             "capabilities": cfg.get("capabilities", ["text"])}
            for mid, cfg in BUILTIN.items()
            if mid != "AgentModel"  # skip backward-compat alias
        ]
    # Merge custom models
    r = await db.execute(
        select(CustomModelConfig).where(CustomModelConfig.user_id == cur_user.id)
    )
    for cm in r.scalars().all():
        models.append({
            "id": cm.model_id, "name": cm.model_name,
            "provider": cm.provider, "is_multimodal": cm.is_multimodal,
            "is_default": False,
            "capabilities": ["vision"] if cm.is_multimodal else ["text"],
        })
    return {"models": models}


async def _detect_model(req: ChatRequest, user_id: str) -> str:
    """Auto-detect model based on message content.

    Routing rules:
    1. Image → deepseek_v4_pro (agent discovers session skills/MCP tools and decides:
       call MCP tool for specialized processing, or use vision_analyze tool
       which internally calls qwen3_5 for visual recognition)
    2. Plain text → deepseek_v4_pro (default main model)
    """
    if req.model_id:
        return req.model_id
    return "deepseek_v4_pro"


async def _generate_title(
    conv_id: str,
    first_user: str,
    first_assistant: str,
    db: AsyncSession,
):
    """Summarize the first QA exchange into a short conversation title using
    qwen3_5 (thinking disabled). Must NOT echo the user's literal question."""
    excerpt = f"用户:{first_user[:300].strip()}\n助手:{(first_assistant or '').strip()[:500]}"
    prompt = (
        "下面是一段对话的开头。请用 4 到 10 个字概括这段对话的核心主题,作为会话标题。\n"
        "硬性要求:\n"
        "1. 不能直接复述或抄写用户的原话,要做主题归纳\n"
        "2. 不要加引号、不要加句号、不要加任何解释\n"
        "3. 用对话本身所用的语言\n"
        "4. 只输出标题本身,一行\n\n"
        f"{excerpt}"
    )
    title = ""
    stream_error = None
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            async with c.stream(
                "POST",
                f"{settings.qwen3_5_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.qwen3_5_api_key}"},
                json={
                    "model": "qwen3_5",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 80,
                    "temperature": 0.3,
                    "stream": True,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            ) as r:
                if r.status_code >= 400:
                    stream_error = f"qwen3_5 HTTP {r.status_code}"
                else:
                    async for line in r.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        chunk = line[6:]
                        if chunk == "[DONE]":
                            break
                        try:
                            delta = json.loads(chunk)["choices"][0].get("delta", {})
                            piece = delta.get("content")
                            if piece:
                                title += piece
                        except Exception:
                            continue
    except Exception as e:
        stream_error = str(e)
        logger.warning("title generation stream failed for %s: %s", conv_id, e)

    title = title.strip().strip('"').strip("'").splitlines()[0].strip() if title.strip() else ""
    if not title:
        # Fallback: use first ~20 chars of user message if qwen3_5 unreachable
        # or returns empty. Ensures the sidebar always shows something meaningful
        # instead of "新会话".
        fallback = first_user.strip().splitlines()[0][:20].strip()
        if fallback:
            title = fallback
            logger.info("using fallback title for %s: %r", conv_id, title)
    if not title or len(title) > 100:
        return
    r = await db.execute(select(Conversation).where(Conversation.id == conv_id))
    conv = r.scalar_one_or_none()
    if conv:
        conv.title = title
        await db.commit()
        logger.info("title saved for %s: %r", conv_id, title)


async def _chat_stream(req: ChatRequest, cur_user: User, db: AsyncSession):
    # Serialize a conversation before verifying it or loading history.  A
    # different conversation receives a distinct lock and is unaffected.
    conv_id = req.conversation_id
    turn_lease: _ConversationTurnLease | None = None
    try:
        if conv_id:
            turn_lease = await _acquire_conversation_turn(conv_id)
            conv = (await db.execute(
                select(Conversation).where(
                    Conversation.id == conv_id,
                    Conversation.user_id == cur_user.id,
                )
            )).scalar_one_or_none()
            if not conv:
                raise HTTPException(404, "Conversation not found")
            model_id = canonical_agent_model_id(
                req.model_id
                or conv.model_id
                or await _detect_model(req, str(cur_user.id))
            )
            if model_id != conv.model_id:
                # Repair historic persisted `AgentModel` aliases on the next
                # ordinary turn. qwen3_5 is intentionally not canonicalized
                # here: the harness may retain it for an explicit image turn.
                conv.model_id = model_id
                await db.commit()
            ensure_workspace(cur_user.id, conv_id)
        else:
            model_id = canonical_agent_model_id(
                await _detect_model(req, str(cur_user.id))
            )
            conv = Conversation(user_id=cur_user.id, model_id=model_id)
            db.add(conv)
            await db.commit()
            await db.refresh(conv)
            conv_id = conv.id
            turn_lease = await _acquire_conversation_turn(conv_id)
            ensure_workspace(cur_user.id, conv_id)
            await emit_event(
                cur_user.id,
                "session.created",
                {"conversation_id": conv_id},
                conv_id,
            )

        return await _chat_stream_with_turn(
            req,
            cur_user,
            db,
            conv=conv,
            conv_id=conv_id,
            model_id=model_id,
            turn_lease=turn_lease,
        )
    except _ConversationProjectionBarrierError as exc:
        if turn_lease is not None:
            _release_conversation_turn(conv_id, turn_lease)
        raise HTTPException(
            status_code=503,
            detail=(
                "The previous conversation turn could not be durably projected; "
                "this turn was not started."
            ),
        ) from exc
    except BaseException:
        if turn_lease is not None:
            _release_conversation_turn(conv_id, turn_lease)
        raise


async def _chat_stream_with_turn(
    req: ChatRequest,
    cur_user: User,
    db: AsyncSession,
    *,
    conv: Conversation,
    conv_id: str,
    model_id: str,
    turn_lease: _ConversationTurnLease,
):
    await _assert_no_unprojected_primary_turn(db, conv_id)

    provider_config = await resolve_model_config(model_id, cur_user, db)
    max_tokens = (
        BUILTIN.get(model_id, {}).get("max_tokens")
        if model_id in BUILTIN else DEFAULT_CUSTOM_MAX_TOKENS
    ) or DEFAULT_CUSTOM_MAX_TOKENS
    enabled_tools = serialize_json_list(conv.enabled_tools, DEFAULT_NATIVE_TOOLS)
    enabled_user_skills = serialize_json_list(conv.enabled_user_skills, [])
    fallback_ids, removed_fallback_ids = filter_agentic_fallback_model_ids(
        serialize_json_list(conv.fallback_model_ids, []),
        requested_model_id=model_id,
    )
    if removed_fallback_ids:
        logger.info(
            "Filtered auxiliary/duplicate agentic fallbacks conversation=%s "
            "requested_model=%s removed=%s",
            conv_id,
            model_id,
            removed_fallback_ids,
        )
    fallback_configs = []
    for fallback_id in fallback_ids:
        if fallback_id == model_id:
            continue
        try:
            fallback_configs.append(
                await resolve_model_config(fallback_id, cur_user, db)
            )
        except HTTPException:
            continue

    # Load history before saving the new user message
    history_msgs = (await db.execute(
        select(Message).where(Message.conversation_id == conv_id).order_by(
            Message.created_at,
            Message.id,
        )
    )).scalars().all()
    history = []
    for m in history_msgs:
        entry = {"role": m.role, "content": m.content or ""}
        # Preserve image_urls so subsequent turns can reference images
        if m.image_urls:
            try:
                urls = json.loads(m.image_urls) if isinstance(m.image_urls, str) else m.image_urls
                if urls:
                    entry["image_urls"] = urls
            except (json.JSONDecodeError, TypeError):
                pass
        history.append(entry)

    # Save user message synchronously — must persist even if stream cancelled
    user_created_at = await _next_message_created_at(db, conv_id)
    user_msg = Message(
        conversation_id=conv_id,
        role="user",
        content=req.content,
        image_urls=json.dumps(req.image_urls) if req.image_urls else None,
        model_id=model_id,
        source="chat",
        created_at=user_created_at,
    )
    db.add(user_msg)
    run = AgentRun(
        user_id=cur_user.id,
        conversation_id=conv_id,
        source="chat",
        requested_model_id=model_id,
        resolved_model_id=model_id,
        status="running",
        agent_kind="primary",
        agent_name="primary",
        depth=0,
        workspace_scope="shared_session",
        requested_tools=json.dumps(enabled_tools, ensure_ascii=False),
        effective_tools=json.dumps(enabled_tools, ensure_ascii=False),
        started_at=user_created_at,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    run.root_run_id = run.id
    await db.commit()
    await emit_event(
        cur_user.id,
        "message.created",
        {
            "conversation_id": conv_id,
            "message_id": user_msg.id,
            "role": "user",
            "model_id": model_id,
            "source": "chat",
        },
        conv_id,
    )
    await emit_event(
        cur_user.id, "run.started",
        {"conversation_id": conv_id, "run_id": run.id, "model_id": model_id},
        conv_id,
    )

    async def generate():
        full_content = ""
        full_reasoning = ""
        full_tool_progress = ""
        error_message: Optional[str] = None
        finish_reason = "stop"
        resolved_model_id = model_id
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        agent_events: list[dict] = []
        seen_agent_events: set[tuple[str, str, int]] = set()
        cancelled = False
        stream_aborted = False

        try:
            # Establish cleanup before the first yield: a client may disconnect
            # immediately after receiving routing metadata.
            yield f"data: {json.dumps({'routed_model': model_id, 'conversation_id': conv_id})}\n\n"

            # Build messages: system prompt + history + user message.
            # Images are passed as proper image_url content parts. The harness
            # handles image routing: for text-only models (deepseek_v4_pro), it
            # pre-analyzes images with qwen3_5 and enriches the message with
            # text descriptions. The agent still has vision_analyze as a tool
            # for deeper inspection, and session skills/MCP tools are
            # discovered via skills_list/skill_view.
            final = [{"role": "system", "content": "You are a helpful AI assistant."}]
            # Convert history messages with image_urls to multimodal format
            for h in history:
                if h.get("image_urls"):
                    content_parts = [{"type": "text", "text": h.get("content", "")}]
                    for url in h["image_urls"]:
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": url},
                        })
                    final.append({"role": h["role"], "content": content_parts})
                else:
                    final.append({"role": h["role"], "content": h.get("content", "")})
            if req.image_urls:
                # Build multimodal content with text + images
                content_parts = [{"type": "text", "text": req.content or "请分析这张图片"}]
                for url in req.image_urls:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": url},
                    })
                final.append({"role": "user", "content": content_parts})
            else:
                final.append({"role": "user", "content": req.content})

            # Stream from harness — agent loop with full tool set
            try:
                async with httpx.AsyncClient(timeout=1800) as client:
                    async with client.stream(
                        "POST",
                        f"{settings.harness_url}/v1/chat/completions",
                        headers={"Content-Type": "application/json"},
                        json={
                            "model": model_id,
                            "messages": final,
                            "max_tokens": max_tokens,
                            "temperature": 0.6,
                            "stream": True,
                            "tools": enabled_tools,
                            "session_id": conv_id,
                            "user": cur_user.id,
                            "provider_config": provider_config,
                            "fallback_configs": fallback_configs,
                            "source": "chat",
                            "enabled_user_skills": enabled_user_skills,
                            "event_schema": "chatds.agent.v2",
                            "run_metadata": {
                                "run_id": run.id,
                                "root_run_id": run.id,
                                "agent_kind": "primary",
                                "agent_name": "primary",
                                "depth": 0,
                                "workspace_scope": "shared_session",
                            },
                        },
                    ) as response:
                        if response.status_code >= 400:
                            body = (await response.aread()).decode("utf-8", "ignore")[:300]
                            error_message = f"Harness 返回 HTTP {response.status_code}:{body}"
                        else:
                            async for line in response.aiter_lines():
                                if not line.startswith("data: "):
                                    continue
                                chunk = line[6:]
                                if chunk == "[DONE]":
                                    break
                                try:
                                    data = json.loads(chunk)
                                    delta = data["choices"][0].get("delta", {})
                                    agent_event = delta.get("agent_event")
                                    if isinstance(agent_event, dict):
                                        key = (
                                            str(agent_event.get("run_id") or ""),
                                            str(agent_event.get("event_type") or ""),
                                            int(agent_event.get("seq") or 0),
                                        )
                                        if key not in seen_agent_events:
                                            seen_agent_events.add(key)
                                            agent_events.append(agent_event)
                                            event_type = str(agent_event.get("event_type") or "")
                                            if event_type == "run.failed":
                                                payload = _event_payload(agent_event)
                                                error_message = str(payload.get("error") or "Agent run failed.")[:4000]
                                            if _should_persist_agent_event_immediately(event_type):
                                                _spawn_agent_event_immediate_persist(
                                                    conv_id=conv_id,
                                                    user_id=cur_user.id,
                                                    root_run_id=run.id,
                                                    requested_model_id=model_id,
                                                    resolved_model_id=resolved_model_id,
                                                    event=agent_event,
                                                )
                                            yield f"data: {json.dumps({'agent_event': agent_event, 'conversation_id': conv_id})}\n\n"
                                    # Harness tool_progress
                                    tp = delta.get("tool_progress")
                                    if tp:
                                        full_tool_progress += (full_tool_progress and "\n" or "") + tp
                                        yield f"data: {json.dumps({'tool_progress': tp, 'conversation_id': conv_id})}\n\n"
                                    reasoning_piece = delta.get("reasoning") or ""
                                    content_piece = delta.get("content") or ""
                                    usage_piece = delta.get("usage")
                                    if isinstance(usage_piece, dict):
                                        usage = {
                                            "input_tokens": int(usage_piece.get("input_tokens", 0) or 0),
                                            "output_tokens": int(usage_piece.get("output_tokens", 0) or 0),
                                            "total_tokens": int(usage_piece.get("total_tokens", 0) or 0),
                                        }
                                    switch = delta.get("model_switch")
                                    if isinstance(switch, dict):
                                        resolved_model_id = switch.get("to_model") or resolved_model_id
                                        msg = (
                                            f"↪ 模型回退: {switch.get('from_model')} → "
                                            f"{switch.get('to_model')} ({switch.get('reason')})"
                                        )
                                        full_tool_progress += (full_tool_progress and "\n" or "") + msg
                                        yield f"data: {json.dumps({'tool_progress': msg, 'model_switch': switch, 'conversation_id': conv_id})}\n\n"
                                    choice = data.get("choices", [{}])[0]
                                    if "error" in data:
                                        error_message = str(data.get("error") or "Harness stream error")
                                    elif isinstance(choice.get("delta"), dict) and choice["delta"].get("error"):
                                        error_message = str(choice["delta"].get("error"))
                                    if choice.get("finish_reason"):
                                        finish_reason = choice["finish_reason"]
                                    if data.get("model"):
                                        resolved_model_id = data["model"]
                                    if reasoning_piece:
                                        full_reasoning += reasoning_piece
                                        yield f"data: {json.dumps({'reasoning_delta': reasoning_piece, 'conversation_id': conv_id})}\n\n"
                                    if content_piece:
                                        full_content += content_piece
                                        yield f"data: {json.dumps({'delta': content_piece, 'conversation_id': conv_id})}\n\n"
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    pass
            except httpx.ConnectError:
                error_message = f"无法连接到 Harness 服务 {settings.harness_url}。请检查 harness 容器是否在运行。"
            except httpx.TimeoutException:
                error_message = "Harness 服务响应超时。"
            except Exception as e:
                error_message = f"调用 Harness 时出错:{type(e).__name__}: {e}"

            terminal_status, terminal_error = _agent_event_terminal_status(
                agent_events,
                run_id=run.id,
            )
            if terminal_status == "failed" and not error_message:
                error_message = terminal_error
            elif terminal_status is None and not error_message:
                error_message = "Harness stream ended without a terminal run event."

            if error_message:
                if full_content:
                    warning = (
                        "\n\n---\n"
                        f"⚠️ 本次响应在流式输出过程中中断：{error_message}\n"
                        "已显示的是不完整草稿，请重新发送或点击重试。"
                    )
                    full_content += warning
                    yield f"data: {json.dumps({'delta': warning, 'conversation_id': conv_id})}\n\n"
                else:
                    # Surface the error as the assistant's content so the user sees it
                    full_content = f"⚠️ {error_message}"
                    yield f"data: {json.dumps({'delta': full_content, 'conversation_id': conv_id})}\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            stream_aborted = True
            root_terminal_status, _root_terminal_error = (
                _agent_event_terminal_status(agent_events, run_id=run.id)
            )
            if root_terminal_status is None:
                cancelled = True
                finish_reason = "task_cancelled"
                root_seq = max(
                    (
                        int(event.get("seq") or 0)
                        for event in agent_events
                        if str(event.get("run_id") or "") == run.id
                    ),
                    default=0,
                )
                cancellation_event = {
                    "type": "agent_event",
                    "event_type": "run.cancelled",
                    "run_id": run.id,
                    "root_run_id": run.id,
                    "parent_run_id": None,
                    "agent_kind": "primary",
                    "agent_name": "primary",
                    "depth": 0,
                    "workspace_scope": "shared_session",
                    "seq": root_seq + 1,
                    "payload": {
                        "finish_reason": "task_cancelled",
                        "terminal_reason": "task_cancelled",
                        "usage": dict(usage),
                    },
                }
                agent_events.append(cancellation_event)
                seen_agent_events.add(
                    (run.id, "run.cancelled", root_seq + 1)
                )
            # Client disconnect is control flow.  Starlette normally cancels
            # the streaming task, while explicit async-generator cleanup can
            # arrive as GeneratorExit; both must close the harness stream and
            # persist the same root cancellation boundary.
            raise
        finally:
            root_terminal_status, _root_terminal_error = (
                _agent_event_terminal_status(agent_events, run_id=run.id)
            )
            if (
                not cancelled
                and root_terminal_status is None
                and not full_content
                and not full_reasoning
                and not error_message
            ):
                error_message = "Stream ended before producing a response."
            projection_task: asyncio.Task | None = None
            try:
                projection_task = _spawn_persist_then_emit(
                    user_id=cur_user.id,
                    conv_id=conv_id,
                    model_id=model_id,
                    content=full_content,
                    reasoning=full_reasoning,
                    tool_progress=full_tool_progress,
                    first_user_content=req.content,
                    run_id=run.id,
                    resolved_model_id=resolved_model_id,
                    usage=usage,
                    finish_reason=finish_reason,
                    error_message=error_message,
                    agent_events=list(agent_events),
                )
                if not stream_aborted:
                    projected = await asyncio.shield(projection_task)
                    if projected is not True:
                        raise _ConversationProjectionBarrierError(
                            "Terminal conversation projection was not durable."
                        )
            finally:
                # On disconnect the shielded task remains registered.  The
                # next turn acquires this lock and drains it before history.
                _release_conversation_turn(conv_id, turn_lease)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/completions")
async def chat_completion(req: ChatRequest,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    return await _chat_stream(req, cur_user, db)
