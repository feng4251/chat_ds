"""Lifecycle webhook management."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from auth import get_current_user
from database import get_db
from hooks import _url_allowed
from models import Conversation, EventHook
from schemas import EventHookCreate, EventHookUpdate

router = APIRouter(prefix="/api/hooks", tags=["hooks"])

ALLOWED_EVENTS = {
    "*",
    "session.created", "session.forked", "session.deleted",
    "message.created", "run.started", "run.completed", "run.failed",
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
    unknown = set(payload.events) - ALLOWED_EVENTS
    if unknown:
        raise HTTPException(400, f"Unknown hook events: {sorted(unknown)}")
    if not _url_allowed(payload.url):
        raise HTTPException(
            400,
            "Hook URL is not allowed. Use a public http(s) endpoint or enable private hook URLs.",
        )
    if payload.conversation_id:
        conv = (await db.execute(
            select(Conversation).where(
                Conversation.id == payload.conversation_id,
                Conversation.user_id == user.id,
            )
        )).scalar_one_or_none()
        if conv is None:
            raise HTTPException(404, "Conversation not found")
    hook = EventHook(
        user_id=user.id,
        conversation_id=payload.conversation_id,
        name=payload.name,
        events=json.dumps(list(dict.fromkeys(payload.events))),
        url=payload.url,
        secret=payload.secret,
        enabled=payload.enabled,
    )
    db.add(hook)
    await db.commit()
    await db.refresh(hook)
    return {"id": hook.id, "ok": True}


@router.patch("/{hook_id}")
async def update_hook(
    hook_id: str,
    payload: EventHookUpdate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    hook = (await db.execute(
        select(EventHook).where(
            EventHook.id == hook_id,
            EventHook.user_id == user.id,
        )
    )).scalar_one_or_none()
    if hook is None:
        raise HTTPException(404, "Hook not found")
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
    await db.commit()
    return {"id": hook.id, "ok": True}


@router.delete("/{hook_id}")
async def delete_hook(
    hook_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    hook = (await db.execute(
        select(EventHook).where(
            EventHook.id == hook_id,
            EventHook.user_id == user.id,
        )
    )).scalar_one_or_none()
    if hook is None:
        raise HTTPException(404, "Hook not found")
    await db.delete(hook)
    await db.commit()
    return {"ok": True}
