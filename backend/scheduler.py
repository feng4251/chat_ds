"""Persistent session-scoped scheduler for agent jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from agent_engines.base import (
    ENGINE_ID_CLAUDE_CODE,
    ENGINE_ID_DEEPSEEK_HARNESS,
    ENGINE_ID_LEGACY,
)
from config import settings
from database import async_session
from hooks import emit_event
from model_routing import (
    DEFAULT_AGENT_MODEL_ID,
)
from native_tools import (
    PLATFORM_IO_CAPABILITY_SET,
    canonicalize_scheduled_platform_capabilities,
)
from models import (
    AgentRun,
    Conversation,
    ScheduledJob,
    ScheduledJobRun,
    User,
)
from workspace import ensure_workspace_async, serialize_json_list
from workspace_lock import WorkspaceMutationLockError
from schemas import ScheduledJobCreate
from schedule_spec import (
    ScheduleSpecError,
    next_cron_occurrence,
    resolve_schedule_spec,
)

logger = logging.getLogger(__name__)

_CRON_THREAT_PATTERNS = (
    (re.compile(r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions", re.I), "prompt_injection"),
    (re.compile(r"do\s+not\s+tell\s+the\s+user", re.I), "deception_hide"),
    (re.compile(r"system\s+prompt\s+override", re.I), "system_prompt_override"),
    (re.compile(r"disregard\s+(?:your|all|any)\s+(?:instructions|rules|guidelines)", re.I), "disregard_rules"),
    (re.compile(r"cat\s+[^\n]*(?:\.env|credentials|\.netrc|\.pgpass)", re.I), "read_secrets"),
    (re.compile(r"authorized_keys", re.I), "ssh_backdoor"),
    (re.compile(r"/etc/sudoers|visudo", re.I), "sudoers_modification"),
    (re.compile(r"rm\s+-rf\s+/", re.I), "destructive_root_delete"),
    (re.compile(r"curl\s+[^\n]*(?:--data|-d|--form|-F)[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?", re.I), "secret_exfiltration"),
)
_CRON_INVISIBLE = {
    "\u200b", "\u200c", "\u2060", "\ufeff",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
}


@dataclass
class _JobExecutionFlight:
    """One runtime-owned execution claim for an exact scheduled job."""

    force_requested: bool
    task: asyncio.Task[None] | None = None
    run_id: str | None = None


_JOB_EXECUTION_FLIGHTS: dict[str, _JobExecutionFlight] = {}


def _utcnow() -> datetime:
    return datetime.utcnow()


def scan_cron_prompt(prompt: str) -> str | None:
    """Return a stable threat identifier for unsafe unattended prompts."""
    for char in _CRON_INVISIBLE:
        if char in prompt:
            return f"invisible_unicode_U+{ord(char):04X}"
    for pattern, threat in _CRON_THREAT_PATTERNS:
        if pattern.search(prompt):
            return threat
    return None


def parse_schedule(
    schedule: str,
    timezone_name: str = "UTC",
    *,
    now: datetime | None = None,
) -> tuple[str, str, datetime]:
    resolved = resolve_schedule_spec(
        schedule,
        timezone_name,
        now=now,
    )
    return resolved.kind, resolved.value, resolved.next_run_at


def next_run_for(job: ScheduledJob, after: datetime | None = None) -> datetime | None:
    base_utc = (after or _utcnow()).replace(tzinfo=timezone.utc)
    if job.schedule_kind == "once":
        return None
    if job.schedule_kind == "interval":
        return (base_utc + timedelta(seconds=int(job.schedule_value))).replace(tzinfo=None)
    if job.schedule_kind == "cron":
        return next_cron_occurrence(
            job.schedule_value,
            job.timezone,
            after=base_utc,
        )
    return None


def enqueue_job_execution(
    job_id: str,
    *,
    force: bool = False,
) -> tuple[asyncio.Task[None], bool]:
    """Start or join one per-job flight without ever queuing a duplicate.

    A force request upgrades an existing automatic flight in place. It never
    schedules a second run behind an active one.
    """

    key = str(job_id)
    existing = _JOB_EXECUTION_FLIGHTS.get(key)
    if (
        existing is not None
        and existing.task is not None
        and not existing.task.done()
    ):
        if force:
            existing.force_requested = True
        return existing.task, False

    flight = _JobExecutionFlight(force_requested=bool(force))
    task = asyncio.create_task(
        _execute_job_once(key, flight),
        name=f"scheduled-job:{key}",
    )
    flight.task = task
    _JOB_EXECUTION_FLIGHTS[key] = flight

    def finish(completed: asyncio.Task[None]) -> None:
        if _JOB_EXECUTION_FLIGHTS.get(key) is flight:
            _JOB_EXECUTION_FLIGHTS.pop(key, None)
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            error = None
        if error is not None:
            logger.error(
                "Scheduled job execution failed job=%s",
                key,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(finish)
    return task, True


async def execute_job(job_id: str, *, force: bool = False) -> None:
    """Start or join the exact per-job flight and wait without owning it."""

    task, _started = enqueue_job_execution(job_id, force=force)
    await asyncio.shield(task)


def _job_may_run(job: ScheduledJob | None, flight: _JobExecutionFlight) -> bool:
    if job is None:
        return False
    if job.max_runs is not None and job.run_count >= job.max_runs:
        return False
    if job.expires_at is not None and _utcnow() > job.expires_at:
        return False
    if flight.force_requested:
        return True
    return bool(
        job.enabled
        and job.next_run_at is not None
        and job.next_run_at <= _utcnow()
    )


def _scheduled_job_platform_capabilities(job: ScheduledJob) -> list[str]:
    """Resolve the persisted three-state platform I/O contract exactly."""

    raw = (
        None
        if job.enabled_tools is None
        else serialize_json_list(job.enabled_tools, [])
    )
    return list(canonicalize_scheduled_platform_capabilities(
        raw,
        allowed_capabilities=PLATFORM_IO_CAPABILITY_SET,
    ))


async def stage_schedule_control_writes(
    db,
    *,
    user_id: str,
    conversation_id: str,
    root_run_id: str,
    model_id: str,
    writes: object,
    allowed_platform_capabilities: set[str] | frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Stage validated, idempotent schedule rows in the root projection txn.

    Identity is controller-bound; model arguments cannot select a user,
    Session, engine, or ownership scope. Re-projecting the same durable root
    terminal derives the same primary key and therefore cannot duplicate an
    unattended side effect.
    """

    if not isinstance(writes, list) or len(writes) > 64:
        raise ValueError("schedule_control_writes_invalid")
    created: list[str] = []
    observed_tool_calls: set[str] = set()
    bound_platform_capabilities = (
        PLATFORM_IO_CAPABILITY_SET
        if allowed_platform_capabilities is None
        else frozenset(allowed_platform_capabilities)
    )

    for row in writes:
        if (
            not isinstance(row, dict)
            or row.get("schema") != "chatds.schedule-write.v1"
            or row.get("operation") != "create"
        ):
            raise ValueError("schedule_control_write_invalid")
        tool_call_id = row.get("tool_call_id")
        if (
            not isinstance(tool_call_id, str)
            or not tool_call_id
            or len(tool_call_id) > 256
            or tool_call_id in observed_tool_calls
        ):
            raise ValueError("schedule_control_tool_call_invalid")
        observed_tool_calls.add(tool_call_id)
        request = _normalize_schedule_control_request(row.get("request"))
        platform_capabilities = canonicalize_scheduled_platform_capabilities(
            request.get("platform_capabilities"),
            allowed_capabilities=bound_platform_capabilities,
        )
        threat = scan_cron_prompt(request["prompt"])
        if threat:
            raise ValueError(f"schedule_control_prompt_{threat}")
        try:
            resolved = resolve_schedule_spec(
                request["schedule"],
                request["timezone"],
                expires_at=request.get("expires_at"),
            )
        except ScheduleSpecError as exc:
            if exc.code == "schedule_expiry_timezone_missing":
                raise ValueError(
                    "schedule_control_expiry_timezone_missing"
                ) from exc
            if exc.code == "schedule_no_occurrence_before_expiry":
                raise ValueError(
                    "schedule_control_no_occurrence_before_expiry"
                ) from exc
            raise ValueError(f"schedule_control_{exc.code}") from exc
        job_id = hashlib.sha256(
            (
                "chatds.schedule-control.v1\0"
                + root_run_id
                + "\0"
                + tool_call_id
            ).encode("utf-8")
        ).hexdigest()[:32]
        existing = await db.get(ScheduledJob, job_id)
        if existing is not None:
            if (
                existing.user_id != user_id
                or existing.conversation_id != conversation_id
            ):
                raise ValueError("schedule_control_identity_collision")
            created.append(job_id)
            continue
        db.add(ScheduledJob(
            id=job_id,
            user_id=user_id,
            conversation_id=conversation_id,
            name=request["name"],
            prompt=request["prompt"],
            schedule_kind=resolved.kind,
            schedule_value=resolved.value,
            timezone=request["timezone"],
            model_id=model_id,
            # Persist the exact bound subset, including an explicit empty
            # set. ``NULL`` represents historical/controller-created rows that
            # omitted an explicit platform I/O contract.
            enabled_tools=json.dumps(
                list(platform_capabilities),
                ensure_ascii=False,
            ),
            enabled=True,
            delete_after_run=request["delete_after_run"],
            max_runs=request.get("max_runs"),
            run_count=0,
            expires_at=resolved.expires_at,
            next_run_at=resolved.next_run_at,
        ))
        created.append(job_id)
    return tuple(created)


