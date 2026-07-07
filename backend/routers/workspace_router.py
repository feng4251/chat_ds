"""Session-wise workspace, settings, goals, forks, and run audit APIs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import (
    AgentRun,
    Conversation,
    CustomModelConfig,
    Message,
    ScheduledJob,
    ScheduledJobRun,
    SkillPackage,
)
from schemas import (
    ConversationSettingsUpdate,
    GoalUpdate,
    WorkspaceFileWrite,
)
from workspace import (
    MAX_WORKSPACE_FILE_CHARS,
    atomic_write_text,
    build_workspace_context,
    clone_session_workspace,
    ensure_workspace,
    list_workspace_files,
    redact_trajectory_value,
    safe_workspace_path,
    serialize_json_list,
)
from hooks import emit_event

router = APIRouter(prefix="/api/conversations", tags=["workspace"])

DEFAULT_TOOLS = [
    "web_search", "web_extract", "execute_code",
    "read_file", "write_file", "patch_file", "search_files",
    "todo", "clarify", "memory",
    "skills_list", "skill_view", "skill_manage",
    "browser_navigate", "browser_snapshot", "browser_click",
    "browser_type", "browser_scroll", "browser_back",
    "session_search", "sessions_list", "sessions_history", "sessions_send",
    "sessions_fork", "session_status",
    "delegate_task", "cronjob", "get_goal", "create_goal", "update_goal",
    "image_generate", "vision_analyze",
    "mcp_server_list", "mcp_server_status",
]


async def _conversation(cid: str, user_id: str, db: AsyncSession) -> Conversation:
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == cid,
            Conversation.user_id == user_id,
        )
    )).scalar_one_or_none()
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    return conv


async def _model_exists(model_id: str, user_id: str, db: AsyncSession) -> bool:
    from routers.chat_router import BUILTIN
    if model_id in BUILTIN:
        return True
    return (await db.execute(
        select(CustomModelConfig.id).where(
            CustomModelConfig.user_id == user_id,
            CustomModelConfig.model_id == model_id,
        )
    )).scalar_one_or_none() is not None


@router.get("/{cid}/workspace")
async def workspace_files(
    cid: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    ensure_workspace(user.id, cid)
    return {
        "files": list_workspace_files(user.id, cid),
        "context_preview": build_workspace_context(user.id, cid),
    }


@router.get("/{cid}/workspace/file")
async def read_workspace_file(
    cid: str,
    path: str = Query(...),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    try:
        file_path = safe_workspace_path(user.id, cid, path, must_exist=True)
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not file_path.is_file():
        raise HTTPException(400, "Not a regular file")
    if file_path.stat().st_size > MAX_WORKSPACE_FILE_CHARS * 4:
        raise HTTPException(400, "File is too large to edit in the browser")
    return {
        "path": path,
        "content": file_path.read_text(encoding="utf-8", errors="replace"),
    }


@router.put("/{cid}/workspace/file")
async def write_workspace_file(
    cid: str,
    payload: WorkspaceFileWrite,
    path: str = Query(...),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    try:
        file_path = safe_workspace_path(user.id, cid, path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if Path(path).name.startswith(".env") and Path(path).name != ".env.example":
        raise HTTPException(400, "Secret-bearing .env files are not allowed")
    atomic_write_text(file_path, payload.content)
    return {"ok": True, "path": path, "size": len(payload.content)}


@router.delete("/{cid}/workspace/file")
async def delete_workspace_file(
    cid: str,
    path: str = Query(...),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    if Path(path).name in {"AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "MEMORY.md"}:
        raise HTTPException(400, "Bootstrap files can be emptied but not deleted")
    try:
        file_path = safe_workspace_path(user.id, cid, path, must_exist=True)
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    file_path.unlink()
    return {"ok": True}


@router.get("/{cid}/settings")
async def get_conversation_settings(
    cid: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    conv = await _conversation(cid, user.id, db)
    return {
        "model_id": conv.model_id,
        "enabled_tools": serialize_json_list(conv.enabled_tools, DEFAULT_TOOLS),
        "fallback_model_ids": serialize_json_list(conv.fallback_model_ids, []),
        "enabled_user_skills": serialize_json_list(conv.enabled_user_skills, []),
        "usage": {
            "input_tokens": conv.input_tokens,
            "output_tokens": conv.output_tokens,
            "total_tokens": conv.total_tokens,
        },
    }


@router.patch("/{cid}/settings")
async def update_conversation_settings(
    cid: str,
    payload: ConversationSettingsUpdate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    conv = await _conversation(cid, user.id, db)
    if payload.model_id is not None:
        if not await _model_exists(payload.model_id, user.id, db):
            raise HTTPException(400, f"Unknown model: {payload.model_id}")
        conv.model_id = payload.model_id
    if payload.enabled_tools is not None:
        unknown_tools = set(payload.enabled_tools) - set(DEFAULT_TOOLS)
        if unknown_tools:
            raise HTTPException(400, f"Unknown tools: {sorted(unknown_tools)}")
        conv.enabled_tools = json.dumps(list(dict.fromkeys(payload.enabled_tools)))
    if payload.fallback_model_ids is not None:
        for model_id in payload.fallback_model_ids:
            if not await _model_exists(model_id, user.id, db):
                raise HTTPException(400, f"Unknown fallback model: {model_id}")
        conv.fallback_model_ids = json.dumps(
            [m for m in dict.fromkeys(payload.fallback_model_ids) if m != conv.model_id]
        )
    if payload.enabled_user_skills is not None:
        existing_names = (await db.execute(
            select(SkillPackage.name).where(
                SkillPackage.user_id == user.id,
                SkillPackage.session_id.is_(None),
                SkillPackage.name.in_(payload.enabled_user_skills),
            )
        )).scalars().all()
        invalid = set(payload.enabled_user_skills) - set(existing_names)
        if invalid:
            raise HTTPException(400, f"Unknown user-level skills: {sorted(invalid)}")
        conv.enabled_user_skills = json.dumps(list(dict.fromkeys(payload.enabled_user_skills)))
    await db.commit()
    return await get_conversation_settings(cid, user, db)


@router.get("/{cid}/goal")
async def get_goal(
    cid: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    conv = await _conversation(cid, user.id, db)
    return {
        "objective": conv.goal_objective,
        "status": conv.goal_status,
        "note": conv.goal_note,
        "token_budget": conv.goal_token_budget,
        "tokens_used": max(0, conv.total_tokens - conv.goal_started_tokens),
    }


@router.put("/{cid}/goal")
async def update_goal(
    cid: str,
    payload: GoalUpdate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    conv = await _conversation(cid, user.id, db)
    if payload.objective is not None:
        next_objective = payload.objective.strip() or None
        if next_objective and next_objective != conv.goal_objective:
            conv.goal_started_tokens = conv.total_tokens
        conv.goal_objective = next_objective
        if conv.goal_objective and not payload.status:
            conv.goal_status = "active"
    if payload.status is not None:
        if not conv.goal_objective:
            raise HTTPException(400, "Cannot set goal status without an objective")
        conv.goal_status = payload.status
    if payload.note is not None:
        conv.goal_note = payload.note.strip() or None
    if payload.token_budget is not None:
        conv.goal_token_budget = payload.token_budget
    await db.commit()
    await emit_event(
        user.id, "goal.updated",
        {
            "conversation_id": cid,
            "status": conv.goal_status,
            "objective": conv.goal_objective,
        },
        cid,
    )
    return await get_goal(cid, user, db)


@router.delete("/{cid}/goal")
async def clear_goal(
    cid: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    conv = await _conversation(cid, user.id, db)
    conv.goal_objective = None
    conv.goal_status = None
    conv.goal_note = None
    conv.goal_token_budget = None
    conv.goal_started_tokens = conv.total_tokens
    await db.commit()
    await emit_event(
        user.id,
        "goal.updated",
        {"conversation_id": cid, "status": None, "objective": None},
        cid,
    )
    return {"ok": True}


@router.post("/{cid}/fork")
async def fork_conversation(
    cid: str,
    title: str | None = None,
    include_messages: bool = True,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    source = await _conversation(cid, user.id, db)
    fork = Conversation(
        user_id=user.id,
        title=title or ((source.title or "会话") + " · 分支"),
        model_id=source.model_id,
        enabled_tools=source.enabled_tools,
        fallback_model_ids=source.fallback_model_ids,
        goal_objective=source.goal_objective,
        goal_status="paused" if source.goal_objective else None,
        goal_note="Forked from another session." if source.goal_objective else None,
        goal_token_budget=source.goal_token_budget,
    )
    db.add(fork)
    await db.flush()

    if include_messages:
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
                input_tokens=message.input_tokens,
                output_tokens=message.output_tokens,
                total_tokens=message.total_tokens,
            ))
    await db.commit()
    clone_session_workspace(user.id, cid, fork.id)

    source_skills = (await db.execute(
        select(SkillPackage).where(
            SkillPackage.user_id == user.id,
            SkillPackage.session_id == cid,
        )
    )).scalars().all()
    source_skill_dir = Path("data/skills") / user.id / cid
    target_skill_dir = Path("data/skills") / user.id / fork.id
    if source_skill_dir.exists():
        shutil.copytree(source_skill_dir, target_skill_dir, dirs_exist_ok=True)
    for skill in source_skills:
        db.add(SkillPackage(
            user_id=user.id,
            session_id=fork.id,
            name=skill.name,
            description=skill.description,
            category=skill.category,
            version=skill.version,
        ))
    await db.commit()
    await emit_event(
        user.id, "session.forked",
        {"source_conversation_id": cid, "conversation_id": fork.id},
        fork.id,
    )
    return {"id": fork.id, "title": fork.title, "source_conversation_id": cid}


@router.get("/{cid}/runs")
async def list_runs(
    cid: str,
    limit: int = Query(50, ge=1, le=200),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    runs = (await db.execute(
        select(AgentRun)
        .where(AgentRun.conversation_id == cid, AgentRun.user_id == user.id)
        .order_by(desc(AgentRun.started_at))
        .limit(limit)
    )).scalars().all()
    return [{
        "id": run.id,
        "source": run.source,
        "requested_model_id": run.requested_model_id,
        "resolved_model_id": run.resolved_model_id,
        "status": run.status,
        "finish_reason": run.finish_reason,
        "error": run.error,
        "tool_events": json.loads(run.tool_events) if run.tool_events else [],
        "usage": {
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "total_tokens": run.total_tokens,
        },
        "started_at": str(run.started_at),
        "ended_at": str(run.ended_at) if run.ended_at else None,
    } for run in runs]


@router.get("/{cid}/trajectory")
async def export_trajectory(
    cid: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    conv = await _conversation(cid, user.id, db)
    messages = (await db.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.created_at)
    )).scalars().all()
    runs = await list_runs(cid, 200, user, db)
    jobs = (await db.execute(
        select(ScheduledJob)
        .where(ScheduledJob.conversation_id == cid, ScheduledJob.user_id == user.id)
        .order_by(desc(ScheduledJob.created_at))
    )).scalars().all()
    scheduled_runs = (await db.execute(
        select(ScheduledJobRun)
        .where(ScheduledJobRun.conversation_id == cid)
        .order_by(desc(ScheduledJobRun.started_at))
        .limit(500)
    )).scalars().all()
    payload = {
        "schema": "chat-ds-session-trajectory-v1",
        "conversation": {
            "id": conv.id,
            "title": conv.title,
            "model_id": conv.model_id,
            "created_at": str(conv.created_at),
            "updated_at": str(conv.updated_at),
        },
        "settings": {
            "enabled_tools": serialize_json_list(conv.enabled_tools, DEFAULT_TOOLS),
            "fallback_model_ids": serialize_json_list(conv.fallback_model_ids, []),
        },
        "goal": await get_goal(cid, user, db),
        "messages": [{
            "id": m.id,
            "role": m.role,
            "source": m.source,
            "content": redact_trajectory_value(m.content),
            "model_id": m.model_id,
            "usage": {
                "input_tokens": m.input_tokens,
                "output_tokens": m.output_tokens,
                "total_tokens": m.total_tokens,
            },
            "created_at": str(m.created_at),
        } for m in messages],
        "runs": redact_trajectory_value(runs),
        "scheduled_jobs": [{
            "id": job.id,
            "name": job.name,
            "prompt": redact_trajectory_value(job.prompt),
            "schedule_kind": job.schedule_kind,
            "schedule_value": job.schedule_value,
            "timezone": job.timezone,
            "model_id": job.model_id,
            "enabled": job.enabled,
            "last_status": job.last_status,
            "created_at": str(job.created_at),
        } for job in jobs],
        "scheduled_runs": redact_trajectory_value([{
            "id": run.id,
            "job_id": run.job_id,
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
        } for run in scheduled_runs]),
    }
    return JSONResponse(
        payload,
        headers={
            "Content-Disposition": f'attachment; filename="trajectory-{cid}.json"'
        },
    )
