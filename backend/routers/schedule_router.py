"""Scheduled task CRUD and run history."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select, desc

from auth import get_current_user
from config import settings
from database import get_db
from hooks import emit_event
from models import CustomModelConfig, ScheduledJob, ScheduledJobRun
from session_lifecycle import session_control_plane_mutation
from scheduler import (
    enqueue_job_execution,
    next_run_for,
    parse_schedule,
    scan_cron_prompt,
)
from schemas import ScheduledJobCreate, ScheduledJobUpdate

router = APIRouter(prefix="/api/schedules", tags=["schedules"])
internal_router = APIRouter(prefix="/internal", tags=["internal"])


def _job_dict(job: ScheduledJob) -> dict:
    return {
        "id": job.id,
        "conversation_id": job.conversation_id,
        "name": job.name,
        "prompt": job.prompt,
        "schedule_kind": job.schedule_kind,
        "schedule_value": job.schedule_value,
        "timezone": job.timezone,
        "model_id": job.model_id,
        "enabled_tools": json.loads(job.enabled_tools) if job.enabled_tools else [],
        "enabled": job.enabled,
        "delete_after_run": job.delete_after_run,
        "next_run_at": str(job.next_run_at) if job.next_run_at else None,
        "last_run_at": str(job.last_run_at) if job.last_run_at else None,
        "last_status": job.last_status,
        "consecutive_errors": job.consecutive_errors,
        "created_at": str(job.created_at),
    }


async def _validate_model_id(model_id: str | None, user_id: str, db) -> None:
    if not model_id:
        return
    from routers.chat_router import BUILTIN
    if model_id in BUILTIN:
        return
    exists = (await db.execute(
        select(CustomModelConfig.id).where(
            CustomModelConfig.user_id == user_id,
            CustomModelConfig.model_id == model_id,
        )
    )).scalar_one_or_none()
    if exists is None:
        raise HTTPException(400, f"Unknown model: {model_id}")


def _validate_enabled_tools(enabled_tools: list[str] | None) -> None:
    if enabled_tools is None:
        return
    from native_tools import DEFAULT_NATIVE_TOOL_SET
    unknown = set(enabled_tools) - DEFAULT_NATIVE_TOOL_SET
    if unknown:
        raise HTTPException(400, f"Unknown tools: {sorted(unknown)}")


async def _create_for_user_in_session(
    payload: ScheduledJobCreate,
    user_id: str,
    db,
):
    await _validate_model_id(payload.model_id, user_id, db)
    _validate_enabled_tools(payload.enabled_tools)
    try:
        kind, value, next_run = parse_schedule(payload.schedule, payload.timezone)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    threat = scan_cron_prompt(payload.prompt)
    if threat:
        raise HTTPException(
            400,
            f"Unsafe unattended prompt blocked by security rule: {threat}",
        )
    job = ScheduledJob(
        user_id=user_id,
        conversation_id=payload.conversation_id,
        name=payload.name,
        prompt=payload.prompt,
        schedule_kind=kind,
        schedule_value=value,
        timezone=payload.timezone,
        model_id=payload.model_id,
        enabled_tools=json.dumps(payload.enabled_tools) if payload.enabled_tools else None,
        delete_after_run=payload.delete_after_run,
        next_run_at=next_run,
    )
    db.add(job)
    if payload.conversation_id:
        from workspace import require_session_workspace_active

        require_session_workspace_active(
            user_id,
            payload.conversation_id,
        )
    await db.commit()
    await db.refresh(job)
    await emit_event(user_id, "cron.created", {"job_id": job.id}, payload.conversation_id)
    return _job_dict(job)


async def _create_for_user(
    payload: ScheduledJobCreate,
    user_id: str,
    db,
    *,
    source_session_id: str | None = None,
):
    if not payload.conversation_id:
        return await _create_for_user_in_session(payload, user_id, db)
    async with session_control_plane_mutation(
        user_id,
        payload.conversation_id,
        source_session_id=source_session_id,
    ) as (mutation_db, _conversation):
        return await _create_for_user_in_session(
            payload,
            user_id,
            mutation_db,
        )


async def _owned_job(job_id: str, user_id: str, db) -> ScheduledJob:
    job = (await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.id == job_id,
            ScheduledJob.user_id == user_id,
        )
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "Scheduled job not found")
    return job


async def _apply_job_update(
    job: ScheduledJob,
    payload: ScheduledJobUpdate,
    user_id: str,
    db,
) -> dict:
    await _validate_model_id(payload.model_id, user_id, db)
    _validate_enabled_tools(payload.enabled_tools)
    for field in (
        "name",
        "prompt",
        "timezone",
        "model_id",
        "enabled",
        "delete_after_run",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(job, field, value)
    if payload.enabled_tools is not None:
        job.enabled_tools = json.dumps(payload.enabled_tools)
    if payload.prompt is not None:
        threat = scan_cron_prompt(payload.prompt)
        if threat:
            raise HTTPException(
                400,
                f"Unsafe unattended prompt blocked by security rule: {threat}",
            )
    if payload.schedule is not None:
        try:
            kind, value, next_run = parse_schedule(
                payload.schedule,
                payload.timezone or job.timezone,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        job.schedule_kind = kind
        job.schedule_value = value
        job.next_run_at = next_run
    elif payload.timezone is not None and job.schedule_kind == "cron":
        _, _, job.next_run_at = parse_schedule(
            job.schedule_value,
            job.timezone,
        )
    elif (
        payload.enabled is True
        and job.next_run_at is None
        and job.schedule_kind != "once"
    ):
        job.next_run_at = next_run_for(job)
    if job.conversation_id:
        from workspace import require_session_workspace_active

        require_session_workspace_active(
            user_id,
            job.conversation_id,
        )
    await db.commit()
    return _job_dict(job)


async def _update_for_user(
    job_id: str,
    payload: ScheduledJobUpdate,
    user_id: str,
    db,
    *,
    source_session_id: str | None = None,
) -> dict:
    observed = await _owned_job(job_id, user_id, db)
    if not observed.conversation_id:
        return await _apply_job_update(observed, payload, user_id, db)
    conversation_id = str(observed.conversation_id)
    # Release the request-session read transaction before the authoritative
    # post-lock write session is opened (important for SQLite deployments).
    await db.rollback()
    async with session_control_plane_mutation(
        user_id,
        conversation_id,
        source_session_id=source_session_id,
    ) as (mutation_db, _conversation):
        current = await _owned_job(job_id, user_id, mutation_db)
        if current.conversation_id != conversation_id:
            raise HTTPException(
                409,
                "Scheduled job session changed during mutation",
            )
        return await _apply_job_update(
            current,
            payload,
            user_id,
            mutation_db,
        )


async def _delete_for_user(
    job_id: str,
    user_id: str,
    db,
    *,
    source_session_id: str | None = None,
) -> dict:
    observed = await _owned_job(job_id, user_id, db)
    if not observed.conversation_id:
        await db.delete(observed)
        await db.commit()
        return {"ok": True}
    conversation_id = str(observed.conversation_id)
    await db.rollback()
    async with session_control_plane_mutation(
        user_id,
        conversation_id,
        source_session_id=source_session_id,
    ) as (mutation_db, _conversation):
        current = await _owned_job(job_id, user_id, mutation_db)
        if current.conversation_id != conversation_id:
            raise HTTPException(
                409,
                "Scheduled job session changed during mutation",
            )
        await mutation_db.delete(current)
        from workspace import require_session_workspace_active

        require_session_workspace_active(
            user_id,
            conversation_id,
        )
        await mutation_db.commit()
    return {"ok": True}


async def _trigger_for_user(
    job_id: str,
    user_id: str,
    db,
    *,
    source_session_id: str | None = None,
) -> dict:
    observed = await _owned_job(job_id, user_id, db)
    if not observed.conversation_id:
        _task, started = enqueue_job_execution(observed.id, force=True)
    else:
        conversation_id = str(observed.conversation_id)
        await db.rollback()
        async with session_control_plane_mutation(
            user_id,
            conversation_id,
            source_session_id=source_session_id,
        ) as (mutation_db, _conversation):
            current = await _owned_job(job_id, user_id, mutation_db)
            if current.conversation_id != conversation_id:
                raise HTTPException(
                    409,
                    "Scheduled job session changed during mutation",
                )
            _task, started = enqueue_job_execution(
                current.id,
                force=True,
            )
    return {
        "ok": True,
        "status": "queued" if started else "already_running",
    }


@router.get("")
async def list_jobs(
    conversation_id: str | None = Query(None),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    stmt = select(ScheduledJob).where(ScheduledJob.user_id == user.id)
    if conversation_id:
        stmt = stmt.where(ScheduledJob.conversation_id == conversation_id)
    jobs = (await db.execute(stmt.order_by(desc(ScheduledJob.created_at)))).scalars().all()
    return [_job_dict(job) for job in jobs]


@router.post("")
async def create_job(
    payload: ScheduledJobCreate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    return await _create_for_user(payload, user.id, db)


@router.patch("/{job_id}")
async def update_job(
    job_id: str,
    payload: ScheduledJobUpdate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    return await _update_for_user(job_id, payload, user.id, db)


@router.delete("/{job_id}")
async def delete_job(
    job_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    return await _delete_for_user(job_id, user.id, db)


@router.post("/{job_id}/run")
async def trigger_job(
    job_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    return await _trigger_for_user(job_id, user.id, db)


@router.get("/{job_id}/runs")
async def job_runs(
    job_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    job = (await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.id == job_id,
            ScheduledJob.user_id == user.id,
        )
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "Scheduled job not found")
    runs = (await db.execute(
        select(ScheduledJobRun)
        .where(ScheduledJobRun.job_id == job_id)
        .order_by(desc(ScheduledJobRun.started_at))
        .limit(100)
    )).scalars().all()
    return [{
        "id": run.id,
        "status": run.status,
        "output": run.output,
        "error": run.error,
        "model_id": run.model_id,
        "usage": {
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "total_tokens": run.total_tokens,
        },
        "started_at": str(run.started_at),
        "ended_at": str(run.ended_at) if run.ended_at else None,
    } for run in runs]


def _check_internal(token: str | None) -> None:
    if token != settings.internal_api_token:
        raise HTTPException(403, "Invalid internal token")


@internal_router.post("/schedules")
async def internal_create_schedule(
    payload: ScheduledJobCreate,
    x_internal_token: str | None = Header(None),
    user_id: str = Query(...),
    source_session_id: str | None = Query(None),
    db=Depends(get_db),
):
    _check_internal(x_internal_token)
    return await _create_for_user(
        payload,
        user_id,
        db,
        source_session_id=source_session_id,
    )


@internal_router.get("/schedules")
async def internal_list_schedules(
    x_internal_token: str | None = Header(None),
    user_id: str = Query(...),
    conversation_id: str | None = Query(None),
    db=Depends(get_db),
):
    _check_internal(x_internal_token)
    stmt = select(ScheduledJob).where(ScheduledJob.user_id == user_id)
    if conversation_id:
        stmt = stmt.where(ScheduledJob.conversation_id == conversation_id)
    jobs = (await db.execute(stmt.order_by(desc(ScheduledJob.created_at)))).scalars().all()
    return [_job_dict(job) for job in jobs]


@internal_router.patch("/schedules/{job_id}")
async def internal_update_schedule(
    job_id: str,
    payload: ScheduledJobUpdate,
    x_internal_token: str | None = Header(None),
    user_id: str = Query(...),
    source_session_id: str | None = Query(None),
    db=Depends(get_db),
):
    _check_internal(x_internal_token)
    return await _update_for_user(
        job_id,
        payload,
        user_id,
        db,
        source_session_id=source_session_id,
    )


@internal_router.delete("/schedules/{job_id}")
async def internal_delete_schedule(
    job_id: str,
    x_internal_token: str | None = Header(None),
    user_id: str = Query(...),
    source_session_id: str | None = Query(None),
    db=Depends(get_db),
):
    _check_internal(x_internal_token)
    return await _delete_for_user(
        job_id,
        user_id,
        db,
        source_session_id=source_session_id,
    )


@internal_router.post("/schedules/{job_id}/run")
async def internal_trigger_schedule(
    job_id: str,
    x_internal_token: str | None = Header(None),
    user_id: str = Query(...),
    source_session_id: str | None = Query(None),
    db=Depends(get_db),
):
    _check_internal(x_internal_token)
    return await _trigger_for_user(
        job_id,
        user_id,
        db,
        source_session_id=source_session_id,
    )