def _normalize_schedule_control_request(value: object) -> dict:
    allowed = {
        "name", "prompt", "schedule", "timezone", "max_runs",
        "expires_at", "platform_capabilities", "delete_after_run",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("schedule_control_request_invalid")
    try:
        payload = ScheduledJobCreate.model_validate(value)
    except Exception as exc:
        raise ValueError("schedule_control_request_invalid") from exc
    if payload.conversation_id is not None or payload.model_id is not None:
        raise ValueError("schedule_control_identity_override")
    return {
        "name": payload.name.strip(),
        "prompt": payload.prompt.strip(),
        "schedule": payload.schedule.strip(),
        "timezone": payload.timezone.strip(),
        "max_runs": payload.max_runs,
        "expires_at": payload.expires_at,
        "platform_capabilities": payload.platform_capabilities,
        "delete_after_run": payload.delete_after_run,
    }


async def _persist_job_run_terminal(
    job_id: str,
    run_id: str | None,
    *,
    status: str,
    error: str | None,
) -> bool:
    """Idempotently terminalize one exact running row in a fresh session."""

    if not run_id:
        return False
    if status not in {"failed", "cancelled"}:
        raise ValueError("Scheduled terminal status must be failed/cancelled.")
    async with async_session() as db:
        run = (await db.execute(
            select(ScheduledJobRun).where(
                ScheduledJobRun.id == str(run_id),
                ScheduledJobRun.job_id == str(job_id),
            )
        )).scalar_one_or_none()
        if run is None or str(run.status) != "running":
            return False
        job = await db.get(ScheduledJob, str(job_id))
        run.status = status
        run.error = error if status == "failed" else None
        run.ended_at = _utcnow()
        if job is not None:
            job.last_status = status
            if status == "failed":
                job.consecutive_errors += 1
            if job.schedule_kind == "once":
                job.enabled = False
        await db.commit()
    return True


def _outer_job_failure_terminal(
    exc: Exception,
) -> tuple[str, str | None]:
    if (
        isinstance(exc, WorkspaceMutationLockError)
        and exc.code == "workspace_session_deleted"
    ):
        return "cancelled", None
    message = f"{type(exc).__name__}: {exc}"
    return "failed", message[:1000]


async def _execute_job_once(
    job_id: str,
    flight: _JobExecutionFlight,
) -> None:
    """Bind a session, then run under the shared delete/turn authority."""

    try:
        async with async_session() as db:
            job = (await db.execute(
                select(ScheduledJob).where(ScheduledJob.id == job_id)
            )).scalar_one_or_none()
            if not _job_may_run(job, flight):
                return
            if not job.conversation_id:
                conv = Conversation(
                    user_id=job.user_id,
                    title=f"定时任务 · {job.name}",
                    model_id=job.model_id or DEFAULT_AGENT_MODEL_ID,
                    engine_id=settings.default_agent_engine_id,
                    # A controller-created unattended Session has no browser
                    # connected to resolve native one-shot prompts.  Its
                    # authority remains confined to the dedicated Session.
                    permission_preset="session_full",
                )
                db.add(conv)
                await db.flush()
                job.conversation_id = conv.id
                await db.commit()
            else:
                conv = (await db.execute(
                    select(Conversation).where(
                        Conversation.id == job.conversation_id,
                        Conversation.user_id == job.user_id,
                    )
                )).scalar_one_or_none()
                if conv is None:
                    return
            user_id = str(job.user_id)
            conversation_id = str(job.conversation_id)

        from routers.chat_router import (
            conversation_maintenance_lease,
            registered_conversation_producer,
        )
        async with registered_conversation_producer(
            user_id,
            conversation_id,
        ):
            if str(conv.engine_id) != ENGINE_ID_LEGACY:
                # The ordinary native-engine chat path owns the conversation turn
                # lease from history snapshot through durable projection.
                # Holding it here as well would self-deadlock on the same
                # non-reentrant per-Conversation lock.
                await _execute_job_bound(job_id, flight=flight)
            else:
                # A historical retired-engine row is terminalized without
                # dispatch; retain serialization while disabling its job.
                async with conversation_maintenance_lease(conversation_id):
                    await _execute_job_bound(job_id, flight=flight)
    except asyncio.CancelledError:
        try:
            await _persist_job_run_terminal(
                job_id,
                flight.run_id,
                status="cancelled",
                error=None,
            )
        except BaseException:
            logger.exception(
                "Could not persist scheduled cancellation job=%s run=%s",
                job_id,
                flight.run_id,
            )
        raise
    except Exception as exc:
        status, error = _outer_job_failure_terminal(exc)
        try:
            await _persist_job_run_terminal(
                job_id,
                flight.run_id,
                status=status,
                error=error,
            )
        except BaseException:
            logger.exception(
                "Could not persist scheduled failure job=%s run=%s",
                job_id,
                flight.run_id,
            )
        raise


async def _commit_scheduled_session_state(
    db,
    *,
    user_id: str,
    conversation_id: str,
) -> None:
    """Commit only while the durable session fence and DB owner are live."""

    from workspace import require_session_workspace_active

    require_session_workspace_active(user_id, conversation_id)
    owner = (await db.execute(
        select(Conversation.id).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )).scalar_one_or_none()
    if owner is None:
        raise asyncio.CancelledError(
            "Scheduled session authority no longer exists."
        )
    await db.commit()


async def _execute_job_bound(
    job_id: str,
    *,
    flight: _JobExecutionFlight,
) -> None:
    async with async_session() as db:
        job = (await db.execute(
            select(ScheduledJob).where(ScheduledJob.id == job_id)
        )).scalar_one_or_none()
        if not _job_may_run(job, flight):
            return
        if not job.conversation_id:
            return
        conv = (await db.execute(
            select(Conversation).where(
                Conversation.id == job.conversation_id,
                Conversation.user_id == job.user_id,
            )
        )).scalar_one_or_none()
        if conv is None:
            return
        run = ScheduledJobRun(
            job_id=job.id,
            conversation_id=job.conversation_id,
            status="running",
            model_id=job.model_id,
        )
        job.last_run_at = _utcnow()
        job.run_count += 1
        candidate_next_run = next_run_for(job, job.last_run_at)
        if (
            job.schedule_kind == "once"
            or job.max_runs is not None and job.run_count >= job.max_runs
            or job.expires_at is not None
            and (
                candidate_next_run is None
                or candidate_next_run > job.expires_at
            )
        ):
            job.next_run_at = None
            job.enabled = False
        else:
            job.next_run_at = candidate_next_run
        db.add(run)
        await db.flush()
        flight.run_id = str(run.id)
        await _commit_scheduled_session_state(
            db,
            user_id=str(job.user_id),
            conversation_id=str(conv.id),
        )
        await db.refresh(run)
        await emit_event(job.user_id, "cron.started", {"job_id": job.id, "run_id": run.id}, job.conversation_id)
        if conv.engine_id == ENGINE_ID_LEGACY:
            error = (
                "The retired ChatDS Harness cannot execute scheduled Turns; "
                "fork this Conversation to a native Agent Engine"
            )
            run.status = "failed"
            run.error = error
            run.ended_at = _utcnow()
            job.last_status = "failed"
            job.consecutive_errors += 1
            job.enabled = False
            job.next_run_at = None
            await _commit_scheduled_session_state(
                db,
                user_id=str(job.user_id),
                conversation_id=str(conv.id),
            )
            await emit_event(
                job.user_id,
                "cron.failed",
                {"job_id": job.id, "run_id": run.id, "error": error},
                job.conversation_id,
            )
            return
        threat = scan_cron_prompt(job.prompt)
        if threat:
            error = f"Unsafe unattended prompt blocked at runtime: {threat}"
            run.status = "failed"
            run.error = error
            run.ended_at = _utcnow()
            job.last_status = "failed"
            job.consecutive_errors += 1
            if job.schedule_kind == "once":
                job.enabled = False
            await _commit_scheduled_session_state(
                db,
                user_id=str(job.user_id),
                conversation_id=str(conv.id),
            )
            await emit_event(
                job.user_id,
                "cron.failed",
                {"job_id": job.id, "run_id": run.id, "error": error},
                job.conversation_id,
            )
            return

        await ensure_workspace_async(job.user_id, conv.id)
        platform_capabilities = _scheduled_job_platform_capabilities(job)

        if conv.engine_id == ENGINE_ID_CLAUDE_CODE:
            native_executor = _execute_claude_scheduled_turn
        elif conv.engine_id == ENGINE_ID_DEEPSEEK_HARNESS:
            native_executor = _execute_native_scheduled_turn
        else:
            raise RuntimeError(
                f"Unsupported native Agent Engine: {conv.engine_id}"
            )
        await native_executor(
            db,
            job=job,
            conv=conv,
            scheduled_run=run,
            platform_capabilities=platform_capabilities,
        )
        return
async def _execute_native_scheduled_turn(
    db,
    *,
    job: ScheduledJob,
    conv: Conversation,
    scheduled_run: ScheduledJobRun,
    platform_capabilities: list[str],
    _engine_id_override: str | None = None,
) -> None:
    """Execute unattended work through the Conversation's native engine.

    This deliberately reuses the ordinary chat ingestion/projection path, so
    checkpoints, exact Skills, workspace locking, terminal receipts and model
    bindings cannot drift between interactive and scheduled Turns.
    """

    from routers.chat_router import _chat_stream
    from schemas import ChatRequest

    user = await db.get(User, str(job.user_id))
    if user is None:
        raise RuntimeError("Scheduled job owner no longer exists")
    response = await _chat_stream(
        ChatRequest(
            conversation_id=str(conv.id),
            content=str(job.prompt),
            model_id=(str(job.model_id) if job.model_id else None),
            engine_id=_engine_id_override or str(conv.engine_id),
        ),
        user,
        db,
        source="cron",
        platform_capabilities_override=tuple(platform_capabilities),
    )
    agent_run_id: str | None = None
    output_parts: list[str] = []
    async for raw_chunk in response.body_iterator:
        text_chunk = (
            raw_chunk.decode("utf-8", "replace")
            if isinstance(raw_chunk, bytes)
            else str(raw_chunk)
        )
        for line in text_chunk.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                payload = json.loads(line[6:])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            candidate_run = payload.get("run_id")
            if isinstance(candidate_run, str) and candidate_run:
                agent_run_id = agent_run_id or candidate_run
            delta = payload.get("delta")
            if isinstance(delta, str):
                output_parts.append(delta)
    if not agent_run_id:
        raise RuntimeError("Scheduled Claude Turn returned no durable run identity")

    # The terminal SSE is emitted only after the root projection commits.
    # End the request Session's read transaction before observing that commit.
    await db.rollback()
    agent_run = await db.get(AgentRun, agent_run_id)
    if agent_run is None or agent_run.source != "cron":
        raise RuntimeError("Scheduled Claude Turn projection is missing")
    # _chat_stream inserted this AgentRun through the request Session, while
    # its terminal projector committed through an independent Session.  A
    # primary-key get may therefore return the request Session's stale
    # identity-map object even though the terminal SSE is already durable.
    # Force a database refresh before deriving the outer schedule terminal.
    await db.refresh(agent_run)
    if agent_run.status not in {"succeeded", "failed", "cancelled"}:
        raise RuntimeError(
            "Scheduled Claude Turn durable projection is nonterminal: "
            f"{agent_run.status}"
        )
    await db.refresh(scheduled_run)
    await db.refresh(job)
    scheduled_run.ended_at = _utcnow()
    scheduled_run.output = "".join(output_parts)
    scheduled_run.model_id = agent_run.resolved_model_id or agent_run.requested_model_id
    scheduled_run.input_tokens = int(agent_run.input_tokens or 0)
    scheduled_run.output_tokens = int(agent_run.output_tokens or 0)
    scheduled_run.total_tokens = int(agent_run.total_tokens or 0)
    if agent_run.status == "succeeded":
        scheduled_run.status = "succeeded"
        scheduled_run.error = None
        job.last_status = "succeeded"
        job.consecutive_errors = 0
    else:
        scheduled_run.status = (
            "cancelled" if agent_run.status == "cancelled" else "failed"
        )
        scheduled_run.error = agent_run.error or agent_run.finish_reason
        job.last_status = scheduled_run.status
        if scheduled_run.status == "failed":
            job.consecutive_errors += 1
    await _commit_scheduled_session_state(
        db,
        user_id=str(job.user_id),
        conversation_id=str(conv.id),
    )
    await emit_event(
        str(job.user_id),
        "cron.completed" if scheduled_run.status == "succeeded" else "cron.failed",
        {
            "job_id": str(job.id),
            "run_id": str(scheduled_run.id),
            "agent_run_id": agent_run_id,
            "conversation_id": str(conv.id),
            "error": scheduled_run.error,
        },
        str(conv.id),
    )


async def _execute_claude_scheduled_turn(
    db,
    *,
    job: ScheduledJob,
    conv: Conversation,
    scheduled_run: ScheduledJobRun,
    platform_capabilities: list[str],
) -> None:
    """Backward-compatible name for the shared native-engine transaction."""

    await _execute_native_scheduled_turn(
        db,
        job=job,
        conv=conv,
        scheduled_run=scheduled_run,
        platform_capabilities=platform_capabilities,
        _engine_id_override=ENGINE_ID_CLAUDE_CODE,
    )


async def enqueue_due_jobs_once() -> dict[str, int]:
    """Snapshot due ids and coalesce them into runtime-owned job flights."""

    async with async_session() as db:
        due = (await db.execute(
            select(ScheduledJob.id).where(
                ScheduledJob.enabled.is_(True),
                ScheduledJob.next_run_at.is_not(None),
                ScheduledJob.next_run_at <= _utcnow(),
            ).limit(20)
        )).scalars().all()
    started = 0
    for job_id in due:
        _task, created = enqueue_job_execution(str(job_id))
        if created:
            started += 1
    return {
        "due": len(due),
        "started": started,
        "coalesced": len(due) - started,
    }


async def shutdown_scheduled_job_executions(
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, int | bool]:
    """Cancel and drain every runtime-owned scheduled flight."""

    tasks = {
        flight.task
        for flight in _JOB_EXECUTION_FLIGHTS.values()
        if flight.task is not None and not flight.task.done()
    }
    for task in tasks:
        task.cancel()
    residual: set[asyncio.Task] = set()
    done: set[asyncio.Task] = set()
    if tasks:
        done, residual = await asyncio.wait(
            tasks,
            timeout=max(0.1, min(float(timeout_seconds), 60.0)),
            return_when=asyncio.ALL_COMPLETED,
        )
    for task in done:
        try:
            task.exception()
        except BaseException:
            pass
    return {
        "success": not residual,
        "cancelled_count": len(done),
        "residual_count": len(residual),
    }


async def scheduler_loop() -> None:
    """Claim and execute due jobs. A single backend process owns this loop."""
    while True:
        try:
            await enqueue_due_jobs_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(max(5, settings.scheduler_poll_seconds))
