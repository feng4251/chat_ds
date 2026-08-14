import asyncio
import hashlib
import json
import logging
import re
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional, TypeVar
import httpx
import workspace as workspace_store
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings

logger = logging.getLogger(__name__)
from database import get_db, async_session
from models import (
    AgentEngineRawEvent,
    AgentEngineSession,
    Artifact,
    User,
    Conversation,
    Message,
    CustomModelConfig,
    AgentRun,
    AgentRunEvent,
    TurnActivityEvent,
    TaskItem,
    SkillPackage,
)
from schemas import ChatRequest
from turn_activity import TurnActivityBuilder
from workspace import (
    ensure_workspace_async,
    require_session_workspace_active,
    safe_workspace_path_in_root,
    serialize_json_list,
    workspace_file_metadata,
)
from hooks import emit_event
from native_tools import DEFAULT_NATIVE_TOOLS
from model_routing import (
    DEFAULT_AGENT_MODEL_ID,
    canonical_agent_model_id,
    filter_agentic_fallback_model_ids,
)
from auth import get_current_user
from skill_bundles import (
    content_address_skill_bundle_registry_rows,
    skill_bundle_registry_rows,
)
from stream_observability import (
    _ObservedStreamingResponse,
    _StreamObservation,
    _append_backend_stream_debug_file,
    _append_backend_stream_termination_event,
    _cancellation_diagnostic_fields,
    _cancellation_interruption_message,
    _local_abort_source,
    _provider_failure_summary,
    _safe_diagnostic_label,
    _unsatisfied_contract_summary,
    set_service_shutdown_started,
)
from agent_engines.base import (
    ENGINE_ID_CLAUDE_CODE,
    ENGINE_ID_DEEPSEEK_HARNESS,
    ENGINE_ID_LEGACY,
    AgentEngineError,
    AgentEngineRequest,
)
from agent_engines.input_attachments import (
    InputAttachmentError,
    materialize_message_attachments,
)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Background tasks (keep references so they don't get GC'd before completion).
_background_tasks: set[asyncio.Task] = set()
_detached_chat_producers: set[asyncio.Task] = set()
_detached_chat_producers_by_conversation: dict[
    str,
    set[asyncio.Task],
] = {}
_best_effort_tasks: set[asyncio.Task] = set()
_INCOMPLETE_RESPONSE_MARKERS = (
    "⚠️ 本次任务执行失败：",
    "⚠️ 本次响应在流式输出过程中中断：",
)
_DETACHED_STREAM_DONE = object()
_DETACHED_STREAM_MAX_CHUNKS = 256
_DETACHED_STREAM_MAX_BYTES = 4 * 1024 * 1024
_DETACHED_STREAM_PUBLISH_WAIT_SECONDS = 5.0
_AGENT_RUN_ERROR_STORAGE_LIMIT = 4000
_RAW_ENGINE_EVENT_INLINE_BYTES = 1024 * 1024


class _NormalizedEngineResponse:
    """Expose normalized AgentEngine events through the legacy SSE parser."""

    status_code = 200

    def __init__(self, engine, request: AgentEngineRequest) -> None:
        self._engine = engine
        self._request = request

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self):
        async for event in self._engine.stream(self._request):
            delta: dict[str, object] = {}
            finish_reason = None
            model = None
            if event.kind == "content":
                delta["content"] = str(event.data.get("text") or "")
            elif event.kind == "reasoning":
                delta["reasoning"] = str(event.data.get("text") or "")
            elif event.kind == "tool_progress":
                delta["tool_progress"] = str(event.data.get("text") or "")
            elif event.kind == "agent_event":
                delta["agent_event"] = dict(event.data)
            elif event.kind == "approval":
                delta["approval"] = dict(event.data)
            elif event.kind == "usage":
                delta["usage"] = dict(event.data)
            elif event.kind == "model":
                model = str(event.data.get("resolved_model_id") or "") or None
            elif event.kind == "finish":
                finish_reason = str(event.data.get("finish_reason") or "stop")
            elif event.kind == "diagnostic":
                code = str(event.data.get("code") or "engine_diagnostic")
                if code in {
                    "claude_result_error",
                    "claude_runner_failed",
                    "malformed_claude_runner_event",
                }:
                    delta["error"] = str(event.data.get("message") or code)
                else:
                    delta["engine_diagnostic"] = dict(event.data)
            payload: dict[str, object] = {
                "choices": [{"delta": delta, "finish_reason": finish_reason}],
            }
            if model:
                payload["model"] = model
            if event.raw is not None:
                payload["engine_raw_event"] = dict(event.raw)
            yield "data: " + json.dumps(payload, ensure_ascii=False)
        yield "data: [DONE]"


@asynccontextmanager
async def _open_agent_engine_stream(
    *,
    engine_id: str,
    request: AgentEngineRequest,
    legacy_payload: dict,
):
    if engine_id == ENGINE_ID_LEGACY:
        if not settings.legacy_engine_new_runs_enabled:
            raise HTTPException(
                409,
                "Legacy Harness execution is disabled for new Turns",
            )
        async with httpx.AsyncClient(
            timeout=settings.harness_stream_timeout_seconds
        ) as client:
            async with client.stream(
                "POST",
                f"{settings.harness_url}/v1/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "X-Internal-Token": settings.internal_api_token,
                },
                json=legacy_payload,
            ) as response:
                yield response
        return
    from agent_engines.registry import build_agent_engine_registry

    engine = build_agent_engine_registry().get(engine_id)
    yield _NormalizedEngineResponse(engine, request)


