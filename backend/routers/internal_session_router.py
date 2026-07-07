"""Trusted control-plane endpoints used by session-scoped harness tools."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from config import settings
from database import get_db
from hooks import emit_event
from models import Conversation, Message, SkillPackage
from workspace import serialize_json_list

router = APIRouter(prefix="/internal/sessions", tags=["internal"])


def _check(token: str | None) -> None:
    if token != settings.internal_api_token:
        raise HTTPException(403, "Invalid internal token")


async def _owned(cid: str, user_id: str, db) -> Conversation:
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == cid,
            Conversation.user_id == user_id,
        )
    )).scalar_one_or_none()
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.get("")
async def list_sessions(
    user_id: str = Query(...),
    current_session_id: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    search: str | None = Query(None),
    x_internal_token: str | None = Header(None),
    db=Depends(get_db),
):
    _check(x_internal_token)
    stmt = select(Conversation).where(Conversation.user_id == user_id)
    if search:
        stmt = stmt.where(Conversation.title.ilike(f"%{search}%"))
    sessions = (await db.execute(
        stmt.order_by(desc(Conversation.updated_at)).limit(limit)
    )).scalars().all()
    output = []
    for conv in sessions:
        last = (await db.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(desc(Message.created_at))
            .limit(1)
        )).scalar_one_or_none()
        count = (await db.execute(
            select(func.count(Message.id)).where(Message.conversation_id == conv.id)
        )).scalar() or 0
        output.append({
            "id": conv.id,
            "title": conv.title,
            "model_id": conv.model_id,
            "is_current": conv.id == current_session_id,
            "message_count": count,
            "last_message": (last.content or "")[:300] if last else "",
            "updated_at": str(conv.updated_at),
            "usage": {
                "input_tokens": conv.input_tokens,
                "output_tokens": conv.output_tokens,
                "total_tokens": conv.total_tokens,
            },
            "goal_status": conv.goal_status,
        })
    return {"sessions": output}


@router.get("/{cid}/history")
async def session_history(
    cid: str,
    user_id: str = Query(...),
    limit: int = Query(30, ge=1, le=200),
    include_tools: bool = Query(False),
    x_internal_token: str | None = Header(None),
    db=Depends(get_db),
):
    _check(x_internal_token)
    conv = await _owned(cid, user_id, db)
    rows = (await db.execute(
        select(Message)
        .where(Message.conversation_id == cid)
        .order_by(desc(Message.created_at))
        .limit(limit)
    )).scalars().all()
    messages = []
    for row in reversed(rows):
        item = {
            "role": row.role,
            "content": (row.content or "")[:12_000],
            "source": row.source,
            "model_id": row.model_id,
            "created_at": str(row.created_at),
        }
        if include_tools:
            item["tool_progress"] = row.tool_progress
        messages.append(item)
    return {
        "session": {
            "id": conv.id,
            "title": conv.title,
            "model_id": conv.model_id,
        },
        "messages": messages,
    }


@router.get("/{cid}/status")
async def session_status(
    cid: str,
    user_id: str = Query(...),
    x_internal_token: str | None = Header(None),
    db=Depends(get_db),
):
    _check(x_internal_token)
    conv = await _owned(cid, user_id, db)
    count = (await db.execute(
        select(func.count(Message.id)).where(Message.conversation_id == cid)
    )).scalar() or 0
    return {
        "id": conv.id,
        "title": conv.title,
        "model_id": conv.model_id,
        "message_count": count,
        "enabled_tools": serialize_json_list(conv.enabled_tools, []),
        "fallback_model_ids": serialize_json_list(conv.fallback_model_ids, []),
        "goal": {
            "objective": conv.goal_objective,
            "status": conv.goal_status,
            "note": conv.goal_note,
            "token_budget": conv.goal_token_budget,
        },
        "usage": {
            "input_tokens": conv.input_tokens,
            "output_tokens": conv.output_tokens,
            "total_tokens": conv.total_tokens,
        },
        "updated_at": str(conv.updated_at),
    }


class InternalSessionMessage(BaseModel):
    content: str = Field(min_length=1, max_length=40_000)


@router.post("/{cid}/messages")
async def send_session_message(
    cid: str,
    payload: InternalSessionMessage,
    user_id: str = Query(...),
    source_session_id: str | None = Query(None),
    x_internal_token: str | None = Header(None),
    db=Depends(get_db),
):
    _check(x_internal_token)
    await _owned(cid, user_id, db)
    source_note = (
        f"[Message from session {source_session_id}]\n"
        if source_session_id else "[Cross-session message]\n"
    )
    message = Message(
        conversation_id=cid,
        role="user",
        content=source_note + payload.content.strip(),
        source="session",
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    await emit_event(
        user_id,
        "message.created",
        {
            "conversation_id": cid,
            "message_id": message.id,
            "role": "user",
            "source": "session",
        },
        cid,
    )
    return {
        "ok": True,
        "message_id": message.id,
        "target_session_id": cid,
        "delivery": "queued_in_history",
    }


class InternalFork(BaseModel):
    title: str | None = Field(default=None, max_length=256)
    include_messages: bool = True


@router.post("/{cid}/fork")
async def fork_session(
    cid: str,
    payload: InternalFork,
    user_id: str = Query(...),
    x_internal_token: str | None = Header(None),
    db=Depends(get_db),
):
    _check(x_internal_token)
    source = await _owned(cid, user_id, db)
    fork = Conversation(
        user_id=user_id,
        title=payload.title or ((source.title or "Session") + " · fork"),
        model_id=source.model_id,
        enabled_tools=source.enabled_tools,
        fallback_model_ids=source.fallback_model_ids,
    )
    db.add(fork)
    await db.flush()
    if payload.include_messages:
        messages = (await db.execute(
            select(Message).where(Message.conversation_id == cid).order_by(Message.created_at)
        )).scalars().all()
        for message in messages:
            db.add(Message(
                conversation_id=fork.id,
                role=message.role,
                content=message.content,
                reasoning=message.reasoning,
                tool_progress=message.tool_progress,
                image_urls=message.image_urls,
                model_id=message.model_id,
                source="fork",
            ))
    await db.commit()
    from workspace import clone_session_workspace
    clone_session_workspace(user_id, cid, fork.id)
    source_skills = (await db.execute(
        select(SkillPackage).where(
            SkillPackage.user_id == user_id,
            SkillPackage.session_id == cid,
        )
    )).scalars().all()
    source_skill_dir = Path("data/skills") / user_id / cid
    target_skill_dir = Path("data/skills") / user_id / fork.id
    if source_skill_dir.exists():
        shutil.copytree(source_skill_dir, target_skill_dir, dirs_exist_ok=True)
    for skill in source_skills:
        if not (source_skill_dir / skill.name / "SKILL.md").is_file():
            continue
        db.add(SkillPackage(
            user_id=user_id,
            session_id=fork.id,
            name=skill.name,
            description=skill.description,
            category=skill.category,
            version=skill.version,
        ))
    await db.commit()
    return {"id": fork.id, "title": fork.title}


@router.get("/{cid}/goal")
async def internal_get_goal(
    cid: str,
    user_id: str = Query(...),
    x_internal_token: str | None = Header(None),
    db=Depends(get_db),
):
    _check(x_internal_token)
    conv = await _owned(cid, user_id, db)
    return {
        "objective": conv.goal_objective,
        "status": conv.goal_status,
        "note": conv.goal_note,
        "token_budget": conv.goal_token_budget,
        "tokens_used": max(0, conv.total_tokens - conv.goal_started_tokens),
    }


class InternalGoalUpdate(BaseModel):
    action: str
    objective: str | None = None
    note: str | None = None
    token_budget: int | None = None


@router.post("/{cid}/goal")
async def internal_update_goal(
    cid: str,
    payload: InternalGoalUpdate,
    user_id: str = Query(...),
    x_internal_token: str | None = Header(None),
    db=Depends(get_db),
):
    _check(x_internal_token)
    conv = await _owned(cid, user_id, db)
    if payload.action == "create":
        if conv.goal_objective and conv.goal_status != "complete":
            raise HTTPException(409, "An unfinished goal already exists")
        if not payload.objective or not payload.objective.strip():
            raise HTTPException(400, "objective is required")
        conv.goal_objective = payload.objective.strip()
        conv.goal_status = "active"
        conv.goal_note = payload.note
        conv.goal_token_budget = payload.token_budget
        conv.goal_started_tokens = conv.total_tokens
    elif payload.action in {"complete", "blocked", "pause", "budget_limited"}:
        if not conv.goal_objective:
            raise HTTPException(404, "No goal exists")
        conv.goal_status = "paused" if payload.action == "pause" else payload.action
        conv.goal_note = payload.note
    else:
        raise HTTPException(
            400,
            "Action must be create, complete, blocked, pause, or budget_limited",
        )
    await db.commit()
    return {
        "objective": conv.goal_objective,
        "status": conv.goal_status,
        "note": conv.goal_note,
        "token_budget": conv.goal_token_budget,
        "tokens_used": max(0, conv.total_tokens - conv.goal_started_tokens),
    }
