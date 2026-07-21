"""Persistent session-scoped scheduler for agent jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
    canonical_agent_model_id,
    filter_agentic_fallback_model_ids,
)
from models import (
    Conversation,
    CustomModelConfig,
    Message,
    ScheduledJob,
    ScheduledJobRun,
)
from workspace import ensure_workspace, serialize_json_list

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
        "agentic_auxiliary_only": config.get(
            "agentic_auxiliary_only", False
        ),
        "supports_thinking_toggle": config.get(
            "supports_thinking_toggle", False
        ),
        "thinking_enabled_by_default": config.get(
            "thinking_enabled_by_default", True
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
        "extra_headers": extra_headers,
    }


async def execute_job(job_id: str, *, force: bool = False) -> None:
    async with async_session() as db:
        job = (await db.execute(
            select(ScheduledJob).where(ScheduledJob.id == job_id)
        )).scalar_one_or_none()
        if job is None or (not job.enabled and not force):
            return
        run = ScheduledJobRun(
            job_id=job.id,
            conversation_id=job.conversation_id,
            status="running",
            model_id=job.model_id,
        )
        job.last_run_at = _utcnow()
        job.next_run_at = next_run_for(job, job.last_run_at)
        db.add(run)
        await db.commit()
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
            await db.commit()
            await emit_event(
                job.user_id,
                "cron.failed",
                {"job_id": job.id, "run_id": run.id, "error": error},
                job.conversation_id,
            )
            return

        conv = None
        if job.conversation_id:
            conv = (await db.execute(
                select(Conversation).where(
                    Conversation.id == job.conversation_id,
                    Conversation.user_id == job.user_id,
                )
            )).scalar_one_or_none()
        if conv is None:
            conv = Conversation(
                user_id=job.user_id,
                title=f"定时任务 · {job.name}",
                model_id=job.model_id or "deepseek_v4_pro",
            )
            db.add(conv)
            await db.commit()
            await db.refresh(conv)
            job.conversation_id = conv.id
            run.conversation_id = conv.id
            await db.commit()

        ensure_workspace(job.user_id, conv.id)
        model_id = canonical_agent_model_id(
            job.model_id or conv.model_id or "deepseek_v4_pro"
        )
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
        tools = serialize_json_list(job.enabled_tools, [])
        if not tools:
            from native_tools import UNATTENDED_DEFAULT_NATIVE_TOOLS
            tools = list(UNATTENDED_DEFAULT_NATIVE_TOOLS)

        user_message = Message(
            conversation_id=conv.id,
            role="user",
            content=job.prompt,
            model_id=model_id,
            source="cron",
        )
        db.add(user_message)
        await db.commit()
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
            async with httpx.AsyncClient(timeout=900) as client:
                response = await client.post(
                    f"{settings.harness_url}/v1/chat/completions",
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
        await db.commit()
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


async def scheduler_loop() -> None:
    """Claim and execute due jobs. A single backend process owns this loop."""
    while True:
        try:
            async with async_session() as db:
                due = (await db.execute(
                    select(ScheduledJob.id).where(
                        ScheduledJob.enabled.is_(True),
                        ScheduledJob.next_run_at.is_not(None),
                        ScheduledJob.next_run_at <= _utcnow(),
                    ).limit(20)
                )).scalars().all()
            for job_id in due:
                asyncio.create_task(execute_job(job_id))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(max(5, settings.scheduler_poll_seconds))