def _bounded_agent_run_error(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= _AGENT_RUN_ERROR_STORAGE_LIMIT:
        return text
    return text[:_AGENT_RUN_ERROR_STORAGE_LIMIT - 1] + "…"


async def _persist_engine_raw_events(
    *,
    user_id: str,
    conversation_id: str,
    run_id: str,
    engine_id: str,
    envelopes: list[dict],
) -> None:
    if not envelopes:
        return
    prepared: dict[int, dict[str, object]] = {}
    for envelope in envelopes:
        seq = envelope.get("seq")
        if not isinstance(seq, int) or seq < 1:
            continue
        full_payload = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded = full_payload.encode("utf-8")
        payload_sha256 = hashlib.sha256(encoded).hexdigest()
        native = envelope.get("event")
        native_type = (
            str(native.get("type") or "")[:96]
            if isinstance(native, dict)
            else None
        )
        native_event_id = None
        if isinstance(native, dict):
            candidate = native.get("uuid") or native.get("id") or native.get("task_id")
            if candidate is not None:
                native_event_id = str(candidate)[:192]
        if len(encoded) > _RAW_ENGINE_EVENT_INLINE_BYTES:
            full_payload = json.dumps(
                {
                    "storage": "claude_runner_session_ledger",
                    "inline": False,
                    "size_bytes": len(encoded),
                    "payload_sha256": payload_sha256,
                },
                separators=(",", ":"),
            )
        row = {
            "seq": seq,
            "payload": full_payload,
            "payload_sha256": payload_sha256,
            "native_event_id": native_event_id,
            "native_event_type": native_type,
        }
        previous = prepared.get(seq)
        if previous is not None and previous["payload_sha256"] != payload_sha256:
            raise RuntimeError("Conflicting native Agent Engine sequence in one batch")
        prepared[seq] = row
    if not prepared:
        return

    async def persist_once() -> None:
        async with async_session() as event_db:
            existing_rows = (await event_db.execute(
                select(AgentEngineRawEvent).where(
                    AgentEngineRawEvent.run_id == run_id,
                    AgentEngineRawEvent.seq.in_(tuple(prepared)),
                )
            )).scalars().all()
            existing = {row.seq: row for row in existing_rows}
            for seq, row in sorted(prepared.items()):
                prior = existing.get(seq)
                if prior is not None:
                    if prior.payload_sha256 != row["payload_sha256"]:
                        raise RuntimeError(
                            "Native Agent Engine sequence replay changed payload"
                        )
                    continue
                event_db.add(AgentEngineRawEvent(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    engine_id=engine_id,
                    seq=seq,
                    native_event_id=row["native_event_id"],
                    native_event_type=row["native_event_type"],
                    payload=str(row["payload"]),
                    payload_sha256=str(row["payload_sha256"]),
                ))
            await event_db.commit()

    try:
        await _run_sqlite_persist_with_retry(
            persist_once,
            description=f"native engine event persistence run={run_id}",
        )
    except Exception:
        # Native events are part of the execution audit contract.  Do not
        # silently claim a fully durable Turn when their index conflicts or
        # cannot be committed; the caller's stream barrier will fail closed.
        logger.exception(
            "Native Agent Engine event index persistence failed run=%s",
            run_id,
        )
        raise


async def _persist_turn_activity_events(
    *,
    user_id: str,
    conversation_id: str,
    root_run_id: str,
    events: list[dict],
) -> None:
    """Idempotently append a bounded safe presentation batch."""

    if not events:
        return
    prepared: dict[int, dict] = {}
    for event in events:
        if event.get("root_run_id") != root_run_id:
            raise RuntimeError("Turn activity root identity mismatch")
        seq = event.get("seq")
        if not isinstance(seq, int) or seq < 1:
            raise RuntimeError("Turn activity sequence is invalid")
        payload = json.dumps(
            event.get("payload") or {},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        candidate = {
            "id": str(event.get("event_id") or "")[:32],
            "run_id": str(event.get("run_id") or root_run_id)[:32],
            "seq": seq,
            "node_id": str(event.get("node_id") or "")[:192],
            "kind": str(event.get("kind") or "progress")[:32],
            "operation": str(event.get("operation") or "append")[:16],
            "payload": payload,
        }
        if not candidate["id"] or not candidate["node_id"]:
            raise RuntimeError("Turn activity identity is invalid")
        previous = prepared.get(seq)
        if previous is not None and previous != candidate:
            raise RuntimeError("Conflicting Turn activity sequence in one batch")
        prepared[seq] = candidate

    async def persist_once() -> None:
        async with async_session() as event_db:
            rows = (await event_db.execute(
                select(TurnActivityEvent).where(
                    TurnActivityEvent.root_run_id == root_run_id,
                    TurnActivityEvent.seq.in_(tuple(prepared)),
                )
            )).scalars().all()
            existing = {row.seq: row for row in rows}
            for seq, candidate in sorted(prepared.items()):
                prior = existing.get(seq)
                if prior is not None:
                    prior_contract = (
                        prior.id, prior.run_id, prior.node_id, prior.kind,
                        prior.operation, prior.payload,
                    )
                    incoming_contract = (
                        candidate["id"], candidate["run_id"],
                        candidate["node_id"], candidate["kind"],
                        candidate["operation"], candidate["payload"],
                    )
                    if prior_contract != incoming_contract:
                        raise RuntimeError(
                            "Turn activity replay changed a durable sequence"
                        )
                    continue
                event_db.add(TurnActivityEvent(
                    id=candidate["id"],
                    user_id=user_id,
                    conversation_id=conversation_id,
                    root_run_id=root_run_id,
                    run_id=candidate["run_id"],
                    seq=seq,
                    node_id=candidate["node_id"],
                    kind=candidate["kind"],
                    operation=candidate["operation"],
                    payload=candidate["payload"],
                ))
            await event_db.commit()

    await _run_sqlite_persist_with_retry(
        persist_once,
        description=f"turn activity persistence run={root_run_id}",
    )


class _DetachedStreamRelay:
    """Relay one accepted chat turn without coupling it to one HTTP client.

    The upstream Harness request belongs to the durable AgentRun, not to the
    browser connection that happened to start it.  Starlette cancels a
    ``StreamingResponse`` body iterator when the browser refreshes or a proxy
    drops the socket.  Keeping the producer in a separately tracked task lets
    it continue projecting events and the final assistant message while this
    small relay simply stops forwarding bytes to the departed subscriber.

    The queue is bounded and applies backpressure while a subscriber is
    attached. Detach clears it and wakes a blocked publisher; future publishes
    become no-ops, so neither a slow nor a departed client can cause unbounded
    memory growth.
    """

    __slots__ = (
        "_queue",
        "_attached",
        "_finished",
        "_error",
        "_queued_bytes",
        "_detach_reason",
    )

    def __init__(self) -> None:
        self._queue: asyncio.Queue[object] = asyncio.Queue(
            maxsize=_DETACHED_STREAM_MAX_CHUNKS
        )
        self._attached = True
        self._finished = False
        self._error: BaseException | None = None
        self._queued_bytes = 0
        self._detach_reason: str | None = None

    async def publish(self, chunk: str) -> None:
        if not self._attached or self._finished:
            return
        chunk_bytes = len(chunk.encode("utf-8", "replace"))
        if (
            chunk_bytes > _DETACHED_STREAM_MAX_BYTES
            or self._queued_bytes + chunk_bytes
            > _DETACHED_STREAM_MAX_BYTES
        ):
            self.detach("subscriber_backpressure")
            return
        try:
            await asyncio.wait_for(
                self._queue.put((chunk, chunk_bytes)),
                timeout=_DETACHED_STREAM_PUBLISH_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            # A half-open or stalled browser must not stop the durable
            # Backend→Harness producer. Drop this subscriber and continue the
            # accepted run in the background.
            self.detach("subscriber_backpressure")
            return
        self._queued_bytes += chunk_bytes
        if not self._attached:
            # Detach may have woken this publisher by draining a full queue.
            # Drop the one item it raced to publish; no consumer remains.
            self._drain_queue()
            self._queued_bytes = 0

    def finish(self, error: BaseException | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        self._error = error
        # If buffered content remains, the consumer observes it first and then
        # notices _finished at the top of its next loop. An empty queue needs a
        # sentinel to wake a consumer already blocked in get().
        if self._attached and self._queue.empty():
            self._queue.put_nowait(_DETACHED_STREAM_DONE)

    def _drain_queue(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[1], int)
            ):
                self._queued_bytes = max(
                    0,
                    self._queued_bytes - item[1],
                )

    def detach(self, reason: str = "consumer_closed") -> None:
        if not self._attached:
            return
        self._attached = False
        self._detach_reason = reason
        self._drain_queue()
        self._queued_bytes = 0
        # Wake a consumer blocked in get(). The sentinel is not buffered model
        # content and therefore does not count against the byte budget.
        self._queue.put_nowait(_DETACHED_STREAM_DONE)

    async def stream(self):
        try:
            while True:
                if self._finished and self._queue.empty():
                    if self._error is not None:
                        raise self._error
                    return
                item = await self._queue.get()
                if item is _DETACHED_STREAM_DONE:
                    if self._error is not None:
                        raise self._error
                    return
                chunk, chunk_bytes = item
                self._queued_bytes = max(
                    0,
                    self._queued_bytes - int(chunk_bytes),
                )
                yield str(chunk)
        finally:
            self.detach()

    @property
    def detach_reason(self) -> str | None:
        return self._detach_reason


def _track_best_effort_task(
    operation: Awaitable[None],
    *,
    description: str,
) -> asyncio.Task:
    """Retain a non-critical task without making it part of a durable barrier."""

    task = asyncio.create_task(operation)
    _best_effort_tasks.add(task)

    def finish(completed: asyncio.Task) -> None:
        _best_effort_tasks.discard(completed)
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.warning(
                "Best-effort background task failed: %s",
                description,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(finish)
    return task


def _track_detached_chat_producer(
    *,
    conv_id: str,
    run_id: str,
    relay: _DetachedStreamRelay,
    operation: Awaitable[None],
    producer_started: Callable[[], bool] | None = None,
    on_prestart_exit: Callable[[BaseException | None], None] | None = None,
) -> asyncio.Task:
    """Run the accepted turn independently and retain/log its lifecycle."""

    task = asyncio.create_task(operation)
    _background_tasks.add(task)
    _detached_chat_producers.add(task)
    _detached_chat_producers_by_conversation.setdefault(
        str(conv_id),
        set(),
    ).add(task)

    def finish(completed: asyncio.Task) -> None:
        _background_tasks.discard(completed)
        _detached_chat_producers.discard(completed)
        conversation_tasks = (
            _detached_chat_producers_by_conversation.get(str(conv_id))
        )
        if conversation_tasks is not None:
            conversation_tasks.discard(completed)
            if not conversation_tasks:
                _detached_chat_producers_by_conversation.pop(
                    str(conv_id),
                    None,
                )
        error: BaseException | None = None
        try:
            error = completed.exception()
        except asyncio.CancelledError as exc:
            error = exc
        relay.finish(error)
        if (
            on_prestart_exit is not None
            and producer_started is not None
            and not producer_started()
        ):
            try:
                on_prestart_exit(error)
            except Exception:
                logger.exception(
                    "Detached chat pre-start recovery failed conv=%s run=%s",
                    conv_id,
                    run_id,
                )
        if error is not None and not isinstance(error, asyncio.CancelledError):
            logger.error(
                "Detached chat producer failed conv=%s run=%s",
                conv_id,
                run_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(finish)
    return task


async def cancel_conversation_producers(
    conv_id: str,
    *,
    grace_seconds: float = 15.0,
) -> dict[str, int | bool]:
    """Cancel and drain every detached producer owned by one conversation."""

    tasks = {
        task
        for task in _detached_chat_producers_by_conversation.get(
            str(conv_id),
            set(),
        )
        if not task.done()
    }
    for task in tasks:
        task.cancel()
    timeout = max(0.1, min(float(grace_seconds), 60.0))
    done: set[asyncio.Task] = set()
    residual: set[asyncio.Task] = set()
    if tasks:
        done, residual = await asyncio.wait(
            tasks,
            timeout=timeout,
            return_when=asyncio.ALL_COMPLETED,
        )
    for task in done:
        try:
            task.exception()
        except BaseException:
            pass
    barrier_tasks: set[asyncio.Task] = set()
    state = _conversation_turn_states.get(str(conv_id))
    if state is not None:
        # Failed projections intentionally remain tracked until a lifecycle
        # barrier observes them. Include already-completed failures here:
        # deletion has published a durable tombstone and owns the explicit
        # purge, so an expected fail-closed projection must be consumed rather
        # than making the first delete attempt fail with a stale barrier.
        barrier_tasks.update(state.projection_tasks)
    for (task_conv_id, _root_run_id), pending in list(
        _agent_event_persist_tasks.items()
    ):
        if task_conv_id == str(conv_id):
            barrier_tasks.update(
                task for task in pending if not task.done()
            )
    barrier_done: set[asyncio.Task] = set()
    barrier_residual: set[asyncio.Task] = set()
    if not residual and barrier_tasks:
        barrier_done, barrier_residual = await asyncio.wait(
            barrier_tasks,
            timeout=timeout,
            return_when=asyncio.ALL_COMPLETED,
        )
        for task in barrier_done:
            try:
                task.exception()
            except BaseException:
                pass
        # Delete only needs proof that every projection stopped. A projection
        # may fail closed because the durable tombstone revoked its commit
        # authority; consume that expected failure so the maintenance lease
        # can proceed to the explicit database purge.
        if state is not None:
            for task in barrier_done:
                state.projection_tasks.discard(task)
            _cleanup_conversation_turn_state(str(conv_id), state)
    return {
        "success": not residual and not barrier_residual,
        "cancelled_count": len(done),
        "residual_count": len(residual),
        "projection_drained_count": len(barrier_done),
        "projection_residual_count": len(barrier_residual),
    }


async def shutdown_chat_background_tasks(
    *,
    producer_cancel_seconds: float = 3.0,
    projection_grace_seconds: float = 8.0,
    best_effort_cancel_seconds: float = 1.0,
) -> None:
    """Cancel accepted producers, then give their terminal projections grace.

    Browser disconnects do not call this function.  It is reserved for an
    actual Backend lifespan shutdown, where cancellation is unavoidable but
    must still be attributed and projected before the process exits.
    """

    best_effort = [
        task for task in _best_effort_tasks
        if not task.done()
    ]
    for task in best_effort:
        task.cancel()

    # Registered non-chat executions (notably scheduled jobs) live in the
    # per-conversation registry rather than _detached_chat_producers. Shutdown
    # must cancel the union or a long cron can outlive Backend lifespan.
    producers = {
        task for task in _detached_chat_producers
        if not task.done()
    }
    for conversation_tasks in (
        _detached_chat_producers_by_conversation.values()
    ):
        producers.update(
            task for task in conversation_tasks
            if not task.done()
        )
    for task in producers:
        task.cancel()
    if producers:
        _done, pending_producers = await asyncio.wait(
            producers,
            timeout=max(0.1, float(producer_cancel_seconds)),
        )
        if pending_producers:
            logger.error(
                "Backend producer cancellation deadline expired with %s "
                "producer(s)",
                len(pending_producers),
            )

    projections = [
        task for task in _background_tasks
        if not task.done()
    ]
    if projections:
        _done, pending = await asyncio.wait(
            projections,
            timeout=max(0.1, float(projection_grace_seconds)),
        )
        if pending:
            logger.error(
                "Backend shutdown projection grace expired with %s task(s)",
                len(pending),
            )
    late_best_effort = [
        task for task in _best_effort_tasks
        if not task.done()
    ]
    for task in late_best_effort:
        task.cancel()
    if late_best_effort:
        _done, pending_best_effort = await asyncio.wait(
            late_best_effort,
            timeout=max(0.1, float(best_effort_cancel_seconds)),
        )
        if pending_best_effort:
            logger.warning(
                "Backend best-effort cancellation deadline expired with %s "
                "task(s)",
                len(pending_best_effort),
            )


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
        # Failed completed tasks deliberately remain tracked until this
        # barrier consumes them.  Looking only at pending tasks would forget a
        # projection that failed between the disconnect and the next turn.
        tracked = list(state.projection_tasks)
        if not tracked:
            return
        # The projection belongs to the conversation, not to the next request.
        # Shielding it ensures cancellation of that waiter cannot cancel the
        # durable write it is waiting for.
        results = await asyncio.gather(
            *(asyncio.shield(task) for task in tracked),
            return_exceptions=True,
        )
        for task in tracked:
            state.projection_tasks.discard(task)
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


@asynccontextmanager
async def conversation_maintenance_lease(conv_id: str):
    """Exclude new turns while a lifecycle operation snapshots or deletes.

    The lease also drains any terminal projection left by a producer that was
    cancelled or detached immediately before the maintenance operation.
    """

    lease = await _acquire_conversation_turn(str(conv_id))
    try:
        yield
    finally:
        _release_conversation_turn(str(conv_id), lease)


@asynccontextmanager
async def registered_conversation_producer(
    user_id: str,
    conv_id: str,
):
    """Register one cancellable producer without acquiring its turn lease.

    This is the lifecycle half of scheduled execution.  A producer which
    calls the ordinary chat path must let that path acquire the conversation
    lease itself; acquiring it here as well would recursively wait on the
    same non-reentrant lock.  Direct producers use
    ``registered_conversation_execution`` below to compose registration with
    exactly one maintenance lease.
    """

    conversation_key = str(conv_id)
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Conversation execution requires an asyncio task.")
    require_session_workspace_active(str(user_id), conversation_key)
    _background_tasks.add(task)
    _detached_chat_producers_by_conversation.setdefault(
        conversation_key,
        set(),
    ).add(task)
    try:
        require_session_workspace_active(str(user_id), conversation_key)
        yield
    finally:
        conversation_tasks = _detached_chat_producers_by_conversation.get(
            conversation_key
        )
        if conversation_tasks is not None:
            conversation_tasks.discard(task)
            if not conversation_tasks:
                _detached_chat_producers_by_conversation.pop(
                    conversation_key,
                    None,
                )
        _background_tasks.discard(task)


@asynccontextmanager
async def registered_conversation_execution(
    user_id: str,
    conv_id: str,
):
    """Register a direct producer and hold exactly one conversation lease."""

    conversation_key = str(conv_id)
    async with registered_conversation_producer(user_id, conversation_key):
        async with conversation_maintenance_lease(conversation_key):
            require_session_workspace_active(
                str(user_id),
                conversation_key,
            )
            yield


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
        try:
            error = completed.exception()
        except asyncio.CancelledError as exc:
            error = exc
        durable = False
        if error is None:
            try:
                durable = completed.result() is True
            except asyncio.CancelledError as exc:
                error = exc
            except Exception as exc:
                error = exc
        # A successful projection no longer needs a future barrier.  Failed,
        # cancelled, and non-durable results remain in the state until the
        # next acquire observes and rejects them.
        if durable:
            state.projection_tasks.discard(completed)
        if error is not None:
            logger.error(
                "Uncaught terminal conversation projection failure conv=%s",
                conv_id,
                exc_info=(type(error), error, error.__traceback__),
            )
        elif not durable:
            logger.error(
                "Terminal conversation projection returned a non-durable "
                "result conv=%s",
                conv_id,
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


def _event_is_authoritative(
    event: dict,
    payload: dict | None = None,
) -> bool:
    """Apply the Harness terminal-authority precedence exactly once.

    The run-contract adapter gives an explicit top-level boolean precedence
    over the payload field, then treats ``provisional_terminal`` as
    non-authoritative.  Backend persistence must use the same rule or one
    event can be provisional in the Harness and terminal in the projection.
    """

    top_level = event.get("authoritative")
    if isinstance(top_level, bool):
        return top_level
    effective_payload = (
        payload if isinstance(payload, dict) else _event_payload(event)
    )
    nested = effective_payload.get("authoritative")
    if isinstance(nested, bool):
        return nested
    if effective_payload.get("provisional_terminal") is True:
        return False
    return True


def _normalized_event_payload(
    event: dict,
    payload: dict | None = None,
) -> dict:
    """Return the durable payload with terminal authority canonicalized."""

    normalized = dict(
        payload if isinstance(payload, dict) else _event_payload(event)
    )
    if str(event.get("event_type") or "") in {
        "run.completed",
        "run.failed",
        "run.cancelled",
    }:
        normalized["authoritative"] = _event_is_authoritative(
            event,
            normalized,
        )
    return normalized


def _event_key(event: dict, run_id: str) -> str:
    return f"{run_id}:{event.get('event_type') or 'unknown'}:{int(event.get('seq') or 0)}"


def _event_contract_fingerprint(
    event: dict,
    *,
    payload: dict | None = None,
) -> str:
    """Hash the durable, projection-relevant identity of one event.

    The hash is used only for replay/conflict decisions and never logs raw
    payloads. Exact replay is idempotent; a different payload under the same
    ``(run_id, event_type, seq)`` key is rejected before any projection.
    """

    effective_payload = _normalized_event_payload(event, payload)
    projection = {
        "run_id": str(event.get("run_id") or ""),
        "event_type": str(event.get("event_type") or ""),
        "seq": int(event.get("seq") or 0),
        "parent_run_id": event.get("parent_run_id"),
        "payload": effective_payload,
        "tool_name": event.get("tool_name")
        or effective_payload.get("tool_name"),
        "tool_call_id": event.get("tool_call_id")
        or effective_payload.get("tool_call_id"),
    }
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _persisted_event_contract_fingerprint(row: object) -> str:
    try:
        payload = json.loads(getattr(row, "payload", None) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return _event_contract_fingerprint(
        {
            "run_id": getattr(row, "run_id", ""),
            "event_type": getattr(row, "event_type", ""),
            "seq": int(getattr(row, "seq", 0) or 0),
            "parent_run_id": getattr(row, "parent_run_id", None),
            "tool_name": getattr(row, "tool_name", None),
            "tool_call_id": getattr(row, "tool_call_id", None),
        },
        payload=payload,
    )


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
        payload = _normalized_event_payload(event)
        if (
            event_type in {"run.completed", "run.failed", "run.cancelled"}
            and not _event_is_authoritative(event, payload)
        ):
            # Nested/provisional convergence terminals are observability, not
            # the root run's committed outcome.  Absence remains authoritative
            # for backwards-compatible producers.
            continue
        if event_type == "run.completed":
            terminal_type = event_type
            terminal_error = None
            break
        elif event_type == "run.failed":
            terminal_type = event_type
            terminal_error = str(payload.get("error") or "Agent run failed.")[:4000]
            break
        elif event_type == "run.cancelled":
            terminal_type = event_type
            terminal_error = None
            break
    if terminal_type == "run.failed":
        return "failed", terminal_error or "Agent run failed."
    if terminal_type == "run.completed":
        return "succeeded", None
    if terminal_type == "run.cancelled":
        return "cancelled", None
    return None, None


def _authoritative_root_terminal_payload(
    agent_events: list[dict],
    *,
    run_id: str,
) -> tuple[str | None, dict]:
    """Return the first authoritative root terminal and its payload."""

    for event in sorted(
        agent_events or [], key=lambda item: int(item.get("seq") or 0)
    ):
        if str(event.get("run_id") or "") != run_id:
            continue
        event_type = str(event.get("event_type") or "")
        if event_type not in {"run.completed", "run.failed", "run.cancelled"}:
            continue
        payload = _normalized_event_payload(event)
        if not _event_is_authoritative(event, payload):
            continue
        return event_type, payload
    return None, {}


def _authoritative_root_finish_reason(
    agent_events: list[dict],
    *,
    run_id: str,
    transport_finish_reason: str | None,
) -> str | None:
    """Project the root event reason into the client terminal envelope.

    An OpenAI-compatible stream commonly ends with transport ``stop`` even
    when the Harness has already emitted an authoritative ``run.failed`` with
    a domain finish reason.  The event is the run authority; the transport
    reason is only a fallback when no authoritative terminal exists.
    """

    terminal_type, payload = _authoritative_root_terminal_payload(
        agent_events,
        run_id=run_id,
    )
    if terminal_type is None:
        return transport_finish_reason
    event_reason = payload.get("finish_reason") or payload.get(
        "terminal_reason"
    )
    if isinstance(event_reason, str) and event_reason.strip():
        return event_reason.strip()
    return {
        "run.completed": "stop",
        "run.failed": "agent_run_failed",
        "run.cancelled": "task_cancelled",
    }.get(terminal_type, transport_finish_reason)


def _reconcile_root_stream_error(
    agent_events: list[dict],
    *,
    run_id: str,
    stream_error: str | None,
) -> tuple[str | None, bool]:
    """Return the durable chat error and whether it is an execution failure.

    A committed root terminal is authoritative over incidental child failures
    and over a late transport symptom.  Without a committed root terminal, an
    HTTP/SSE error (or the missing-terminal condition itself) is a genuine
    stream interruption.
    """

    terminal_status, terminal_error = _agent_event_terminal_status(
        agent_events,
        run_id=run_id,
    )
    if terminal_status == "failed":
        transport_interruption = any(
            str(event.get("run_id") or "") == run_id
            and str(event.get("event_type") or "") == "run.failed"
            and _event_is_authoritative(
                event,
                _normalized_event_payload(event),
            )
            and str(
                _normalized_event_payload(event).get("failure_class") or ""
            ) == "stream_transport"
            for event in sorted(
                agent_events or [],
                key=lambda item: int(item.get("seq") or 0),
            )
        )
        return terminal_error or "Agent run failed.", not transport_interruption
    if terminal_status == "succeeded":
        return None, False
    if terminal_status == "cancelled":
        return "Harness reported that the root run was cancelled before completion.", False
    return (
        stream_error or "Harness stream ended without a terminal run event.",
        False,
    )


def _chat_stream_failure_notice(
    error_message: str,
    *,
    execution_failed: bool,
    has_partial_content: bool,
) -> str:
    prefix = "\n\n---\n" if has_partial_content else ""
    if execution_failed:
        suffix = (
            "\n已显示的是未完成草稿，请修复失败原因后重试。"
            if has_partial_content
            else ""
        )
        return f"{prefix}⚠️ 本次任务执行失败：{error_message}{suffix}"
    suffix = (
        "\n已显示的是不完整草稿，请重新发送或点击重试。"
        if has_partial_content
        else ""
    )
    return (
        f"{prefix}⚠️ 本次响应在流式输出过程中中断："
        f"{error_message}{suffix}"
    )


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


def _seal_missing_root_terminal_for_projection(
    agent_events: list[dict],
    *,
    run_id: str,
    usage: object,
    finish_reason: str,
    error_message: str | None,
    persisted_terminal: dict | None = None,
    persisted_max_seq: int = 0,
) -> tuple[list[dict], str, str | None]:
    """Give every durable root projection one authoritative terminal event.

    A Harness HTTP error, timeout, abrupt EOF, or Backend stream exception can
    end before a root terminal crosses the bridge. Persisting only a failed
    ``AgentRun`` row leaves its root task and descendants nonterminal because
    lifecycle projection is event-driven. Synthesize one explicitly
    attributed transport failure so the root run, root task, descendants, and
    refresh cards cross the same atomic terminal boundary.
    """

    events = list(agent_events or [])
    if isinstance(persisted_terminal, dict):
        persisted_fingerprint = _event_contract_fingerprint(
            persisted_terminal
        )
        retained: list[dict] = []
        retained_persisted = False
        for event in events:
            is_root_terminal = (
                str(event.get("run_id") or "") == run_id
                and str(event.get("event_type") or "") in {
                    "run.completed",
                    "run.failed",
                    "run.cancelled",
                }
                and _event_is_authoritative(
                    event,
                    _normalized_event_payload(event),
                )
            )
            if not is_root_terminal:
                retained.append(event)
                continue
            if (
                not retained_persisted
                and _event_contract_fingerprint(event)
                == persisted_fingerprint
            ):
                retained.append(persisted_terminal)
                retained_persisted = True
        if not retained_persisted:
            retained.append(persisted_terminal)
        events = retained
    terminal_status, _terminal_error = _agent_event_terminal_status(
        events,
        run_id=run_id,
    )
    if terminal_status is not None:
        return events, finish_reason, error_message

    terminal_reason = "missing_authoritative_root_terminal"
    durable_error = (
        str(error_message).strip()
        if isinstance(error_message, str) and error_message.strip()
        else "Harness stream ended without an authoritative root terminal event."
    )
    root_seq = max(
        [max(0, int(persisted_max_seq or 0)), *(
            int(event.get("seq") or 0)
            for event in events
            if str(event.get("run_id") or "") == run_id
        )],
    )
    termination_source = "upstream_stream_missing_terminal"
    for event in reversed(events):
        if (
            str(event.get("run_id") or "") == run_id
            and event.get("event_type") == "debug.backend_stream.terminated"
        ):
            payload = _normalized_event_payload(event)
            termination_source = (
                _safe_diagnostic_label(payload.get("termination_source"))
                or termination_source
            )
            break
    events.append({
        "type": "agent_event",
        "event_type": "run.failed",
        "run_id": run_id,
        "root_run_id": run_id,
        "parent_run_id": None,
        "agent_kind": "primary",
        "agent_name": "primary",
        "depth": 0,
        "workspace_scope": "shared_session",
        "seq": root_seq + 1,
        "authoritative": True,
        "payload": {
            "authoritative": True,
            "error": durable_error[:4000],
            "finish_reason": terminal_reason,
            "terminal_reason": terminal_reason,
            "failure_class": "stream_transport",
            "termination_source": termination_source,
            "retryable": True,
            "usage": _normalized_usage(usage),
        },
    })
    return events, terminal_reason, durable_error


async def _persisted_authoritative_root_terminal(
    *,
    conv_id: str,
    run_id: str,
) -> tuple[dict | None, int]:
    """Load the first already-durable root terminal for projection replay."""

    async with async_session() as s:
        rows = (await s.execute(
            select(AgentRunEvent).where(
                AgentRunEvent.conversation_id == conv_id,
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.event_type.in_(
                    ("run.completed", "run.failed", "run.cancelled")
                ),
            ).order_by(
                AgentRunEvent.seq,
                AgentRunEvent.event_time,
                AgentRunEvent.id,
            )
        )).scalars().all()
        max_seq = (await s.execute(
            select(func.max(AgentRunEvent.seq)).where(
                AgentRunEvent.conversation_id == conv_id,
                AgentRunEvent.run_id == run_id,
            )
        )).scalar_one_or_none()
    for row in rows:
        try:
            payload = json.loads(row.payload or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        event = {
            "type": "agent_event",
            "event_type": str(row.event_type or ""),
            "run_id": str(row.run_id or ""),
            "root_run_id": run_id,
            "parent_run_id": row.parent_run_id,
            "agent_kind": "primary",
            "agent_name": "primary",
            "depth": 0,
            "workspace_scope": "shared_session",
            "seq": int(row.seq or 0),
            "payload": payload,
        }
        if _event_is_authoritative(event, payload):
            return event, max(0, int(max_seq or 0))
    return None, max(0, int(max_seq or 0))


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
            workspace = await ensure_workspace_async(user_id, conv_id)
            file_path = safe_workspace_path_in_root(
                workspace,
                path,
                must_exist=True,
            )
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
    project_lifecycle: bool = True,
) -> None:
    task_key = _task_key_for_event(event, payload, run_id)
    if not task_key:
        return
    # Replayed, provisional, stale, or conflicting lifecycle observations
    # must not mutate even descriptive task projection fields.
    if not project_lifecycle:
        return
    event_type = str(event.get("event_type") or "")
    now = datetime.utcnow()
    agent_kind = str(event.get("agent_kind") or "primary")
    nested_agent = bool(event.get("parent_run_id")) or agent_kind != "primary"
    if event_type.startswith("verifier."):
        kind = "verification"
        title = str(payload.get("verifier_kind") or "Verifier")[:256]
    else:
        kind = "delegate" if nested_agent else "primary"
        title = str(event.get("agent_name") or payload.get("goal") or agent_kind or "Run")[:256]
    task = task_items.get(task_key)
    if (
        task is not None
        and task.status in {"succeeded", "failed", "cancelled", "blocked"}
        and event_type in {
            "agent.spawned",
            "run.started",
            "verifier.requested",
        }
    ):
        return
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
        task.summary = str(
            payload.get("finish_reason")
            or payload.get("terminal_reason")
            or "agent_run_failed"
        )[:4000]
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
    defer_root_terminal: bool = False,
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
    persisted_event_fingerprints: dict[
        tuple[str, str, int], str
    ] = {}
    authoritative_terminal_seq: dict[str, int] = {}
    if event_run_ids:
        rows = (await s.execute(
            select(AgentRunEvent).where(
                AgentRunEvent.conversation_id == conv_id,
                AgentRunEvent.run_id.in_(event_run_ids),
            )
        )).scalars().all()
        persisted_event_fingerprints = {
            (
                str(row.run_id),
                str(row.event_type),
                int(row.seq or 0),
            ): _persisted_event_contract_fingerprint(row)
            for row in rows
        }
        for row in rows:
            persisted_type = str(row.event_type or "")
            if persisted_type not in {
                "run.completed", "run.failed", "run.cancelled",
            }:
                continue
            try:
                persisted_payload = json.loads(row.payload or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                persisted_payload = {}
            if not isinstance(persisted_payload, dict):
                persisted_payload = {}
            if not _event_is_authoritative(
                {
                    "event_type": persisted_type,
                    "payload": persisted_payload,
                },
                persisted_payload,
            ):
                continue
            persisted_run_id = str(row.run_id)
            authoritative_terminal_seq.setdefault(
                persisted_run_id,
                int(row.seq or 0),
            )
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
        payload = _normalized_event_payload(event)
        event_type = str(event.get("event_type") or "unknown")
        if event_type in {"agent.delta", "agent.reasoning_delta"}:
            continue
        terminal_event = event_type in {
            "run.completed", "run.failed", "run.cancelled",
        }
        seq = int(event.get("seq") or 0)
        event_key = (run_id, event_type, seq)
        incoming_fingerprint = _event_contract_fingerprint(
            event,
            payload=payload,
        )
        persisted_fingerprint = persisted_event_fingerprints.get(event_key)
        event_already_persisted = persisted_fingerprint is not None
        replay_deferred_root_terminal = bool(
            event_already_persisted
            and persisted_fingerprint == incoming_fingerprint
            and terminal_event
            and run_id == root_run_id
            and not defer_root_terminal
            and existing_runs.get(run_id) is not None
            and existing_runs[run_id].status == "committing"
        )
        if event_already_persisted:
            if persisted_fingerprint != incoming_fingerprint:
                logger.warning(
                    "Rejected conflicting agent event replay "
                    "conv=%s run=%s type=%s seq=%s",
                    conv_id,
                    run_id,
                    event_type,
                    seq,
                )
            if not replay_deferred_root_terminal:
                continue
        authoritative_terminal = bool(
            terminal_event and _event_is_authoritative(event, payload)
        )
        known_terminal_seq = authoritative_terminal_seq.get(run_id, -1)
        project_lifecycle = (
            not event_already_persisted
            or replay_deferred_root_terminal
        )
        if (
            event_type
            in {"agent.spawned", "run.started", "verifier.requested"}
            and known_terminal_seq >= 0
        ):
            # A delayed/replayed start cannot reopen a durably terminal run.
            project_lifecycle = False
        if terminal_event:
            if replay_deferred_root_terminal:
                project_lifecycle = authoritative_terminal
            elif not authoritative_terminal or known_terminal_seq >= 0:
                project_lifecycle = False
            elif project_lifecycle:
                authoritative_terminal_seq[run_id] = seq
        defer_this_root_terminal = bool(
            defer_root_terminal
            and run_id == root_run_id
            and terminal_event
            and authoritative_terminal
            and project_lifecycle
        )
        if defer_this_root_terminal:
            project_lifecycle = False
        if run_id not in existing_runs:
            incoming_agent_kind = str(
                event.get("agent_kind") or "delegate"
            )
            nested_agent = bool(event.get("parent_run_id")) or (
                incoming_agent_kind != "primary"
            )
            child_run = AgentRun(
                id=run_id,
                user_id=user_id,
                conversation_id=conv_id,
                parent_run_id=event.get("parent_run_id"),
                root_run_id=event.get("root_run_id") or root_run_id,
                delegation_tool_call_id=(
                    payload.get("delegation_batch_id")
                    or payload.get("delegation_tool_call_id")
                ),
                agent_kind=incoming_agent_kind,
                agent_name=event.get("agent_name"),
                depth=int(event.get("depth") or 0),
                workspace_scope=str(event.get("workspace_scope") or "shared_session"),
                source="delegate" if nested_agent else "chat",
                requested_model_id=str(payload.get("model_id") or requested_model_id),
                resolved_model_id=resolved_model_id,
                status="running",
            )
            s.add(child_run)
            existing_runs[run_id] = child_run
        run = existing_runs[run_id]
        run.parent_run_id = event.get("parent_run_id") or run.parent_run_id
        run.root_run_id = event.get("root_run_id") or run.root_run_id or root_run_id
        run.delegation_tool_call_id = (
            payload.get("delegation_batch_id")
            or payload.get("delegation_tool_call_id")
            or run.delegation_tool_call_id
        )
        run.agent_kind = str(event.get("agent_kind") or run.agent_kind or "delegate")
        run.agent_name = event.get("agent_name") or run.agent_name
        run.depth = int(event.get("depth") if event.get("depth") is not None else run.depth or 0)
        run.workspace_scope = str(event.get("workspace_scope") or run.workspace_scope or "shared_session")
        if defer_this_root_terminal:
            # The terminal event is durable, but the assistant message and
            # root status must cross one final transaction together. Until
            # then refresh/polling must continue and a new turn must not start.
            run.status = "committing"
            run.finish_reason = "terminal_projection_pending"
            run.error = None
            run.ended_at = None
            task_key = f"run:{run_id}"
            task = task_items.get(task_key)
            if task is None:
                task = TaskItem(
                    id=uuid.uuid4().hex,
                    user_id=user_id,
                    conversation_id=conv_id,
                    run_id=run_id,
                    root_run_id=root_run_id,
                    parent_run_id=event.get("parent_run_id"),
                    task_key=task_key,
                    kind="primary",
                    title=str(
                        event.get("agent_name") or "primary"
                    )[:256],
                    status="committing",
                    agent_name=event.get("agent_name") or "primary",
                )
                s.add(task)
                task_items[task_key] = task
            task.status = "committing"
            task.summary = "terminal_projection_pending"
            task.error = None
            task.ended_at = None
            task.updated_at = datetime.utcnow()
            task.metadata_json = json.dumps(payload, ensure_ascii=False)
        if event_type == "agent.spawned" and project_lifecycle:
            run.effective_tools = json.dumps(payload.get("effective_tools") or [], ensure_ascii=False)
            run.requested_tools = json.dumps(payload.get("requested_tools") or [], ensure_ascii=False)
        elif event_type == "run.started" and project_lifecycle:
            run.status = "running"
            if payload.get("model_id"):
                run.requested_model_id = str(payload.get("model_id"))
            if payload.get("enabled_tools") is not None:
                run.effective_tools = json.dumps(payload.get("enabled_tools"), ensure_ascii=False)
        elif event_type == "tool_surface.resolved":
            # Session MCP discovery and Skill narrowing happen after
            # ``run.started``. This event is the final callable run surface.
            if payload.get("effective_tools") is not None:
                run.effective_tools = json.dumps(
                    payload.get("effective_tools") or [],
                    ensure_ascii=False,
                )
            run.policy = json.dumps(
                {
                    key: payload.get(key)
                    for key in (
                        "mode",
                        "mcp_policy",
                        "required_tool_groups",
                        "missing_tool_requirements",
                        "capability_registry_sha256",
                        "capability_catalog_revision",
                    )
                    if payload.get(key) is not None
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        elif event_type == "usage.updated":
            _merge_monotonic_run_usage(run, payload)
            run.resolved_model_id = str(payload.get("model") or run.resolved_model_id or resolved_model_id)
        elif event_type == "model.switch":
            run.resolved_model_id = str(payload.get("to_model") or run.resolved_model_id or resolved_model_id)
        elif event_type == "run.completed" and project_lifecycle:
            run.status = "succeeded"
            run.finish_reason = str(payload.get("finish_reason") or "stop")
            usage_payload = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            _merge_monotonic_run_usage(run, usage_payload)
            run.ended_at = datetime.utcnow()
        elif event_type == "run.failed" and project_lifecycle:
            run.status = "failed"
            run.error = _bounded_agent_run_error(
                payload.get("error") or "Unknown error"
            )
            run.finish_reason = str(
                payload.get("finish_reason")
                or payload.get("terminal_reason")
                or "agent_run_failed"
            )[:256]
            usage_payload = (
                payload.get("usage")
                if isinstance(payload.get("usage"), dict)
                else {}
            )
            _merge_monotonic_run_usage(run, usage_payload)
            run.ended_at = datetime.utcnow()
        elif event_type == "run.cancelled" and project_lifecycle:
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
            project_lifecycle=project_lifecycle,
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
            persisted_event_fingerprints[event_key] = incoming_fingerprint


def _should_persist_agent_event_immediately(event_type: str) -> bool:
    if not settings.agent_event_immediate_persist:
        return False
    if event_type in {"agent.delta", "agent.reasoning_delta"}:
        return False
    # Run/task/artifact recovery is a product guarantee, not a debug feature.
    # A page refresh may happen hours before terminal backfill, so lifecycle
    # events must be durable even when verbose debug tracing is disabled.
    if event_type.startswith((
        "run.",
        "usage.",
        "model.",
        "tool.",
        "tool_surface.",
        "artifact.",
        "verifier.",
        "fan_in.",
        "agent.spawned",
    )):
        return True
    return bool(
        settings.agent_debug_trace
        and event_type.startswith("debug.")
    )


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
            require_session_workspace_active(user_id, conv_id)
            conversation_exists = (await s.execute(
                select(Conversation.id).where(
                    Conversation.id == conv_id,
                    Conversation.user_id == user_id,
                )
            )).scalar_one_or_none()
            if conversation_exists is None:
                return False
            run_id = str(event.get("run_id") or "")
            event_type = str(event.get("event_type") or "")
            seq = int(event.get("seq"))
            existing = (await s.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.conversation_id == conv_id,
                    AgentRunEvent.run_id == run_id,
                    AgentRunEvent.event_type == event_type,
                    AgentRunEvent.seq == seq,
                )
            )).scalar_one_or_none()
            if existing is not None:
                incoming_fingerprint = _event_contract_fingerprint(event)
                persisted_fingerprint = (
                    _persisted_event_contract_fingerprint(existing)
                )
                if incoming_fingerprint != persisted_fingerprint:
                    logger.warning(
                        "Rejected conflicting immediate agent event replay "
                        "conv=%s run=%s type=%s seq=%s",
                        conv_id,
                        run_id,
                        event_type,
                        seq,
                    )
                    return False
                return True
            await _persist_agent_events(
                s,
                conv_id=conv_id,
                user_id=user_id,
                root_run_id=root_run_id,
                requested_model_id=requested_model_id,
                resolved_model_id=resolved_model_id,
                events=[event],
                defer_root_terminal=True,
            )
            require_session_workspace_active(user_id, conv_id)
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
            latest_run.status
            in {"pending", "planned", "queued", "running", "committing"}
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


async def _reconcile_nonterminal_descendants(
    s: AsyncSession,
    *,
    conv_id: str,
    user_id: str | None,
    root_run_id: str,
    root_terminal_status: str | None,
) -> int:
    """Close any descendant left nonterminal when its root is terminal.

    A transport can fail after the Harness has cancelled children but before
    their terminal events cross the Backend SSE bridge.  Leaving those rows as
    ``running`` makes refresh recovery lie forever.  Root terminality is the
    ownership boundary: descendants cannot remain live once that root has
    committed, so synthesize a clearly attributed cancellation projection.
    """

    if (
        not user_id
        or root_terminal_status
        not in {"succeeded", "failed", "cancelled"}
    ):
        return 0
    descendants = (await s.execute(
        select(AgentRun).where(
            AgentRun.conversation_id == conv_id,
            AgentRun.root_run_id == root_run_id,
            AgentRun.id != root_run_id,
            AgentRun.status.in_(
                ("pending", "planned", "queued", "running", "committing")
            ),
        )
    )).scalars().all()
    if not descendants:
        return 0

    now = datetime.utcnow()
    task_items: dict[str, list[TaskItem]] = {}
    for task in (await s.execute(
        select(TaskItem).where(
            TaskItem.conversation_id == conv_id,
            TaskItem.root_run_id == root_run_id,
            TaskItem.status.in_(
                ("pending", "planned", "queued", "running", "committing")
            ),
        )
    )).scalars().all():
        task_items.setdefault(str(task.run_id), []).append(task)
    for child in descendants:
        max_seq = (await s.execute(
            select(func.max(AgentRunEvent.seq)).where(
                AgentRunEvent.conversation_id == conv_id,
                AgentRunEvent.run_id == child.id,
            )
        )).scalar_one_or_none()
        seq = max(0, int(max_seq or 0)) + 1
        payload = {
            "finish_reason": "parent_run_terminal",
            "terminal_reason": "parent_run_terminal_reconciliation",
            "cancellation_source": "root_run_terminal",
            "parent_terminal_status": root_terminal_status,
            "authoritative": True,
        }
        s.add(AgentRunEvent(
            id=uuid.uuid4().hex,
            run_id=child.id,
            conversation_id=conv_id,
            user_id=user_id,
            parent_run_id=child.parent_run_id,
            seq=seq,
            event_type="run.cancelled",
            payload=json.dumps(payload, ensure_ascii=False),
        ))
        child.status = "cancelled"
        child.finish_reason = "parent_run_terminal"
        child.error = None
        child.ended_at = now
        for task in task_items.get(child.id, []):
            task.status = "cancelled"
            task.error = None
            task.summary = "parent_run_terminal_reconciliation"
            task.ended_at = now
            task.updated_at = now
    return len(descendants)


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
) -> tuple[str | None, str | None, str]:
    async with async_session() as s:
        assistant_message = None
        event_user_id = None
        try:
            conv = (await s.execute(
                select(Conversation).where(
                    Conversation.id == conv_id
                )
            )).scalar_one_or_none()
            if conv is None:
                raise _ConversationProjectionBarrierError(
                    "Conversation was deleted before terminal projection."
                )
            require_session_workspace_active(str(conv.user_id), conv_id)
            terminal_status, _ = _agent_event_terminal_status(
                agent_events, run_id=run_id
            )
            root_terminal_type, root_terminal_payload = (
                _authoritative_root_terminal_payload(
                    agent_events,
                    run_id=run_id,
                )
            )
            pending_control_writes = root_terminal_payload.get(
                "pending_control_writes", []
            )
            if pending_control_writes is None:
                pending_control_writes = []
            if terminal_status == "succeeded":
                from scheduler import stage_schedule_control_writes

                await stage_schedule_control_writes(
                    s,
                    user_id=str(conv.user_id),
                    conversation_id=conv_id,
                    root_run_id=run_id,
                    model_id=resolved_model_id or model_id,
                    writes=pending_control_writes,
                    allowed_tools=frozenset(serialize_json_list(
                        conv.enabled_tools,
                        DEFAULT_NATIVE_TOOLS,
                    )),
                )
            elif pending_control_writes:
                # Failed/cancelled Turns never commit unattended effects.
                logger.info(
                    "Discarded %s pending control write(s) for terminal %s run=%s",
                    len(pending_control_writes)
                    if isinstance(pending_control_writes, list) else 0,
                    terminal_status,
                    run_id,
                )
            reconciled_usage = _reconciled_root_run_usage(
                usage,
                agent_events,
                run_id=run_id,
            )
            input_tokens = reconciled_usage["input_tokens"]
            output_tokens = reconciled_usage["output_tokens"]
            total_tokens = reconciled_usage["total_tokens"]
            projection_run = await s.get(AgentRun, run_id)
            message_source = (
                str(projection_run.source)
                if projection_run is not None
                and str(projection_run.source or "") in {"chat", "cron"}
                else "chat"
            )
            # Every accepted chat root owns exactly one assistant turn at the
            # durable projection boundary. A valid tool/artifact-only success
            # may have no visible model text; omitting the row would leave the
            # preceding user message last and falsely block every later turn
            # as "unprojected".
            assistant_message = Message(
                conversation_id=conv_id,
                role="assistant",
                content=content or "",
                reasoning=reasoning or None,
                tool_progress=tool_progress or None,
                model_id=resolved_model_id or model_id,
                run_id=run_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                source=message_source,
                created_at=await _next_message_created_at(s, conv_id),
            )
            s.add(assistant_message)
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
            reconciled_descendants = await _reconcile_nonterminal_descendants(
                s,
                conv_id=conv_id,
                user_id=str(event_user_id) if event_user_id else None,
                root_run_id=run_id,
                root_terminal_status=terminal_status,
            )
            if reconciled_descendants:
                logger.warning(
                    "Reconciled %s nonterminal descendant run(s) after root "
                    "terminal conv=%s root_run=%s status=%s",
                    reconciled_descendants,
                    conv_id,
                    run_id,
                    terminal_status,
                )
            run = projection_run
            if run:
                run.root_run_id = run.root_run_id or run.id
                run.agent_kind = run.agent_kind or "primary"
                run.agent_name = run.agent_name or "primary"
                run.depth = run.depth or 0
                run.workspace_scope = run.workspace_scope or "shared_session"
                run.resolved_model_id = resolved_model_id or model_id
                if terminal_status == "cancelled":
                    run.status = "cancelled"
                    run.finish_reason = str(
                        root_terminal_payload.get("finish_reason")
                        or root_terminal_payload.get("terminal_reason")
                        or "task_cancelled"
                    )[:256]
                    run.error = None
                else:
                    run.status = "failed" if error_message else "succeeded"
                    run.finish_reason = str(
                        root_terminal_payload.get("finish_reason")
                        or root_terminal_payload.get("terminal_reason")
                        or finish_reason
                    )[:256]
                    run.error = _bounded_agent_run_error(
                        root_terminal_payload.get("error")
                        if root_terminal_type == "run.failed"
                        else error_message
                    )
                run.tool_events = json.dumps(
                    tool_progress.splitlines() if tool_progress else [],
                    ensure_ascii=False,
                )
                run.input_tokens = input_tokens
                run.output_tokens = output_tokens
                run.total_tokens = total_tokens
                run.ended_at = datetime.utcnow()
            require_session_workspace_active(str(conv.user_id), conv_id)
            await s.commit()
            return (
                assistant_message.id if assistant_message is not None else None,
                str(event_user_id) if event_user_id else None,
                message_source,
            )
        except BaseException:
            await s.rollback()
            raise


def _terminal_projection_failure_code(exc: BaseException) -> str:
    """Return a bounded controller-owned diagnostic, never raw exception text."""

    if isinstance(exc, ValueError) and len(exc.args) == 1:
        candidate = str(exc.args[0])
        if re.fullmatch(
            r"(?:schedule_control|session_workspace|artifact)_[a-z0-9_]{1,96}",
            candidate,
        ):
            return candidate
    return "terminal_projection_failed"


async def _persist_terminal_projection_failure_once(
    *,
    conv_id: str,
    model_id: str,
    content: str,
    reasoning: str,
    tool_progress: str,
    run_id: str,
    resolved_model_id: str,
    usage: dict,
    failure_code: str,
) -> tuple[str | None, str | None, str]:
    """Fail closed when the success projection transaction cannot commit.

    The native terminal event proves only that execution ended.  It does not
    prove that controller-owned effects, the assistant row, and Session state
    committed.  This independent transaction makes that distinction durable
    and releases the Session without manufacturing a second run terminal.
    """

    async with async_session() as s:
        conv = await s.get(Conversation, conv_id)
        run = await s.get(AgentRun, run_id)
        if conv is None or run is None:
            raise _ConversationProjectionBarrierError(
                "Terminal projection recovery lost its Session identity."
            )
        event_user_id = str(conv.user_id)
        message_source = (
            str(run.source)
            if str(run.source or "") in {"chat", "cron"}
            else "chat"
        )
        assistant_message_id: str | None = None
        latest = (await s.execute(
            select(Message).where(
                Message.conversation_id == conv_id
            ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1)
        )).scalar_one_or_none()
        if latest is not None and latest.role == "user":
            notice = _chat_stream_failure_notice(
                (
                    "The task reached the terminal commit boundary, but its "
                    f"controller-owned effects could not be committed ({failure_code})."
                ),
                execution_failed=True,
                has_partial_content=bool(content),
            )
            failure_content = content
            if not any(
                marker in failure_content
                for marker in _INCOMPLETE_RESPONSE_MARKERS
            ):
                failure_content += notice
            reconciled = _normalized_usage(usage)
            assistant = Message(
                conversation_id=conv_id,
                role="assistant",
                content=failure_content,
                reasoning=reasoning or None,
                tool_progress=tool_progress or None,
                model_id=resolved_model_id or model_id,
                run_id=run_id,
                input_tokens=reconciled["input_tokens"],
                output_tokens=reconciled["output_tokens"],
                total_tokens=reconciled["total_tokens"],
                source=message_source,
                created_at=await _next_message_created_at(s, conv_id),
            )
            s.add(assistant)
            await s.flush()
            assistant_message_id = assistant.id
            conv.input_tokens += reconciled["input_tokens"]
            conv.output_tokens += reconciled["output_tokens"]
            conv.total_tokens += reconciled["total_tokens"]

        now = datetime.utcnow()
        run.status = "failed"
        run.finish_reason = "terminal_projection_failed"
        run.error = (
            "Controller-owned terminal projection failed closed: "
            + failure_code
        )
        run.ended_at = now
        reconciled = _normalized_usage(usage)
        _merge_monotonic_run_usage(run, reconciled)
        active_tasks = (await s.execute(
            select(TaskItem).where(
                TaskItem.conversation_id == conv_id,
                TaskItem.root_run_id == run_id,
                TaskItem.status.in_((
                    "pending", "planned", "queued", "running", "committing",
                )),
            )
        )).scalars().all()
        for task in active_tasks:
            task.status = "failed"
            task.summary = "terminal_projection_failed"
            task.error = run.error
            task.ended_at = now
            task.updated_at = now

        diagnostic_exists = (await s.execute(
            select(AgentRunEvent.id).where(
                AgentRunEvent.conversation_id == conv_id,
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.event_type == "run.projection_failed",
            ).limit(1)
        )).scalar_one_or_none()
        if diagnostic_exists is None:
            next_seq = int((await s.execute(
                select(func.max(AgentRunEvent.seq)).where(
                    AgentRunEvent.conversation_id == conv_id,
                    AgentRunEvent.run_id == run_id,
                )
            )).scalar_one() or 0) + 1
            s.add(AgentRunEvent(
                run_id=run_id,
                conversation_id=conv_id,
                user_id=event_user_id,
                parent_run_id=None,
                seq=next_seq,
                event_type="run.projection_failed",
                payload=json.dumps({
                    "stage": "terminal_projection",
                    "code": failure_code,
                    "authoritative": False,
                }, ensure_ascii=False, separators=(",", ":")),
                event_time=now,
            ))

        engine_state = (await s.execute(
            select(AgentEngineSession).where(
                AgentEngineSession.conversation_id == conv_id,
                AgentEngineSession.active_run_id == run_id,
            )
        )).scalar_one_or_none()
        if engine_state is not None:
            engine_state.status = "failed"
            engine_state.active_run_id = None
            engine_state.error = run.error
        require_session_workspace_active(event_user_id, conv_id)
        await s.commit()
        return assistant_message_id, event_user_id, message_source


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

    # Every live projection for this root run was registered before the stream
    # entered its finally block.  Await them first so the complete ordered
    # event list below is a true terminal backfill rather than another racing
    # SQLite writer.
    await _drain_agent_event_immediate_persists(conv_id, run_id)
    lock = _agent_event_persist_lock(conv_id)
    assistant_message_id: str | None = None
    event_user_id: str | None = None
    projected_message_source = "chat"
    try:
        async with lock:
            persisted_terminal, persisted_max_seq = (
                await _persisted_authoritative_root_terminal(
                    conv_id=conv_id,
                    run_id=run_id,
                )
            )
            complete_events, finish_reason, error_message = (
                _seal_missing_root_terminal_for_projection(
                    agent_events or [],
                    run_id=run_id,
                    usage=usage,
                    finish_reason=finish_reason,
                    error_message=error_message,
                    persisted_terminal=persisted_terminal,
                    persisted_max_seq=persisted_max_seq,
                )
            )
            terminal_status, _terminal_error = (
                _agent_event_terminal_status(
                    complete_events,
                    run_id=run_id,
                )
            )
            error_message, _execution_failed = (
                _reconcile_root_stream_error(
                    complete_events,
                    run_id=run_id,
                    stream_error=error_message,
                )
            )
            if terminal_status == "cancelled":
                finish_reason = "task_cancelled"
            if (
                error_message
                and not any(
                    marker in content
                    for marker in _INCOMPLETE_RESPONSE_MARKERS
                )
            ):
                content += _chat_stream_failure_notice(
                    error_message,
                    execution_failed=_execution_failed,
                    has_partial_content=bool(content),
                )
            assistant_message_id, event_user_id, projected_message_source = (
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
    except Exception as exc:
        logger.exception(
            "Terminal stream projection failed after retries conv=%s run=%s",
            conv_id,
            run_id,
        )
        failure_code = _terminal_projection_failure_code(exc)
        try:
            (
                assistant_message_id,
                event_user_id,
                projected_message_source,
            ) = await _run_sqlite_persist_with_retry(
                lambda: _persist_terminal_projection_failure_once(
                    conv_id=conv_id,
                    model_id=model_id,
                    content=content,
                    reasoning=reasoning,
                    tool_progress=tool_progress,
                    run_id=run_id,
                    resolved_model_id=resolved_model_id,
                    usage=usage,
                    failure_code=failure_code,
                ),
                description=(
                    "failed terminal projection convergence "
                    f"conv={conv_id} run={run_id}"
                ),
            )
        except Exception:
            logger.exception(
                "Terminal projection failure did not converge conv=%s run=%s",
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
                    "source": projected_message_source,
                },
                conv_id,
            )
        except Exception:
            logger.exception(
                "Post-persist message event failed conv=%s run=%s",
                conv_id,
                run_id,
            )
    async def generate_title_best_effort() -> None:
        async with async_session() as s:
            cnt = (await s.execute(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conv_id
                )
            )).scalar() or 0
            logger.info(
                "title check: conv=%s cnt=%s has_first=%s",
                conv_id,
                cnt,
                bool(first_user_content),
            )
            if cnt <= 2 and first_user_content:
                await _generate_title(
                    conv_id,
                    first_user_content,
                    content,
                    s,
                )

    if first_user_content:
        _track_best_effort_task(
            generate_title_best_effort(),
            description=f"title generation conv={conv_id}",
        )
    return True


async def _finalize_native_engine_session(run_id: str, conv_id: str) -> None:
    async def finalize_once() -> None:
        async with async_session() as s:
            persisted_run = await s.get(AgentRun, run_id)
            if (
                persisted_run is None
                or persisted_run.engine_id != ENGINE_ID_CLAUDE_CODE
            ):
                return
            native_state = (await s.execute(
                select(AgentEngineSession).where(
                    AgentEngineSession.conversation_id == conv_id,
                    AgentEngineSession.engine_id == ENGINE_ID_CLAUDE_CODE,
                )
            )).scalar_one_or_none()
            if native_state is None or native_state.active_run_id != run_id:
                return
            terminal_status = str(persisted_run.status or "failed")
            raw_terminal_rows = list((await s.execute(
                select(AgentEngineRawEvent).where(
                    AgentEngineRawEvent.run_id == run_id,
                    AgentEngineRawEvent.native_event_type
                    == "chatds.supervisor.terminal",
                ).order_by(AgentEngineRawEvent.seq)
            )).scalars().all())
            raw_terminal_status = None
            raw_result_succeeded = False
            raw_checkpoint_observed = False
            if len(raw_terminal_rows) == 1:
                try:
                    raw_envelope = json.loads(raw_terminal_rows[0].payload)
                    raw_native = (
                        raw_envelope.get("event")
                        if isinstance(raw_envelope, dict)
                        else None
                    )
                    if (
                        isinstance(raw_native, dict)
                        and raw_native.get("type")
                        == "chatds.supervisor.terminal"
                    ):
                        raw_terminal_status = str(
                            raw_native.get("status") or ""
                        )
                        raw_result_succeeded = (
                            raw_native.get("result_succeeded") is True
                        )
                        raw_checkpoint_observed = (
                            raw_native.get("checkpoint_observed") is True
                        )
                except (TypeError, ValueError, json.JSONDecodeError):
                    raw_terminal_status = None
            if (
                terminal_status == "succeeded"
                and raw_terminal_status != "succeeded"
            ):
                # A normalized projection is not sufficient authority to
                # publish a native checkpoint.  The lossless Supervisor
                # terminal must have crossed the raw-event durability barrier
                # in the same Run first.
                persisted_run.status = "failed"
                persisted_run.finish_reason = "native_audit_incomplete"
                persisted_run.error = (
                    "Claude Code success was not backed by one durable "
                    "Supervisor terminal event."
                )
                native_state.status = "failed"
                native_state.active_run_id = None
                native_state.error = persisted_run.error
                await s.commit()
                raise RuntimeError("claude_native_terminal_audit_incomplete")
            native_state.status = (
                "idle" if terminal_status == "succeeded" else terminal_status
            )
            native_state.active_run_id = None
            native_state.last_event_seq = int((await s.execute(
                select(func.max(AgentEngineRawEvent.seq)).where(
                    AgentEngineRawEvent.run_id == run_id
                )
            )).scalar_one() or 0)
            native_state.error = persisted_run.error
            publish_transcript_checkpoint = bool(
                persisted_run.native_session_id
                and raw_checkpoint_observed
                and raw_result_succeeded
                and raw_terminal_status in {"succeeded", "failed"}
                and terminal_status in {"succeeded", "failed"}
                and persisted_run.finish_reason
                not in {
                    "terminal_projection_failed",
                    "terminal_projection_interrupted",
                }
            )
            if publish_transcript_checkpoint:
                if not persisted_run.native_session_id:
                    raise RuntimeError(
                        "A complete Claude Code Turn has no candidate checkpoint identity"
                    )
                native_state.native_session_id = persisted_run.native_session_id
                native_state.generation += 1
            await s.commit()

    await _run_sqlite_persist_with_retry(
        finalize_once,
        description=f"native engine session finalization conv={conv_id} run={run_id}",
    )


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
        persisted = await _persist_after_stream(
            conv_id, model_id, content, reasoning, tool_progress, first_user_content,
            run_id, resolved_model_id, usage, finish_reason,
            error_message, agent_events,
        )
        if not persisted:
            raise RuntimeError(
                f"Terminal stream projection was not durable conv={conv_id} run={run_id}"
            )
        await _finalize_native_engine_session(run_id, conv_id)
        try:
            async with async_session() as s:
                persisted_run = await s.get(AgentRun, run_id)
            persisted_status = (
                str(persisted_run.status or "")
                if persisted_run is not None
                else "failed"
            )
            persisted_error = (
                persisted_run.error
                if persisted_run is not None
                else "Terminal projection row was not found."
            )
            persisted_usage = _normalized_usage({
                "input_tokens": getattr(
                    persisted_run, "input_tokens", 0
                ),
                "output_tokens": getattr(
                    persisted_run, "output_tokens", 0
                ),
                "total_tokens": getattr(
                    persisted_run, "total_tokens", 0
                ),
            })
            await emit_event(
                user_id,
                (
                    "run.cancelled"
                    if persisted_status == "cancelled"
                    else "run.failed"
                    if persisted_status == "failed"
                    else "run.completed"
                ),
                {
                    "conversation_id": conv_id,
                    "run_id": run_id,
                    "model_id": resolved_model_id,
                    "usage": persisted_usage,
                    "error": persisted_error,
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
    # External OpenAI-compatible routes.  The provider also exposes an
    # Anthropic Messages facade, but its OpenAI stream preserves
    # ``reasoning_content`` and native tool-call deltas, so that is the
    # authoritative Harness transport.
    "shaiengine_glm_5_2": {
        "api_model": "glm-5.2",
        "base_url": settings.shaiengine_base_url,
        "api_key": settings.shaiengine_api_key,
        "is_multimodal": False,
        "max_tokens": 86400,
        "display_name": "GLM-5.2 (Shaiengine · 默认测试)",
        "is_default": True,
        "agentic_auxiliary_only": False,
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
        "thinking_request_format": "thinking_object",
        "thinking_send_enabled_explicitly": True,
        "capabilities": ["text", "tools", "reasoning"],
        "provider": "shaiengine",
        "claude_provider_profile": "shaiengine",
        "deepseek_harness_provider_profile": "shaiengine",
        "protocol": "openai",
        "context_length": 1000000,
        "discover_runtime_metadata": True,
    },
    "shaiengine_deepseek_v4_pro": {
        "api_model": "deepseek-v4-pro",
        "base_url": settings.shaiengine_base_url,
        "api_key": settings.shaiengine_api_key,
        "is_multimodal": False,
        "max_tokens": 86400,
        "display_name": "DeepSeek V4 Pro (Shaiengine)",
        "is_default": False,
        "agentic_auxiliary_only": False,
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
        "thinking_request_format": "thinking_object",
        "thinking_send_enabled_explicitly": True,
        "capabilities": ["text", "tools", "reasoning"],
        "provider": "shaiengine",
        "claude_provider_profile": "shaiengine",
        "deepseek_harness_provider_profile": "shaiengine",
        "protocol": "openai",
        "context_length": 200000,
        "discover_runtime_metadata": True,
    },
    "shaiengine_kimi_k3": {
        "api_model": "kimi-k3",
        "base_url": settings.shaiengine_base_url,
        "api_key": settings.shaiengine_api_key,
        "is_multimodal": True,
        "max_tokens": 86400,
        "display_name": "Kimi K3 (Shaiengine · 多模态)",
        "is_default": False,
        "agentic_auxiliary_only": False,
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
        "thinking_request_format": "thinking_object",
        "thinking_send_enabled_explicitly": True,
        "capabilities": ["text", "vision", "tools", "reasoning"],
        "provider": "shaiengine",
        "claude_provider_profile": "shaiengine",
        "deepseek_harness_provider_profile": "shaiengine",
        "protocol": "openai",
        # Shaiengine's /v1/models entry advertises the route but omits
        # capacity fields.  Kimi's first-party K3 specification is therefore
        # the deployment-owned authority for this 1M-token bound.
        "context_length": 1000000,
        "discover_runtime_metadata": True,
    },
    # 10.10.132.2 local GLM-5.2 (918528 ctx) — retained for existing sessions
    "deepseek_v4_pro": {
        "api_model": "AgentModel",
        "base_url": settings.deepseek_pro_base_url,
        "api_key": settings.deepseek_pro_api_key,
        "is_multimodal": False,
        "max_tokens": 262144,
        "display_name": "GLM-5.2 (本地 AgentModel)",
        "is_default": False,
        "agentic_auxiliary_only": False,
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
        "thinking_request_format": "chat_template_kwargs",
        "capabilities": ["text", "tools", "reasoning"],
        "provider": "builtin",
        "claude_provider_profile": "local_agentmodel",
        "deepseek_harness_provider_profile": "local_agentmodel",
        "protocol": "openai",
        "context_length": 918528,
        "discover_runtime_metadata": True,
    },
    # 10.10.132.126 local DeepSeek-V4-Flash.  It shares the wire-level
    # ``AgentModel`` name with the GLM deployment, so the user-facing route id
    # and Claude provider profile must stay distinct.
    "local_deepseek_v4_flash": {
        "api_model": "AgentModel",
        "base_url": settings.local_deepseek_v4_flash_base_url,
        "api_key": settings.local_deepseek_v4_flash_api_key,
        "is_multimodal": False,
        "max_tokens": 262144,
        "display_name": "DeepSeek V4 Flash (本地 AgentModel)",
        "is_default": False,
        "agentic_auxiliary_only": False,
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": True,
        "thinking_request_format": "chat_template_kwargs",
        "capabilities": ["text", "tools", "reasoning"],
        "provider": "builtin",
        "claude_provider_profile": "local_deepseek_v4_flash",
        "deepseek_harness_provider_profile": "local_deepseek_v4_flash",
        "protocol": "openai",
        "context_length": 1048576,
        "discover_runtime_metadata": True,
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
        "thinking_request_format": "chat_template_kwargs",
        "capabilities": ["text", "vision", "tools"],
        "provider": "builtin",
        "claude_provider_profile": "local_qwen",
        "deepseek_harness_provider_profile": "local_qwen",
        "protocol": "openai",
        "context_length": 262144,
        "discover_runtime_metadata": True,
    },
}

# Backward-compatible alias for existing conversations
BUILTIN["AgentModel"] = BUILTIN["deepseek_v4_pro"]

_default_builtin_ids = [
    model_id
    for model_id, config in BUILTIN.items()
    if model_id != "AgentModel" and config.get("is_default") is True
]
if _default_builtin_ids != [DEFAULT_AGENT_MODEL_ID]:
    raise RuntimeError(
        "Backend and Harness default model identities diverged: "
        + repr(_default_builtin_ids)
    )

DEFAULT_CUSTOM_MAX_TOKENS = 32768


def claude_code_model_compatible(provider_config: dict) -> bool:
    # Provider credentials are deployment-owned by the trusted Runner and are
    # never forwarded from a user-defined model row.  Consequently protocol
    # shape alone is insufficient: the model must bind one explicitly
    # configured Runner profile.
    profile = provider_config.get("claude_provider_profile")
    return (
        isinstance(profile, str)
        and bool(profile)
        and profile in settings.claude_code_provider_profiles
    )


def deepseek_harness_model_compatible(provider_config: dict) -> bool:
    """Require an exact deployment-owned OpenAI-compatible runner binding."""

    profile = provider_config.get("deepseek_harness_provider_profile")
    return (
        provider_config.get("protocol") == "openai"
        and isinstance(profile, str)
        and bool(profile)
        and profile in settings.deepseek_harness_provider_profiles
    )


_DEEPSEEK_HARNESS_TOOL_CAPABILITIES = frozenset({
    "web_search", "web_extract", "market_quote",
    "execute_code", "run_skill_python", "run_skill_script",
    "run_declared_command", "skill_http_get", "skill_http_post_json",
    "read_file", "write_file", "patch_file", "merge_files", "search_files",
    "todo", "skills_list", "skill_view", "skill_copy_resource",
    "delegate_task", "get_goal", "create_goal", "update_goal",
})


def _effective_engine_tools(engine_id: str, requested: list[str]) -> list[str]:
    """Compile canonical ChatDS grants onto one native engine surface."""

    if engine_id != ENGINE_ID_DEEPSEEK_HARNESS:
        return list(requested)
    return [
        name for name in requested
        if name in _DEEPSEEK_HARNESS_TOOL_CAPABILITIES
    ]


def _require_turn_input_capabilities(
    *,
    engine_id: str,
    image_urls: list[str] | None,
    provider_config: dict,
) -> None:
    """Reject a binary input before creating a run with an incapable model."""

    if (
        engine_id in {ENGINE_ID_CLAUDE_CODE, ENGINE_ID_DEEPSEEK_HARNESS}
        and image_urls
        and (
            not bool(provider_config.get("is_multimodal"))
            or engine_id == ENGINE_ID_DEEPSEEK_HARNESS
        )
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "The selected model does not accept image input. "
                "Choose a vision-capable model for this Turn."
            ),
        )


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
            "claude_provider_profile": cfg.get("claude_provider_profile"),
            "protocol": cfg.get("protocol", "openai"),
            "is_multimodal": cfg["is_multimodal"],
            "context_length": cfg.get("context_length", 262144),
            # The context window and the per-response completion ceiling are
            # independent provider capabilities.  The Harness needs both: a
            # large context does not imply that one reduction/report response
            # may consume the whole remaining window.  Keep the wire name
            # aligned with Harness provider metadata rather than exposing the
            # Backend's historical request-field name (``max_tokens``).
            "max_output_tokens": cfg.get("max_tokens"),
            "discover_runtime_metadata": bool(
                cfg.get("discover_runtime_metadata", False)
            ),
            "agentic_auxiliary_only": cfg.get(
                "agentic_auxiliary_only", False
            ),
            "supports_thinking_toggle": cfg.get(
                "supports_thinking_toggle", False
            ),
            "thinking_enabled_by_default": cfg.get(
                "thinking_enabled_by_default", True
            ),
            "thinking_request_format": cfg.get(
                "thinking_request_format", ""
            ),
            "thinking_send_enabled_explicitly": bool(
                cfg.get("thinking_send_enabled_explicitly", False)
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
        "discover_runtime_metadata": True,
        "extra_headers": extra_headers,
    }


@router.get("/models")
async def get_models(cur_user=Depends(get_current_user), db=Depends(get_db)):
    # Backend provider profiles are the execution authority. Harness discovery
    # may enrich availability/name data, but it must never make configured
    # models disappear merely because another engine reports a partial list.
    discovered: dict[str, dict] = {}
    if settings.legacy_engine_new_runs_enabled:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{settings.harness_url}/v1/models")
                if r.status_code == 200:
                    data = r.json()
                    for m in data.get("data", []):
                        if isinstance(m, dict) and m.get("id"):
                            discovered[canonical_agent_model_id(m["id"])] = m
        except Exception:
            pass
    models = [
        {
            "id": mid,
            "name": cfg["display_name"],
            "provider": cfg.get("provider", "builtin"),
            "is_multimodal": cfg["is_multimodal"],
            "is_default": cfg.get("is_default", False),
            "capabilities": cfg.get("capabilities", ["text"]),
            "legacy_discovered": mid in discovered,
            "compatible_engines": [
                *(
                    ["legacy"]
                    if settings.legacy_engine_new_runs_enabled
                    else []
                ),
                *(["claude_code"] if claude_code_model_compatible(cfg) else []),
                *(
                    ["deepseek_harness"]
                    if settings.deepseek_harness_engine_enabled
                    and deepseek_harness_model_compatible(cfg)
                    else []
                ),
            ],
        }
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
            "compatible_engines": (
                ["legacy"]
                if settings.legacy_engine_new_runs_enabled
                else []
            ),
        })
    return {"models": models}


@router.get("/engines")
async def get_agent_engines(cur_user=Depends(get_current_user)):
    """Expose configured execution engines without leaking runtime details."""

    del cur_user
    from agent_engines.registry import build_agent_engine_registry

    descriptors = await build_agent_engine_registry().descriptors()
    return {
        "engines": [
            {
                "id": item.id,
                "name": item.display_name,
                "is_default": item.id == settings.default_agent_engine_id,
                "available": (
                    item.available
                    and not (
                        item.id == ENGINE_ID_LEGACY
                        and not settings.legacy_engine_new_runs_enabled
                    )
                ),
                "version": item.version,
                "capabilities": list(item.capabilities),
                "unavailable_reason": (
                    "Legacy Harness is retained for history only"
                    if (
                        item.id == ENGINE_ID_LEGACY
                        and not settings.legacy_engine_new_runs_enabled
                    )
                    else item.unavailable_reason
                ),
            }
            for item in descriptors
        ]
    }


async def _detect_model(req: ChatRequest, user_id: str) -> str:
    """Auto-detect model based on message content.

    Routing rules:
    1. Image → the configured primary model (agent discovers session skills/MCP tools and decides:
       call MCP tool for specialized processing, or use vision_analyze tool
       which internally calls qwen3_5 for visual recognition)
    2. Plain text → the configured primary model
    """
    if req.model_id:
        return req.model_id
    return DEFAULT_AGENT_MODEL_ID


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


async def _chat_stream(
    req: ChatRequest,
    cur_user: User,
    db: AsyncSession,
    *,
    ingress_request_id: str | None = None,
    source: str = "chat",
    enabled_tools_override: tuple[str, ...] | None = None,
):
    if source not in {"chat", "cron"}:
        raise ValueError("Unsupported internal chat source")
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
            if req.engine_id is not None and req.engine_id != conv.engine_id:
                raise HTTPException(
                    409,
                    "Agent Engine is fixed for this Conversation; fork it to change engines",
                )
            if (
                conv.engine_id == ENGINE_ID_LEGACY
                and not settings.legacy_engine_new_runs_enabled
            ):
                raise HTTPException(
                    409,
                    "Legacy Harness execution is disabled; fork this Conversation to Claude Code",
                )
            if conv.engine_id != ENGINE_ID_LEGACY:
                from agent_engines.registry import build_agent_engine_registry
                try:
                    build_agent_engine_registry().get(conv.engine_id)
                except LookupError as exc:
                    raise HTTPException(
                        409,
                        "The Conversation's Agent Engine is not configured",
                    ) from exc
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
            await ensure_workspace_async(cur_user.id, conv_id)
        else:
            model_id = canonical_agent_model_id(
                await _detect_model(req, str(cur_user.id))
            )
            requested_engine = req.engine_id or settings.default_agent_engine_id
            if (
                requested_engine == ENGINE_ID_LEGACY
                and not settings.legacy_engine_new_runs_enabled
            ):
                raise HTTPException(
                    400,
                    "Legacy Harness execution is disabled for new Conversations",
                )
            from agent_engines.registry import build_agent_engine_registry
            try:
                build_agent_engine_registry().get(requested_engine)
            except LookupError as exc:
                raise HTTPException(400, "Requested Agent Engine is not configured") from exc
            conv = Conversation(
                user_id=cur_user.id,
                model_id=model_id,
                engine_id=requested_engine,
            )
            db.add(conv)
            await db.commit()
            await db.refresh(conv)
            conv_id = conv.id
            turn_lease = await _acquire_conversation_turn(conv_id)
            await ensure_workspace_async(cur_user.id, conv_id)
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
            ingress_request_id=ingress_request_id,
            source=source,
            enabled_tools_override=enabled_tools_override,
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
    ingress_request_id: str | None = None,
    source: str = "chat",
    enabled_tools_override: tuple[str, ...] | None = None,
):
    await _assert_no_unprojected_primary_turn(db, conv_id)

    provider_config = await resolve_model_config(model_id, cur_user, db)
    if (
        conv.engine_id == ENGINE_ID_CLAUDE_CODE
        and not claude_code_model_compatible(provider_config)
    ):
        raise HTTPException(
            400,
            "The selected model is not allowed by a configured Claude Code provider profile",
        )
    if (
        conv.engine_id == ENGINE_ID_DEEPSEEK_HARNESS
        and not deepseek_harness_model_compatible(provider_config)
    ):
        raise HTTPException(
            400,
            "The selected model is not allowed by a configured DeepSeek Harness provider profile",
        )
    _require_turn_input_capabilities(
        engine_id=conv.engine_id,
        image_urls=req.image_urls,
        provider_config=provider_config,
    )
    max_tokens = (
        BUILTIN.get(model_id, {}).get("max_tokens")
        if model_id in BUILTIN else DEFAULT_CUSTOM_MAX_TOKENS
    ) or DEFAULT_CUSTOM_MAX_TOKENS
    requested_tools = (
        list(enabled_tools_override)
        if enabled_tools_override is not None
        else serialize_json_list(conv.enabled_tools, DEFAULT_NATIVE_TOOLS)
    )
    enabled_tools = _effective_engine_tools(conv.engine_id, requested_tools)
    enabled_user_skills = serialize_json_list(conv.enabled_user_skills, [])
    skill_registry_query = select(SkillPackage).where(
        SkillPackage.user_id == cur_user.id,
        (
            (SkillPackage.session_id == conv_id)
            | (
                SkillPackage.session_id.is_(None)
                & SkillPackage.name.in_(enabled_user_skills)
            )
        ),
    ).order_by(SkillPackage.created_at.desc()).limit(513)
    skill_registry_rows = (
        await db.execute(skill_registry_query)
    ).scalars().all()
    session_skill_registry = skill_bundle_registry_rows(
        skill_registry_rows
    )
    if session_skill_registry:
        # Import lazily to share the router's configured/test-overridden root
        # without creating an import cycle at module initialization.
        from routers.skill_router import SKILLS_DATA_DIR
        session_skill_registry = await asyncio.to_thread(
            content_address_skill_bundle_registry_rows,
            session_skill_registry,
            skill_registry_rows,
            SKILLS_DATA_DIR,
        )
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

    native_skill_view = None
    engine_session: AgentEngineSession | None = None
    resume_from_native_session_id: str | None = None
    candidate_native_session_id: str | None = None
    if conv.engine_id in {ENGINE_ID_CLAUDE_CODE, ENGINE_ID_DEEPSEEK_HARNESS}:
        from agent_engines.skill_view import (
            authorized_skill_sources,
            materialize_claude_skill_view,
        )
        from routers import skill_router as skill_api

        # Freeze both Skill scopes while copying the DB-authorized closure.
        # The resulting plugin tree is content-addressed and read-only, so the
        # long model Turn does not hold either install lock.
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                skill_api._skill_install_lock(cur_user.id, None)
            )
            await stack.enter_async_context(
                skill_api._skill_install_lock(cur_user.id, conv_id)
            )
            fresh_rows = (await db.execute(skill_registry_query)).scalars().all()
            sources = authorized_skill_sources(
                user_id=str(cur_user.id),
                session_id=conv_id,
                registry_rows=fresh_rows,
                skills_data_dir=skill_api.SKILLS_DATA_DIR,
            )
            native_skill_view = await asyncio.to_thread(
                materialize_claude_skill_view,
                session_root=workspace_store.session_root(cur_user.id, conv_id),
                sources=sources,
                enabled_tools=enabled_tools,
                web_search_url=settings.claude_web_search_url,
                market_data_url=settings.claude_market_data_url,
            )

    if conv.engine_id == ENGINE_ID_CLAUDE_CODE:
        engine_session = (await db.execute(
            select(AgentEngineSession).where(
                AgentEngineSession.user_id == cur_user.id,
                AgentEngineSession.conversation_id == conv_id,
                AgentEngineSession.engine_id == ENGINE_ID_CLAUDE_CODE,
            )
        )).scalar_one_or_none()
        if engine_session is None:
            engine_session = AgentEngineSession(
                user_id=cur_user.id,
                conversation_id=conv_id,
                engine_id=ENGINE_ID_CLAUDE_CODE,
                native_session_id=None,
                status="idle",
                # SQLAlchemy column defaults are applied during INSERT.  The
                # first Turn inspects this value before the session is
                # flushed, so initialize the in-memory checkpoint state
                # explicitly instead of comparing None with an integer.
                generation=0,
            )
            db.add(engine_session)
        elif engine_session.status in {"queued", "running", "committing"}:
            raise HTTPException(409, "A Claude Code Turn is already active for this Conversation")
        if engine_session.generation > 0:
            if not engine_session.native_session_id:
                raise HTTPException(
                    409,
                    "Claude Code checkpoint metadata is incomplete for this Conversation",
                )
            resume_from_native_session_id = engine_session.native_session_id
        # Every Turn writes a fresh native checkpoint.  It is promoted into
        # AgentEngineSession only after the authoritative root terminal and
        # assistant projection are durable. A controller-rejected Turn may
        # still publish this candidate as a transcript-only boundary when the
        # raw native result and checkpoint are both complete; its failed
        # receipts remain authoritative for the business outcome.
        candidate_native_session_id = str(uuid.uuid4())

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
        source=source,
        created_at=user_created_at,
    )
    db.add(user_msg)
    run_id = uuid.uuid4().hex
    run = AgentRun(
        id=run_id,
        user_id=cur_user.id,
        conversation_id=conv_id,
        root_run_id=run_id,
        source=source,
        engine_id=conv.engine_id,
        native_session_id=(
            candidate_native_session_id
            if engine_session is not None
            else None
        ),
        requested_model_id=model_id,
        resolved_model_id=model_id,
        status="running",
        agent_kind="primary",
        agent_name="primary",
        depth=0,
        workspace_scope="shared_session",
        requested_tools=json.dumps(requested_tools, ensure_ascii=False),
        effective_tools=json.dumps(enabled_tools, ensure_ascii=False),
        started_at=user_created_at,
    )
    db.add(run)
    if engine_session is not None:
        engine_session.status = "running"
        engine_session.active_run_id = run_id
        engine_session.skill_view_sha256 = native_skill_view.sha256
        engine_session.last_model_id = model_id
        engine_session.error = None
    await db.commit()
    stream_observation = _StreamObservation(
        run_id=run.id,
        ingress_request_id=ingress_request_id,
    )

    async def generate():
        full_content = ""
        full_reasoning = ""
        full_tool_progress = ""
        error_message: Optional[str] = None
        stream_error_message: Optional[str] = None
        finish_reason = "stop"
        resolved_model_id = model_id
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        agent_events: list[dict] = []
        seen_agent_events: set[tuple[str, str, int]] = set()
        cancelled = False
        stream_aborted = False
        termination_event_appended = False
        upstream_failure_kind: str | None = None
        upstream_exception_class: str | None = None
        terminal_envelope_payload: dict | None = None
        raw_engine_event_batch: list[dict] = []
        seen_raw_engine_event_seqs: set[int] = set()
        activity_builder = TurnActivityBuilder(run.id, conv_id)
        activity_event_batch: list[dict] = []

        async def record_activity(
            event: dict | None,
            *,
            force: bool = False,
        ) -> dict | None:
            if event is None:
                return None
            activity_event_batch.append(event)
            if force or len(activity_event_batch) >= 32:
                pending = list(activity_event_batch)
                activity_event_batch.clear()
                await _persist_turn_activity_events(
                    user_id=str(cur_user.id),
                    conversation_id=conv_id,
                    root_run_id=run.id,
                    events=pending,
                )
            return event

        def encode_sse(payload: dict) -> str:
            chunk = f"data: {json.dumps(payload)}\n\n"
            return stream_observation.observe_produced_chunk(chunk)

        try:
            # Establish cleanup before the first yield: a client may disconnect
            # immediately after receiving routing metadata.
            yield encode_sse({
                "routed_model": model_id,
                "conversation_id": conv_id,
                "run_id": run.id,
            })

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
                stream_observation.upstream_state = "connecting"
                engine_messages = tuple(final)
                input_attachments: tuple[dict, ...] = ()
                if conv.engine_id == ENGINE_ID_CLAUDE_CODE:
                    if any(
                        isinstance(message.get("content"), list)
                        and any(
                            isinstance(part, dict)
                            and part.get("type") == "image_url"
                            for part in message["content"]
                        )
                        for message in final
                        if isinstance(message, dict)
                    ):
                        try:
                            projection = await (
                                workspace_store.run_session_workspace_mutation_async(
                                    cur_user.id,
                                    conv_id,
                                    lambda workspace: materialize_message_attachments(
                                        final,
                                        workspace=workspace,
                                    ),
                                )
                            )
                        except InputAttachmentError as exc:
                            raise AgentEngineError(
                                str(exc),
                                code=exc.code,
                                retryable=False,
                                exception_class=type(exc).__name__,
                            ) from exc
                        engine_messages = projection.messages
                        input_attachments = projection.attachments
                legacy_payload = {
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
                    "source": source,
                    "enabled_user_skills": enabled_user_skills,
                    "session_skill_registry": session_skill_registry,
                    "event_schema": "chatds.agent.v2",
                    "run_metadata": {
                        "run_id": run.id,
                        "root_run_id": run.id,
                        "agent_kind": "primary",
                        "agent_name": "primary",
                        "depth": 0,
                        "workspace_scope": "shared_session",
                    },
                }
                engine_request = AgentEngineRequest(
                    run_id=run.id,
                    root_run_id=run.id,
                    user_id=str(cur_user.id),
                    conversation_id=conv_id,
                    model_id=model_id,
                    api_model=str(provider_config.get("api_model") or model_id),
                    messages=engine_messages,
                    input_attachments=input_attachments,
                    max_output_tokens=max_tokens,
                    temperature=0.6,
                    provider_config=provider_config,
                    fallback_configs=tuple(fallback_configs),
                    tools=tuple(enabled_tools),
                    enabled_user_skills=tuple(enabled_user_skills),
                    session_skill_registry=tuple(session_skill_registry),
                    skill_view_path=(
                        str(native_skill_view.root)
                        if native_skill_view is not None
                        else None
                    ),
                    skill_view_sha256=(
                        native_skill_view.sha256
                        if native_skill_view is not None
                        else None
                    ),
                    native_session_id=(
                        candidate_native_session_id
                        if engine_session is not None
                        else None
                    ),
                    resume_from_native_session_id=resume_from_native_session_id,
                    source=source,
                    metadata={
                        "workspace_path": str(
                            workspace_store.workspace_dir(cur_user.id, conv_id)
                        ),
                        "user_turn_text": req.content,
                        "permission_preset": conv.permission_preset,
                    },
                )
                async with _open_agent_engine_stream(
                    engine_id=conv.engine_id,
                    request=engine_request,
                    legacy_payload=legacy_payload,
                ) as response:
                        stream_observation.upstream_state = "connected"
                        if response.status_code >= 400:
                            body = (await response.aread()).decode("utf-8", "ignore")[:300]
                            stream_error_message = f"Harness 返回 HTTP {response.status_code}:{body}"
                            upstream_failure_kind = "upstream_harness_http_error"
                            stream_observation.upstream_state = "http_error"
                        else:
                            async for line in response.aiter_lines():
                                stream_observation.observe_upstream_line(line)
                                if not line.startswith("data: "):
                                    continue
                                chunk = line[6:]
                                if chunk == "[DONE]":
                                    stream_observation.upstream_state = "done_received"
                                    break
                                try:
                                    data = json.loads(chunk)
                                    stream_observation.observe_upstream_data()
                                    raw_engine_event = data.get("engine_raw_event")
                                    if isinstance(raw_engine_event, dict):
                                        raw_seq = raw_engine_event.get("seq")
                                        if (
                                            isinstance(raw_seq, int)
                                            and raw_seq > 0
                                            and raw_seq not in seen_raw_engine_event_seqs
                                        ):
                                            seen_raw_engine_event_seqs.add(raw_seq)
                                            raw_engine_event_batch.append(raw_engine_event)
                                            native_payload = raw_engine_event.get("event")
                                            raw_is_terminal = bool(
                                                isinstance(native_payload, dict)
                                                and native_payload.get("type")
                                                == "chatds.supervisor.terminal"
                                            )
                                            if len(raw_engine_event_batch) >= 32 or raw_is_terminal:
                                                pending_raw = list(raw_engine_event_batch)
                                                raw_engine_event_batch.clear()
                                                await _persist_engine_raw_events(
                                                    user_id=str(cur_user.id),
                                                    conversation_id=conv_id,
                                                    run_id=run.id,
                                                    engine_id=conv.engine_id,
                                                    envelopes=pending_raw,
                                                )
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
                                            stream_observation.observe_agent_event(
                                                agent_event
                                            )
                                            agent_events.append(agent_event)
                                            event_type = str(agent_event.get("event_type") or "")
                                            if _should_persist_agent_event_immediately(event_type):
                                                _spawn_agent_event_immediate_persist(
                                                    conv_id=conv_id,
                                                    user_id=cur_user.id,
                                                    root_run_id=run.id,
                                                    requested_model_id=model_id,
                                                    resolved_model_id=resolved_model_id,
                                                    event=agent_event,
                                                )
                                            activity_event = await record_activity(
                                                activity_builder.agent(agent_event),
                                                force=True,
                                            )
                                            yield encode_sse({
                                                "agent_event": agent_event,
                                                "activity_event": activity_event,
                                                "conversation_id": conv_id,
                                            })
                                    approval = delta.get("approval")
                                    if isinstance(approval, dict):
                                        request_id = str(approval.get("request_id") or "")
                                        if request_id:
                                            activity_event = await record_activity(
                                                activity_builder.approval(
                                                    request_id=request_id,
                                                    status=str(approval.get("status") or "denied"),
                                                    details=approval,
                                                ),
                                                force=True,
                                            )
                                            yield encode_sse({
                                                "approval": approval,
                                                "activity_event": activity_event,
                                                "conversation_id": conv_id,
                                                "run_id": run.id,
                                            })
                                    # Harness tool_progress
                                    tp = delta.get("tool_progress")
                                    if tp:
                                        full_tool_progress += (full_tool_progress and "\n" or "") + tp
                                        activity_event = await record_activity(
                                            activity_builder.progress(tp)
                                        )
                                        yield encode_sse({
                                            "tool_progress": tp,
                                            "activity_event": activity_event,
                                            "conversation_id": conv_id,
                                        })
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
                                        activity_event = await record_activity(
                                            activity_builder.progress(
                                                msg, category="model_switch"
                                            )
                                        )
                                        yield encode_sse({
                                            "tool_progress": msg,
                                            "model_switch": switch,
                                            "activity_event": activity_event,
                                            "conversation_id": conv_id,
                                        })
                                    choice = data.get("choices", [{}])[0]
                                    if "error" in data:
                                        stream_error_message = str(data.get("error") or "Harness stream error")
                                    elif isinstance(choice.get("delta"), dict) and choice["delta"].get("error"):
                                        stream_error_message = str(choice["delta"].get("error"))
                                    if choice.get("finish_reason"):
                                        finish_reason = choice["finish_reason"]
                                    if data.get("model"):
                                        resolved_model_id = data["model"]
                                    if reasoning_piece:
                                        full_reasoning += reasoning_piece
                                        activity_event = await record_activity(
                                            activity_builder.stream_text(
                                                "reasoning", reasoning_piece
                                            )
                                        )
                                        yield encode_sse({
                                            "reasoning_delta": reasoning_piece,
                                            "activity_event": activity_event,
                                            "conversation_id": conv_id,
                                        })
                                    if content_piece:
                                        full_content += content_piece
                                        activity_event = await record_activity(
                                            activity_builder.stream_text(
                                                "content", content_piece
                                            )
                                        )
                                        yield encode_sse({
                                            "delta": content_piece,
                                            "activity_event": activity_event,
                                            "conversation_id": conv_id,
                                        })
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    stream_observation.observe_parse_error()
                            if stream_observation.upstream_state == "connected":
                                stream_observation.upstream_state = "eof"
            except AgentEngineError as exc:
                upstream_failure_kind = f"agent_engine_{exc.code}"
                upstream_exception_class = exc.exception_class or type(exc).__name__
                stream_observation.upstream_state = "engine_error"
                stream_error_message = str(exc)
            except httpx.ConnectError as exc:
                upstream_failure_kind = "upstream_harness_connect_error"
                upstream_exception_class = type(exc).__name__
                stream_observation.upstream_state = "connect_error"
                stream_error_message = f"无法连接到 Legacy Harness 服务 {settings.harness_url}。请检查 harness 容器是否在运行。"
            except httpx.TimeoutException as exc:
                upstream_failure_kind = "upstream_harness_timeout"
                upstream_exception_class = type(exc).__name__
                stream_observation.upstream_state = "timeout"
                stream_error_message = "Legacy Harness 服务响应超时。"
            except Exception as e:
                upstream_failure_kind = "upstream_harness_exception"
                upstream_exception_class = type(e).__name__
                stream_observation.upstream_state = "exception"
                stream_error_message = f"调用 Agent Engine 时出错:{type(e).__name__}: {e}"

            error_message, execution_failed = _reconcile_root_stream_error(
                agent_events,
                run_id=run.id,
                stream_error=stream_error_message,
            )

            if error_message:
                warning = _chat_stream_failure_notice(
                    error_message,
                    execution_failed=execution_failed,
                    has_partial_content=bool(full_content),
                )
                if full_content:
                    full_content += warning
                    activity_event = await record_activity(
                        activity_builder.stream_text("content", warning)
                    )
                    yield encode_sse({
                        "delta": warning,
                        "activity_event": activity_event,
                        "conversation_id": conv_id,
                    })
                else:
                    # Surface the error as the assistant's content so the user sees it
                    full_content = warning
                    activity_event = await record_activity(
                        activity_builder.stream_text("content", full_content)
                    )
                    yield encode_sse({
                        "delta": full_content,
                        "activity_event": activity_event,
                        "conversation_id": conv_id,
                    })

            root_terminal_status, _root_terminal_error = (
                _agent_event_terminal_status(agent_events, run_id=run.id)
            )
            provider_failure = _provider_failure_summary(
                agent_events,
                run_id=run.id,
            )
            engine_source = (
                conv.engine_id
                if conv.engine_id != ENGINE_ID_LEGACY
                else "harness"
            )
            if root_terminal_status == "succeeded":
                termination_source = f"upstream_{engine_source}_completed"
            elif root_terminal_status == "failed":
                termination_source = (
                    "provider_failure_reported_by_harness"
                    if provider_failure.get("reported") is True
                    else f"upstream_{engine_source}_failed"
                )
            elif root_terminal_status == "cancelled":
                termination_source = f"upstream_{engine_source}_cancelled"
            else:
                termination_source = (
                    upstream_failure_kind
                    or "upstream_stream_eof_without_terminal"
                )
            stream_observation.terminal_envelope_planned = True
            _append_backend_stream_termination_event(
                agent_events,
                run_id=run.id,
                observation=stream_observation,
                termination_source=termination_source,
                exception_class=upstream_exception_class,
                root_terminal_status=root_terminal_status,
            )
            termination_event_appended = True
            terminal_envelope_payload = {
                "stream_terminal": {
                    "status": root_terminal_status or "interrupted",
                    "complete": root_terminal_status == "succeeded",
                    "finish_reason": _authoritative_root_finish_reason(
                        agent_events,
                        run_id=run.id,
                        transport_finish_reason=finish_reason,
                    ),
                    "termination_source": termination_source,
                },
                "conversation_id": conv_id,
                "run_id": run.id,
            }
        except (asyncio.CancelledError, GeneratorExit) as exc:
            stream_aborted = True
            root_terminal_status, _root_terminal_error = (
                _agent_event_terminal_status(agent_events, run_id=run.id)
            )
            abort_source = _local_abort_source(exc, stream_observation)
            if not termination_event_appended:
                termination_event = _append_backend_stream_termination_event(
                    agent_events,
                    run_id=run.id,
                    observation=stream_observation,
                    termination_source=abort_source,
                    exception_class=type(exc).__name__,
                    root_terminal_status=root_terminal_status,
                )
                termination_event_appended = True
            else:
                termination_event = next(
                    (
                        event for event in reversed(agent_events)
                        if event.get("event_type")
                        == "debug.backend_stream.terminated"
                    ),
                    None,
                )
            if root_terminal_status is None:
                cancelled = True
                finish_reason = "task_cancelled"
                error_message = _cancellation_interruption_message(
                    abort_source
                )
                if not any(
                    marker in full_content
                    for marker in _INCOMPLETE_RESPONSE_MARKERS
                ):
                    full_content += _chat_stream_failure_notice(
                        error_message,
                        execution_failed=False,
                        has_partial_content=bool(full_content),
                    )
                root_seq = max(
                    (
                        int(event.get("seq") or 0)
                        for event in agent_events
                        if str(event.get("run_id") or "") == run.id
                    ),
                    default=0,
                )
                diagnostic_payload = (
                    termination_event.get("payload")
                    if isinstance(termination_event, dict)
                    and isinstance(termination_event.get("payload"), dict)
                    else {}
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
                        "cancellation_source": abort_source,
                        "exception_class": _safe_diagnostic_label(
                            type(exc).__name__
                        ),
                        **_cancellation_diagnostic_fields(
                            diagnostic_payload
                        ),
                        "usage": dict(usage),
                    },
                }
                agent_events.append(cancellation_event)
                seen_agent_events.add(
                    (run.id, "run.cancelled", root_seq + 1)
                )
                await record_activity(
                    activity_builder.agent(cancellation_event),
                    force=True,
                )
            # Cancellation is control flow, but its source is not inferred
            # from the exception class alone. Only an observed ASGI disconnect
            # is labelled client_disconnected; otherwise the debug boundary
            # retains the weaker generator/cancellation/shutdown fact.
            raise
        except Exception as exc:
            upstream_exception_class = type(exc).__name__
            stream_observation.upstream_state = "backend_exception"
            if not termination_event_appended:
                root_terminal_status, _root_terminal_error = (
                    _agent_event_terminal_status(
                        agent_events,
                        run_id=run.id,
                    )
                )
                stream_observation.terminal_envelope_planned = True
                _append_backend_stream_termination_event(
                    agent_events,
                    run_id=run.id,
                    observation=stream_observation,
                    termination_source="backend_stream_exception",
                    exception_class=upstream_exception_class,
                    root_terminal_status=root_terminal_status,
                )
                termination_event_appended = True
            error_message = (
                "Backend stream processing failed before a durable root "
                "terminal event."
            )
            warning = _chat_stream_failure_notice(
                error_message,
                execution_failed=False,
                has_partial_content=bool(full_content),
            )
            if not any(
                marker in full_content
                for marker in _INCOMPLETE_RESPONSE_MARKERS
            ):
                full_content += warning
                activity_event = await record_activity(
                    activity_builder.stream_text(
                        "content",
                        warning if full_content != warning else full_content,
                    )
                )
                yield encode_sse({
                    "delta": warning if full_content != warning else full_content,
                    "activity_event": activity_event,
                    "conversation_id": conv_id,
                })
            terminal_envelope_payload = {
                "stream_terminal": {
                    "status": "interrupted",
                    "complete": False,
                    "finish_reason": "backend_stream_exception",
                    "termination_source": "backend_stream_exception",
                },
                "conversation_id": conv_id,
                "run_id": run.id,
            }
        finally:
            if raw_engine_event_batch:
                try:
                    await _persist_engine_raw_events(
                        user_id=str(cur_user.id),
                        conversation_id=conv_id,
                        run_id=run.id,
                        engine_id=conv.engine_id,
                        envelopes=list(raw_engine_event_batch),
                    )
                except Exception:
                    error_message = (
                        error_message
                        or "Native Agent Engine audit events could not be persisted."
                    )
                raw_engine_event_batch.clear()
            termination_debug_event = next(
                (
                    event for event in reversed(agent_events)
                    if event.get("event_type")
                    == "debug.backend_stream.terminated"
                ),
                None,
            )
            if isinstance(termination_debug_event, dict):
                await _append_backend_stream_debug_file(
                    user_id=str(cur_user.id),
                    session_id=conv_id,
                    run_id=run.id,
                    event=termination_debug_event,
                )
            root_terminal_status, _root_terminal_error = (
                _agent_event_terminal_status(agent_events, run_id=run.id)
            )
            if (
                conv.engine_id in {
                    ENGINE_ID_CLAUDE_CODE,
                    ENGINE_ID_DEEPSEEK_HARNESS,
                }
                and root_terminal_status is None
            ):
                try:
                    from agent_engines.registry import build_agent_engine_registry

                    revoked = await build_agent_engine_registry().get(
                        conv.engine_id
                    ).cancel_run(
                        user_id=str(cur_user.id),
                        conversation_id=conv_id,
                        run_id=run.id,
                    )
                    if not revoked:
                        error_message = (
                            error_message
                            or "Claude Code execution revocation did not converge."
                        )
                except Exception:
                    logger.exception(
                        "Native Agent Engine revocation failed engine=%s conv=%s run=%s",
                        conv.engine_id, conv_id, run.id,
                    )
                    error_message = (
                        error_message
                        or "Claude Code execution revocation did not converge."
                    )
            if (
                not cancelled
                and root_terminal_status is None
                and not full_content
                and not full_reasoning
                and not error_message
            ):
                error_message = "Stream ended before producing a response."
            # Only after the missing native terminal has triggered fail-closed
            # engine revocation may the presentation projection synthesize a
            # terminal card.  Presentation state must never become authority
            # for deciding whether a native process is still live.
            if root_terminal_status is None:
                sealed_events, finish_reason, error_message = (
                    _seal_missing_root_terminal_for_projection(
                        agent_events,
                        run_id=run.id,
                        usage=usage,
                        finish_reason=finish_reason,
                        error_message=error_message,
                    )
                )
                synthetic_terminal = sealed_events[-1]
                agent_events[:] = sealed_events
                seen_agent_events.add((
                    run.id,
                    str(synthetic_terminal.get("event_type") or "run.failed"),
                    int(synthetic_terminal.get("seq") or 0),
                ))
                activity_event_batch.append(
                    activity_builder.agent(synthetic_terminal)
                )
            # A refresh uses a timeline only when this final marker and every
            # preceding event are durable. An unsealed partial projection is
            # ignored in favor of the assistant/run fallback.
            activity_event_batch.append(activity_builder.commit())
            if activity_event_batch:
                try:
                    await _persist_turn_activity_events(
                        user_id=str(cur_user.id),
                        conversation_id=conv_id,
                        root_run_id=run.id,
                        events=list(activity_event_batch),
                    )
                except Exception:
                    logger.exception(
                        "Turn activity projection persistence failed run=%s",
                        run.id,
                    )
                activity_event_batch.clear()
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
                    # The native terminal is only a commit candidate.  The
                    # projection transaction may fail closed after the model
                    # stream has ended, so the public terminal envelope must
                    # reflect the durable AgentRun rather than stale native
                    # success prose.
                    async with async_session() as projection_db:
                        projected_run = await projection_db.get(
                            AgentRun,
                            run.id,
                        )
                    if (
                        projected_run is not None
                        and projected_run.status == "failed"
                        and projected_run.finish_reason
                        == "terminal_projection_failed"
                    ):
                        terminal_envelope_payload = {
                            "stream_terminal": {
                                "status": "failed",
                                "complete": False,
                                "finish_reason": (
                                    "terminal_projection_failed"
                                ),
                                "termination_source": (
                                    "backend_terminal_projection_failed"
                                ),
                            },
                            "conversation_id": conv_id,
                            "run_id": run.id,
                        }
            finally:
                # On disconnect the shielded task remains registered.  The
                # next turn acquires this lock and drains it before history.
                _release_conversation_turn(conv_id, turn_lease)
        if terminal_envelope_payload is not None:
            # This is emitted only after the terminal assistant/run projection
            # above is durable. EOF without it is therefore an interruption,
            # not a successful response.
            yield encode_sse(terminal_envelope_payload)

    relay = _DetachedStreamRelay()
    producer_state = {"started": False}

    async def produce_detached() -> None:
        producer_state["started"] = True
        try:
            async for chunk in generate():
                await relay.publish(chunk)
        except BaseException as exc:
            relay.finish(exc)
            raise
        else:
            relay.finish()

    def recover_prestart_exit(error: BaseException | None) -> None:
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
            "seq": 1,
            "payload": {
                "finish_reason": "producer_not_started",
                "terminal_reason": "producer_not_started",
                "cancellation_source": "backend_producer_prestart_exit",
                "exception_class": (
                    _safe_diagnostic_label(type(error).__name__)
                    if error is not None
                    else None
                ),
                "authoritative": True,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                },
            },
        }
        _spawn_persist_then_emit(
            user_id=cur_user.id,
            conv_id=conv_id,
            model_id=model_id,
            content="",
            reasoning="",
            tool_progress="",
            first_user_content=req.content,
            run_id=run.id,
            resolved_model_id=model_id,
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            finish_reason="producer_not_started",
            error_message=None,
            agent_events=[cancellation_event],
        )
        _release_conversation_turn(conv_id, turn_lease)

    _track_detached_chat_producer(
        conv_id=conv_id,
        run_id=run.id,
        relay=relay,
        operation=produce_detached(),
        producer_started=lambda: producer_state["started"],
        on_prestart_exit=recover_prestart_exit,
    )

    async def announce_chat_start() -> None:
        await emit_event(
            cur_user.id,
            "message.created",
            {
                "conversation_id": conv_id,
                "message_id": user_msg.id,
                "role": "user",
                "model_id": model_id,
                "source": source,
            },
            conv_id,
        )
        await emit_event(
            cur_user.id,
            "run.started",
            {
                "conversation_id": conv_id,
                "run_id": run.id,
                "model_id": model_id,
            },
            conv_id,
        )

    _track_best_effort_task(
        announce_chat_start(),
        description=f"chat start hooks conv={conv_id} run={run.id}",
    )

    def persist_downstream_final(
        observation: _StreamObservation,
    ) -> None:
        if relay.detach_reason == "subscriber_backpressure":
            observation.mark_downstream(
                "subscriber_backpressure_detached"
            )
        event = {
            "type": "agent_event",
            "event_type": "debug.backend_stream.downstream_final",
            "run_id": run.id,
            "root_run_id": run.id,
            "parent_run_id": None,
            "agent_kind": "primary",
            "agent_name": "primary",
            "depth": 0,
            "workspace_scope": "shared_session",
            "seq": 0,
            "payload": {
                **observation.snapshot(),
                "observation_kind": "downstream_final",
            },
        }
        _track_best_effort_task(
            _append_backend_stream_debug_file(
                user_id=str(cur_user.id),
                session_id=conv_id,
                run_id=run.id,
                event=event,
            ),
            description=(
                f"backend stream debug mirror conv={conv_id} run={run.id}"
            ),
        )
        if _should_persist_agent_event_immediately(event["event_type"]):
            _spawn_agent_event_immediate_persist(
                conv_id=conv_id,
                user_id=str(cur_user.id),
                root_run_id=run.id,
                requested_model_id=model_id,
                resolved_model_id=model_id,
                event=event,
            )

    return _ObservedStreamingResponse(
        relay.stream(),
        observation=stream_observation,
        on_downstream_final=persist_downstream_final,
        media_type="text/event-stream",
    )


@router.post("/completions")
async def chat_completion(
    req: ChatRequest,
    request: Request,
    cur_user=Depends(get_current_user),
    db=Depends(get_db),
):
    return await _chat_stream(
        req,
        cur_user,
        db,
        ingress_request_id=request.headers.get("x-request-id"),
    )
