"""Persistent session-scoped scheduler for agent jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
try:
    from croniter import croniter
except ImportError:  # Local source checks may run before requirements are installed.
    croniter = None
from sqlalchemy import select

from config import settings
from database import async_session
from hooks import emit_event
from model_routing import (
    DEFAULT_AGENT_MODEL_ID,
    canonical_agent_model_id,
    filter_agentic_fallback_model_ids,
)
from models import (
    AgentRun,
    Conversation,
    CustomModelConfig,
    Message,
    ScheduledJob,
    ScheduledJobRun,
    User,
)
from workspace import ensure_workspace_async, serialize_json_list
from workspace_lock import WorkspaceMutationLockError
from schemas import ScheduledJobCreate

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(
    r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
    re.I,
)
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


def _parse_duration(value: str) -> timedelta:
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise ValueError("Use a duration like 30m, 2h, or 1d.")
    amount = int(match.group(1))
    unit = match.group(2).lower()[0]
    return {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]


def scan_cron_prompt(prompt: str) -> str | None:
    """Return a stable threat identifier for unsafe unattended prompts."""
    for char in _CRON_INVISIBLE:
        if char in prompt:
            return f"invisible_unicode_U+{ord(char):04X}"
    for pattern, threat in _CRON_THREAT_PATTERNS:
        if pattern.search(prompt):
            return threat
    return None


def parse_schedule(schedule: str, timezone_name: str = "UTC") -> tuple[str, str, datetime]:
    raw = schedule.strip()
    lower = raw.lower()
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc
    now_local = datetime.now(tz)

    if lower.startswith("every "):
        duration = _parse_duration(raw[6:].strip())
        return "interval", str(int(duration.total_seconds())), (now_local + duration).astimezone(timezone.utc).replace(tzinfo=None)

    parts = raw.split()
    if len(parts) in {5, 6}:
        if croniter is None:
            raise ValueError("Cron expressions require the croniter dependency.")
        try:
            next_local = croniter(raw, now_local).get_next(datetime)
            if next_local.tzinfo is None:
                next_local = next_local.replace(tzinfo=tz)
            return "cron", raw, next_local.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            pass

    if lower.startswith("in "):
        duration = _parse_duration(raw[3:].strip())
        return "once", (now_local + duration).isoformat(), (now_local + duration).astimezone(timezone.utc).replace(tzinfo=None)

    try:
        duration = _parse_duration(raw)
        run_at = now_local + duration
        return "once", run_at.isoformat(), run_at.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        pass

    try:
        run_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=tz)
        return "once", run_at.isoformat(), run_at.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError as exc:
        raise ValueError(
            "Invalid schedule. Use '30m', 'every 2h', an ISO timestamp, or a cron expression."
        ) from exc


def next_run_for(job: ScheduledJob, after: datetime | None = None) -> datetime | None:
    base_utc = (after or _utcnow()).replace(tzinfo=timezone.utc)
    if job.schedule_kind == "once":
        return None
    if job.schedule_kind == "interval":
        return (base_utc + timedelta(seconds=int(job.schedule_value))).replace(tzinfo=None)
    if job.schedule_kind == "cron":
        if croniter is None:
            raise RuntimeError("croniter is required to advance cron schedules")
        tz = ZoneInfo(job.timezone)
        local_base = base_utc.astimezone(tz)
        next_local = croniter(job.schedule_value, local_base).get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=tz)
        return next_local.astimezone(timezone.utc).replace(tzinfo=None)
    return None


def _provider_payload(model_id: str, config: dict) -> dict:
    return {
        "id": model_id,
        "base_url": config["base_url"],
        "api_key": config["api_key"],
        "api_model": config["api_model"],
        "provider": config.get("provider", "openai"),
        "protocol": config.get("protocol", "openai"),
        "is_multimodal": config.get("is_multimodal", False),
        "context_length": config.get("context_length", 262144),
        "discover_runtime_metadata": bool(
            config.get("discover_runtime_metadata", False)
        ),
        "agentic_auxiliary_only": config.get(
            "agentic_auxiliary_only", False
        ),
        "supports_thinking_toggle": config.get(
            "supports_thinking_toggle", False
        ),
        "thinking_enabled_by_default": config.get(
            "thinking_enabled_by_default", True
        ),
        "thinking_request_format": config.get(
            "thinking_request_format", ""
        ),
        "thinking_send_enabled_explicitly": bool(
            config.get("thinking_send_enabled_explicitly", False)
        ),
    }


async def _resolve_job_model(
    db,
    user_id: str,
    model_id: str,
) -> dict:
    from routers.chat_router import BUILTIN
    if model_id in BUILTIN:
        return _provider_payload(model_id, BUILTIN[model_id])
    custom = (await db.execute(
        select(CustomModelConfig).where(
            CustomModelConfig.user_id == user_id,
            CustomModelConfig.model_id == model_id,
        )
    )).scalar_one_or_none()
    if custom is None:
        raise ValueError(f"Unknown model: {model_id}")
    try:
        extra_headers = json.loads(custom.extra_headers) if custom.extra_headers else {}
    except (TypeError, json.JSONDecodeError):
        extra_headers = {}
    if not isinstance(extra_headers, dict):
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


def _harness_client() -> httpx.AsyncClient:
    """Create the scheduler client with the shared long-run Harness deadline."""

    return httpx.AsyncClient(
        timeout=settings.harness_stream_timeout_seconds
    )


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


async def stage_schedule_control_writes(
    db,
    *,
    user_id: str,
    conversation_id: str,
    root_run_id: str,
    model_id: str,
    writes: object,
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
    from native_tools import DEFAULT_NATIVE_TOOL_SET

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
        tools = request.get("enabled_tools")
        if tools is not None and set(tools) - DEFAULT_NATIVE_TOOL_SET:
            raise ValueError("schedule_control_tools_unknown")
        threat = scan_cron_prompt(request["prompt"])
        if threat:
            raise ValueError(f"schedule_control_prompt_{threat}")
        kind, value, next_run = parse_schedule(
            request["schedule"], request["timezone"]
        )
        expires_at = None
        parsed_expiry = request.get("expires_at")
        if parsed_expiry is not None:
            if parsed_expiry.tzinfo is None:
                raise ValueError("schedule_control_expiry_timezone_missing")
            expires_at = parsed_expiry.astimezone(timezone.utc).replace(tzinfo=None)
            if next_run > expires_at:
                raise ValueError("schedule_control_no_occurrence_before_expiry")
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
            schedule_kind=kind,
            schedule_value=value,
            timezone=request["timezone"],
            model_id=model_id,
            enabled_tools=(
                json.dumps(tools, ensure_ascii=False)
                if tools is not None else None
            ),
            enabled=True,
            delete_after_run=request["delete_after_run"],
            max_runs=request.get("max_runs"),
            run_count=0,
            expires_at=expires_at,
            next_run_at=next_run,
        ))
        created.append(job_id)
    return tuple(created)


def _normalize_schedule_control_request(value: object) -> dict:
    allowed = {
        "name", "prompt", "schedule", "timezone", "max_runs",
        "expires_at", "enabled_tools", "delete_after_run",
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
        "enabled_tools": payload.enabled_tools,
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
                )
                db.add(conv)
                await db.flush()
                job.conversation_id = conv.id
                await db.commit()
            user_id = str(job.user_id)
            conversation_id = str(job.conversation_id)

        from routers.chat_router import registered_conversation_execution
        async with registered_conversation_execution(
            user_id,
            conversation_id,
        ):
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
        model_id = canonical_agent_model_id(
            job.model_id or conv.model_id or DEFAULT_AGENT_MODEL_ID
        )
        tools = serialize_json_list(job.enabled_tools, [])
        if not tools:
            from native_tools import UNATTENDED_DEFAULT_NATIVE_TOOLS
            tools = list(UNATTENDED_DEFAULT_NATIVE_TOOLS)

        if conv.engine_id == "claude_code":
            await _execute_claude_scheduled_turn(
                db,
                job=job,
                conv=conv,
                scheduled_run=run,
                tools=tools,
            )
            return

        provider_config = await _resolve_job_model(db, job.user_id, model_id)
        fallback_configs: list[dict] = []
        fallback_ids, removed_fallback_ids = filter_agentic_fallback_model_ids(
            serialize_json_list(conv.fallback_model_ids, []),
            requested_model_id=model_id,
        )
        if removed_fallback_ids:
            logger.info(
                "Filtered auxiliary/duplicate agentic fallbacks job=%s "
                "requested_model=%s removed=%s",
                job.id,
                model_id,
                removed_fallback_ids,
            )
        for fallback_id in fallback_ids:
            try:
                fallback_configs.append(
                    await _resolve_job_model(db, job.user_id, fallback_id)
                )
            except ValueError:
                logger.warning(
                    "Ignoring unknown fallback model job=%s model=%s",
                    job.id,
                    fallback_id,
                )
        user_message = Message(
            conversation_id=conv.id,
            role="user",
            content=job.prompt,
            model_id=model_id,
            source="cron",
        )
        db.add(user_message)
        await _commit_scheduled_session_state(
            db,
            user_id=str(job.user_id),
            conversation_id=str(conv.id),
        )
        await emit_event(
            job.user_id,
            "message.created",
            {
                "conversation_id": conv.id,
                "message_id": user_message.id,
                "role": "user",
                "model_id": model_id,
                "source": "cron",
            },
            conv.id,
        )

        history_rows = (await db.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at)
        )).scalars().all()
        messages = [
            {"role": row.role, "content": row.content or ""}
            for row in history_rows[-40:]
        ]

        full_content = ""
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        resolved_model = model_id
        error = None
        try:
            async with _harness_client() as client:
                response = await client.post(
                    f"{settings.harness_url}/v1/chat/completions",
                    headers={
                        "X-Internal-Token": settings.internal_api_token,
                    },
                    json={
                        "model": model_id,
                        "messages": messages,
                        "stream": False,
                        "tools": tools,
                        "session_id": conv.id,
                        "user": job.user_id,
                        "provider_config": provider_config,
                        "fallback_configs": fallback_configs,
                        "source": "cron",
                    },
                )
            data = response.json()
            if response.status_code >= 400 or data.get("error"):
                raise RuntimeError(data.get("error", {}).get("message") or response.text[:500])
            choice = data["choices"][0]
            full_content = choice["message"].get("content") or ""
            usage.update(data.get("usage") or {})
            resolved_model = data.get("model") or model_id
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        run.ended_at = _utcnow()
        assistant_message = None
        if error:
            run.status = "failed"
            run.error = error
            job.last_status = "failed"
            job.consecutive_errors += 1
        else:
            run.status = "succeeded"
            run.output = full_content
            run.model_id = resolved_model
            run.input_tokens = usage["input_tokens"]
            run.output_tokens = usage["output_tokens"]
            run.total_tokens = usage["total_tokens"]
            job.last_status = "succeeded"
            job.consecutive_errors = 0
            assistant_message = Message(
                conversation_id=conv.id,
                role="assistant",
                content=full_content,
                model_id=resolved_model,
                source="cron",
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                total_tokens=usage["total_tokens"],
            )
            db.add(assistant_message)
            conv.input_tokens += usage["input_tokens"]
            conv.output_tokens += usage["output_tokens"]
            conv.total_tokens += usage["total_tokens"]

        if job.schedule_kind == "once":
            job.enabled = False
        await _commit_scheduled_session_state(
            db,
            user_id=str(job.user_id),
            conversation_id=str(conv.id),
        )
        await emit_event(
            job.user_id,
            "cron.failed" if error else "cron.completed",
            {
                "job_id": job.id,
                "run_id": run.id,
                "conversation_id": conv.id,
                "error": error,
            },
            conv.id,
        )
        if assistant_message is not None:
            await emit_event(
                job.user_id,
                "message.created",
                {
                    "conversation_id": conv.id,
                    "message_id": assistant_message.id,
                    "role": "assistant",
                    "model_id": resolved_model,
                    "source": "cron",
                },
                conv.id,
            )


async def _execute_claude_scheduled_turn(
    db,
    *,
    job: ScheduledJob,
    conv: Conversation,
    scheduled_run: ScheduledJobRun,
    tools: list[str],
) -> None:
    """Execute unattended work through the Conversation's ClaudeEngine.

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
            engine_id="claude_code",
        ),
        user,
        db,
        source="cron",
        enabled_tools_override=tuple(tools),
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
