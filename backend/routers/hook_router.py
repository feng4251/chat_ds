"""Lifecycle webhook management."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from auth import get_current_user
from database import get_db
from hooks import _url_allowed
from models import EventHook
from schemas import EventHookCreate, EventHookUpdate
from session_lifecycle import session_control_plane_mutation

router = APIRouter(prefix="/api/hooks", tags=["hooks"])

ALLOWED_EVENTS = {
    "*",
    "session.created", "session.forked", "session.deleted",
    "message.created", "run.started", "run.completed", "run.failed",
    "run.cancelled",
    "goal.updated", "cron.created", "cron.started", "cron.completed", "cron.failed",
}


@router.get("")
async def list_hooks(user=Depends(get_current_user), db=Depends(get_db)):
    hooks = (await db.execute(
        select(EventHook).where(EventHook.user_id == user.id)
    )).scalars().all()
    return [{
        "id": hook.id,
        "name": hook.name,
        "events": json.loads(hook.events),
        "url": hook.url,
        "conversation_id": hook.conversation_id,
        "enabled": hook.enabled,
        "has_secret": bool(hook.secret),
        "created_at": str(hook.created_at),
    } for hook in hooks]


@router.post("")
async def create_hook(
    payload: EventHookCreate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    if payload.conversation_id:
        async with session_control_plane_mutation(
            user.id,
            payload.conversation_id,
        ) as (mutation_db, _conversation):
            return await _create_hook_for_user(
                payload,
                user.id,
                mutation_db,
            )
    return await _create_hook_for_user(payload, user.id, db)


async def _create_hook_for_user(
    payload: EventHookCreate,
    user_id: str,
    db,
) -> dict:
    unknown = set(payload.events) - ALLOWED_EVENTS
    if unknown:
        raise HTTPException(400, f"Unknown hook events: {sorted(unknown)}")
    if not _url_allowed(payload.url):
        raise HTTPException(
            400,
            "Hook URL is not allowed. Use a public http(s) endpoint or enable private hook URLs.",
        )
    hook = EventHook(
        user_id=user_id,
        conversation_id=payload.conversation_id,
        name=payload.name,
        events=json.dumps(list(dict.fromkeys(payload.events))),
        url=payload.url,
        secret=payload.secret,
        enabled=payload.enabled,
    )
    db.add(hook)
    if payload.conversation_id:
        from workspace import require_session_workspace_active

        require_session_workspace_active(
            user_id,
            payload.conversation_id,
        )
    await db.commit()
    await db.refresh(hook)
    return {"id": hook.id, "ok": True}


async def _owned_hook(hook_id: str, user_id: str, db) -> EventHook:
    hook = (await db.execute(
        select(EventHook).where(
            EventHook.id == hook_id,
            EventHook.user_id == user_id,
        )
    )).scalar_one_or_none()
    if hook is None:
        raise HTTPException(404, "Hook not found")
    return hook


async def _apply_hook_update(
    hook: EventHook,
    payload: EventHookUpdate,
    user_id: str,
    db,
) -> dict:
    if payload.events is not None:
        unknown = set(payload.events) - ALLOWED_EVENTS
        if unknown:
            raise HTTPException(400, f"Unknown hook events: {sorted(unknown)}")
        hook.events = json.dumps(list(dict.fromkeys(payload.events)))
    if payload.url is not None:
        if not _url_allowed(payload.url):
            raise HTTPException(400, "Hook URL is not allowed")
        hook.url = payload.url
    if payload.name is not None:
        hook.name = payload.name
    if payload.secret is not None:
        hook.secret = payload.secret or None
    if payload.enabled is not None:
        hook.enabled = payload.enabled
    if hook.conversation_id:
        from workspace import require_session_workspace_active

        require_session_workspace_active(
            user_id,
            hook.conversation_id,
        )
    await db.commit()
    return {"id": hook.id, "ok": True}


@router.patch("/{hook_id}")
async def update_hook(
    hook_id: str,
    payload: EventHookUpdate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    observed = await _owned_hook(hook_id, user.id, db)
    if not observed.conversation_id:
        return await _apply_hook_update(
            observed,
            payload,
            user.id,
            db,
        )
    conversation_id = str(observed.conversation_id)
    await db.rollback()
    async with session_control_plane_mutation(
        user.id,
        conversation_id,
    ) as (mutation_db, _conversation):
        current = await _owned_hook(hook_id, user.id, mutation_db)
        if current.conversation_id != conversation_id:
            raise HTTPException(
                409,
                "Hook session changed during mutation",
            )
        return await _apply_hook_update(
            current,
            payload,
            user.id,
            mutation_db,
        )


@router.delete("/{hook_id}")
async def delete_hook(
    hook_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    observed = await _owned_hook(hook_id, user.id, db)
    if not observed.conversation_id:
        await db.delete(observed)
        await db.commit()
        return {"ok": True}
    conversation_id = str(observed.conversation_id)
    await db.rollback()
    async with session_control_plane_mutation(
        user.id,
        conversation_id,
    ) as (mutation_db, _conversation):
        current = await _owned_hook(hook_id, user.id, mutation_db)
        if current.conversation_id != conversation_id:
            raise HTTPException(
                409,
                "Hook session changed during mutation",
            )
        await mutation_db.delete(current)
        from workspace import require_session_workspace_active

        require_session_workspace_active(user.id, conversation_id)
        await mutation_db.commit()
    return {"ok": True}
