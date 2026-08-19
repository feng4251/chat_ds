"""Session-wise workspace, settings, goals, forks, and run audit APIs."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import uuid
from collections import defaultdict
from contextlib import AsyncExitStack
from pathlib import Path

import workspace as workspace_store
from config import settings
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import (
    AgentEngineRawEvent,
    AgentEngineSession,
    AgentRun,
    AgentRunEvent,
    TurnActivityEvent,
    Artifact,
    Conversation,
    CustomModelConfig,
    Message,
    ScheduledJob,
    ScheduledJobRun,
    SkillPackage,
    TaskItem,
)
from schemas import (
    ApprovalDecision,
    ConversationSettingsUpdate,
    GoalUpdate,
    WorkspaceFileWrite,
)
from workspace import (
    MAX_WORKSPACE_FILE_CHARS,
    atomic_write_text,
    build_workspace_context,
    ensure_workspace_async,
    list_workspace_files,
    redact_trajectory_value,
    run_session_workspace_mutation_async,
    safe_workspace_path_in_root,
    serialize_json_list,
    workspace_file_metadata,
)
from hooks import emit_event
from native_tools import DEFAULT_NATIVE_TOOL_SET, DEFAULT_NATIVE_TOOLS
from model_routing import (
    DEFAULT_AGENT_MODEL_ID,
    canonical_agent_model_id,
    filter_agentic_fallback_model_ids,
)
from workspace_lock import (
    WORKSPACE_MUTATION_LOCK_FILENAME,
    run_sync_cancellation_safe,
)

router = APIRouter(prefix="/api/conversations", tags=["workspace"])


async def _conversation(cid: str, user_id: str, db: AsyncSession) -> Conversation:
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == cid,
            Conversation.user_id == user_id,
        )
    )).scalar_one_or_none()
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    workspace_store.require_session_workspace_active(user_id, cid)
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


async def _engine_options_for_user(
    *,
    current_model_id: str,
    user,
    db: AsyncSession,
) -> list[dict]:
    """Return non-secret engine/model compatibility for safe UI switching.

    Compatibility is derived from the same provider resolver used at Turn
    dispatch.  The browser therefore never guesses that a Legacy model can be
    sent through a deployment-owned Claude provider profile.
    """

    from routers.chat_router import (
        BUILTIN,
        claude_code_model_compatible,
        deepseek_harness_model_compatible,
        resolve_model_config,
    )

    custom_ids = list((await db.execute(
        select(CustomModelConfig.model_id).where(
            CustomModelConfig.user_id == user.id,
        ).order_by(CustomModelConfig.model_id)
    )).scalars().all())
    model_ids = list(dict.fromkeys([
        *(
            canonical_agent_model_id(model_id)
            for model_id in BUILTIN
            if model_id != "AgentModel"
        ),
        *custom_ids,
    ]))
    claude_model_ids: list[str] = []
    deepseek_model_ids: list[str] = []
    for model_id in model_ids:
        try:
            provider = await resolve_model_config(model_id, user, db)
        except HTTPException:
            continue
        if claude_code_model_compatible(provider):
            claude_model_ids.append(model_id)
        if deepseek_harness_model_compatible(provider):
            deepseek_model_ids.append(model_id)

    canonical_current = canonical_agent_model_id(current_model_id)
    legacy_default = (
        canonical_current
        if canonical_current in model_ids
        else DEFAULT_AGENT_MODEL_ID
    )
    options = [{
        "id": "legacy",
        "name": "ChatDS Legacy Harness",
        "available": settings.legacy_engine_new_runs_enabled,
        "unavailable_reason": (
            None
            if settings.legacy_engine_new_runs_enabled
            else "Legacy Harness is retained for history only"
        ),
        "compatible_model_ids": model_ids,
        "default_model_id": legacy_default,
        "capabilities": ["skills", "multi_agent", "mcp", "sandbox"],
    }]
    if settings.claude_code_engine_enabled:
        claude_default = (
            canonical_current
            if canonical_current in claude_model_ids
            else DEFAULT_AGENT_MODEL_ID
            if DEFAULT_AGENT_MODEL_ID in claude_model_ids
            else claude_model_ids[0]
            if claude_model_ids
            else None
        )
        options.append({
            "id": "claude_code",
            "name": "Claude Code",
            "available": bool(claude_model_ids),
            "unavailable_reason": (
                None
                if claude_model_ids
                else "No deployment-owned compatible model profile"
            ),
            "compatible_model_ids": claude_model_ids,
            "default_model_id": claude_default,
            "capabilities": [
                "skills", "multi_agent", "sandbox", "native_resume", "vision",
            ],
        })
    if settings.deepseek_harness_engine_enabled:
        deepseek_default = (
            canonical_current
            if canonical_current in deepseek_model_ids
            else DEFAULT_AGENT_MODEL_ID
            if DEFAULT_AGENT_MODEL_ID in deepseek_model_ids
            else deepseek_model_ids[0]
            if deepseek_model_ids
            else None
        )
        options.append({
            "id": "deepseek_harness",
            "name": "DeepSeek Harness",
            "available": bool(deepseek_model_ids),
            "unavailable_reason": (
                None
                if deepseek_model_ids
                else "No deployment-owned compatible model profile"
            ),
            "compatible_model_ids": deepseek_model_ids,
            "default_model_id": deepseek_default,
            "capabilities": [
                "skills", "multi_agent", "sandbox", "web_search",
            ],
        })
    return options


@router.get("/{cid}/workspace")
async def workspace_files(
    cid: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    await ensure_workspace_async(user.id, cid)
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
    workspace = await ensure_workspace_async(user.id, cid)
    try:
        file_path = safe_workspace_path_in_root(
            workspace,
            path,
            must_exist=True,
        )
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not file_path.is_file():
        raise HTTPException(400, "Not a regular file")
    meta = workspace_file_metadata(file_path)
    if not meta["is_text"]:
        return {
            "path": path,
            "content": "",
            "editable": False,
            **meta,
        }
    if file_path.stat().st_size > MAX_WORKSPACE_FILE_CHARS * 4:
        return {
            "path": path,
            "content": "",
            "editable": False,
            "too_large": True,
            **meta,
        }
    return {
        "path": path,
        "content": file_path.read_text(encoding="utf-8", errors="replace"),
        "editable": True,
        **meta,
    }


@router.get("/{cid}/workspace/file/raw")
async def raw_workspace_file(
    cid: str,
    path: str = Query(...),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    workspace = await ensure_workspace_async(user.id, cid)
    try:
        file_path = safe_workspace_path_in_root(
            workspace,
            path,
            must_exist=True,
        )
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not file_path.is_file():
        raise HTTPException(400, "Not a regular file")
    meta = workspace_file_metadata(file_path)
    headers = {"Content-Disposition": f'inline; filename="{file_path.name}"'}
    return FileResponse(file_path, media_type=meta["mime_type"], filename=file_path.name, headers=headers)


@router.put("/{cid}/workspace/file")
async def write_workspace_file(
    cid: str,
    payload: WorkspaceFileWrite,
    path: str = Query(...),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    if Path(path).name.startswith(".env") and Path(path).name != ".env.example":
        raise HTTPException(400, "Secret-bearing .env files are not allowed")

    def _write(workspace: Path) -> None:
        file_path = safe_workspace_path_in_root(workspace, path)
        atomic_write_text(file_path, payload.content)

    try:
        await run_session_workspace_mutation_async(user.id, cid, _write)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
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

    def _delete(workspace: Path) -> None:
        file_path = safe_workspace_path_in_root(
            workspace,
            path,
            must_exist=True,
        )
        if not file_path.is_file():
            raise HTTPException(400, "Not a regular file")
        file_path.unlink()

    try:
        await run_session_workspace_mutation_async(user.id, cid, _delete)
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@router.get("/{cid}/settings")
async def get_conversation_settings(
    cid: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    conv = await _conversation(cid, user.id, db)
    fallback_model_ids, _ = filter_agentic_fallback_model_ids(
        serialize_json_list(conv.fallback_model_ids, []),
        requested_model_id=conv.model_id,
    )
    enabled_tools = serialize_json_list(conv.enabled_tools, DEFAULT_NATIVE_TOOLS)
    tool_surface = {"chatds_capabilities": enabled_tools}
    if conv.engine_id == "deepseek_harness":
        from native_tools import deepseek_harness_native_tools
        tool_surface["deepseek_native_tools"] = list(
            deepseek_harness_native_tools(enabled_tools)
        )
    return {
        "engine_id": conv.engine_id,
        "engine_locked": bool((await db.execute(
            select(func.count(Message.id)).where(Message.conversation_id == cid)
        )).scalar_one() or (await db.execute(
            select(func.count(AgentRun.id)).where(AgentRun.conversation_id == cid)
        )).scalar_one()),
        "engine_options": await _engine_options_for_user(
            current_model_id=conv.model_id,
            user=user,
            db=db,
        ),
        "model_id": canonical_agent_model_id(conv.model_id),
        "enabled_tools": enabled_tools,
        "fallback_model_ids": fallback_model_ids,
        "enabled_user_skills": serialize_json_list(conv.enabled_user_skills, []),
        "permission_preset": conv.permission_preset,
        "tool_surface": tool_surface,
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
    if payload.engine_id is not None and payload.engine_id != conv.engine_id:
        from agent_engines.registry import build_agent_engine_registry

        if (
            payload.engine_id == "legacy"
            and not settings.legacy_engine_new_runs_enabled
        ):
            raise HTTPException(400, "Legacy Harness execution is disabled")
        try:
            build_agent_engine_registry().get(payload.engine_id)
        except LookupError as exc:
            raise HTTPException(400, "Requested Agent Engine is not configured") from exc
        durable_turns = int((await db.execute(
            select(func.count(Message.id)).where(Message.conversation_id == cid)
        )).scalar_one())
        durable_runs = int((await db.execute(
            select(func.count(AgentRun.id)).where(AgentRun.conversation_id == cid)
        )).scalar_one())
        if durable_turns or durable_runs:
            raise HTTPException(
                409,
                "Agent Engine is fixed after the first Turn; fork the Conversation to change it",
            )
        conv.engine_id = payload.engine_id
    if payload.model_id is not None:
        canonical_model_id = canonical_agent_model_id(payload.model_id)
        if not await _model_exists(canonical_model_id, user.id, db):
            raise HTTPException(400, f"Unknown model: {payload.model_id}")
        conv.model_id = canonical_model_id
    if payload.enabled_tools is not None:
        unknown_tools = set(payload.enabled_tools) - DEFAULT_NATIVE_TOOL_SET
        if unknown_tools:
            raise HTTPException(400, f"Unknown tools: {sorted(unknown_tools)}")
        conv.enabled_tools = json.dumps(list(dict.fromkeys(payload.enabled_tools)))
    if payload.fallback_model_ids is not None:
        for model_id in payload.fallback_model_ids:
            if not await _model_exists(model_id, user.id, db):
                raise HTTPException(400, f"Unknown fallback model: {model_id}")
        allowed_fallbacks, _ = filter_agentic_fallback_model_ids(
            payload.fallback_model_ids,
            requested_model_id=conv.model_id,
        )
        conv.fallback_model_ids = json.dumps(allowed_fallbacks)
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
    if payload.permission_preset is not None:
        active_runs = int((await db.execute(
            select(func.count(AgentRun.id)).where(
                AgentRun.conversation_id == cid,
                AgentRun.status.in_(("queued", "running", "committing")),
            )
        )).scalar_one())
        if active_runs:
            raise HTTPException(
                409,
                "Permission preset cannot change while a Turn is active",
            )
        conv.permission_preset = payload.permission_preset
    if conv.engine_id == "claude_code":
        from routers.chat_router import (
            claude_code_model_compatible,
            resolve_model_config,
        )

        provider_config = await resolve_model_config(conv.model_id, user, db)
        if not claude_code_model_compatible(provider_config):
            raise HTTPException(
                400,
                "The selected model is not compatible with the Claude Code engine",
            )
    if conv.engine_id == "deepseek_harness":
        from routers.chat_router import (
            deepseek_harness_model_compatible,
            resolve_model_config,
        )

        provider_config = await resolve_model_config(conv.model_id, user, db)
        if not deepseek_harness_model_compatible(provider_config):
            raise HTTPException(
                400,
                "The selected model is not compatible with the DeepSeek Harness engine",
            )
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


def _fork_snapshot_sha256(
    *,
    source: Conversation,
    messages: list[Message],
    skills: list[SkillPackage],
    title: str,
    include_messages: bool,
    workspace_digest: str,
    skills_digest: str,
    target_engine_id: str | None = None,
    target_model_id: str | None = None,
) -> str:
    payload = {
        "source": {
            "title": source.title,
            "model_id": source.model_id,
            "engine_id": source.engine_id,
            "enabled_tools": source.enabled_tools,
            "fallback_model_ids": source.fallback_model_ids,
            "enabled_user_skills": source.enabled_user_skills,
            "goal_objective": source.goal_objective,
            "goal_status": source.goal_status,
            "goal_token_budget": source.goal_token_budget,
        },
        "title": title,
        "include_messages": include_messages,
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "reasoning": message.reasoning,
                "tool_progress": message.tool_progress,
                "image_urls": message.image_urls,
                "model_id": message.model_id,
                "source": message.source,
                "input_tokens": message.input_tokens,
                "output_tokens": message.output_tokens,
                "total_tokens": message.total_tokens,
            }
            for message in messages
        ] if include_messages else [],
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "category": skill.category,
                "version": skill.version,
                "bundle_id": skill.bundle_id,
                "bundle_role": skill.bundle_role,
                "bundle_root_name": skill.bundle_root_name,
                "bundle_source_path": skill.bundle_source_path,
            }
            for skill in skills
        ],
        "workspace_digest": workspace_digest,
        "skills_digest": skills_digest,
    }
    # Preserve the historic digest for ordinary same-engine forks, while
    # binding an explicit engine/model transition into the immutable fork
    # identity. This also lets pre-upgrade recovery journals remain valid.
    if target_engine_id is not None:
        payload["target_engine_id"] = target_engine_id
    if target_model_id is not None:
        payload["target_model_id"] = target_model_id
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _fork_lifecycle_operation_id(
    *,
    user_id: str,
    source_id: str,
    target_id: str,
    snapshot_sha256: str,
) -> str:
    return hashlib.sha256(
        "\0".join((
            "fork-v1",
            str(user_id),
            str(source_id),
            str(target_id),
            str(snapshot_sha256),
        )).encode("utf-8")
    ).hexdigest()


def _prepare_fork_skill_snapshot(
    *,
    operation_dir: Path,
    staged_workspace_root: Path,
    staged_skill_root: Path,
    source_skill_scope: Path,
    source: Conversation,
    messages: list[Message],
    source_skills: list[SkillPackage],
    expected_title: str,
    include_messages: bool,
    user_id: str,
    source_id: str,
    target_id: str,
    target_engine_id: str | None,
    target_model_id: str | None,
    skill_api,
) -> dict:
    """Copy/digest/persist a fork stage entirely off the asyncio loop."""

    try:
        staged_skill_root.mkdir(parents=True)
        if source_skill_scope.is_symlink():
            raise HTTPException(
                status_code=409,
                detail="Source Skill scope must not be a symbolic link",
            )
        for skill in source_skills:
            source_member = source_skill_scope / str(skill.name)
            if not source_member.is_dir() or source_member.is_symlink():
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Source Skill '{skill.name}' is missing "
                        "from the fork snapshot"
                    ),
                )
            shutil.copytree(
                source_member,
                staged_skill_root / str(skill.name),
                symlinks=True,
            )
        bundle_ids = sorted({
            str(skill.bundle_id)
            for skill in source_skills
            if skill.bundle_id
        })
        for bundle_id in bundle_ids:
            source_runtime = (
                source_skill_scope / "_bundle_runtime" / bundle_id
            )
            if source_runtime.is_symlink():
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Source Skill runtime must not be a symbolic link"
                    ),
                )
            if source_runtime.is_dir():
                shutil.copytree(
                    source_runtime,
                    staged_skill_root / "_bundle_runtime" / bundle_id,
                    symlinks=True,
                )

        workspace_digest = _fork_directory_digest(
            staged_workspace_root,
            skill_api,
            ignore_workspace_lock=True,
        )
        skills_digest = skill_api._directory_digest(staged_skill_root)
        snapshot_sha256 = _fork_snapshot_sha256(
            source=source,
            messages=messages,
            skills=source_skills,
            title=expected_title,
            include_messages=include_messages,
            workspace_digest=workspace_digest,
            skills_digest=skills_digest,
            target_engine_id=target_engine_id,
            target_model_id=target_model_id,
        )
        journal = {
            "version": 1,
            "kind": "fork",
            "user_id": user_id,
            "source_id": source_id,
            "target_id": target_id,
            "title": expected_title,
            "include_messages": include_messages,
            "snapshot_sha256": snapshot_sha256,
            "workspace_digest": workspace_digest,
            "skills_digest": skills_digest,
            "state": "prepared",
        }
        if target_engine_id is not None:
            journal["target_engine_id"] = target_engine_id
        if target_model_id is not None:
            journal["target_model_id"] = target_model_id
        skill_api._atomic_write_json(
            operation_dir / "journal.json",
            journal,
        )
        return journal
    except BaseException:
        shutil.rmtree(operation_dir, ignore_errors=True)
        raise


def _create_fork_bootstrap_workspace(staged_workspace: Path) -> None:
    staged_workspace.mkdir(parents=True)
    for filename, content in workspace_store.BOOTSTRAP_FILES.items():
        (staged_workspace / filename).write_text(
            content,
            encoding="utf-8",
        )


def _fork_directory_digest(
    path: Path,
    skill_api,
    *,
    ignore_workspace_lock: bool,
) -> str:
    if not ignore_workspace_lock:
        return skill_api._directory_digest(path)
    if not path.is_dir() or path.is_symlink():
        raise HTTPException(
            status_code=409,
            detail="Expected immutable fork workspace is missing",
        )
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise HTTPException(
                status_code=409,
                detail="Symbolic link found in immutable fork workspace",
            )
        if not child.is_file():
            continue
        relative = str(child.relative_to(path)).replace("\\", "/")
        # The shared Backend/Harness flock is session lifecycle metadata, not
        # user workspace content. It can legitimately appear after publish
        # and must not invalidate the immutable fork snapshot.
        if relative == WORKSPACE_MUTATION_LOCK_FILENAME:
            continue
        data_digest = skill_api._sha256_file(child)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(child.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(data_digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _fork_directory_matches(
    path: Path,
    digest: str,
    skill_api,
    *,
    ignore_workspace_lock: bool = False,
) -> bool:
    return (
        path.is_dir()
        and not path.is_symlink()
        and _fork_directory_digest(
            path,
            skill_api,
            ignore_workspace_lock=ignore_workspace_lock,
        ) == digest
    )


def _cleanup_uncommitted_fork_filesystem(
    *,
    target_workspace_root: Path,
    target_skill_dir: Path,
    operation_dir: Path,
    journal: dict,
    skill_api,
    published_workspace: tuple[Path, int, int] | None = None,
    published_skills: tuple[Path, int, int] | None = None,
) -> None:
    if published_skills is not None:
        skill_api._remove_request_owned_directory(published_skills)
    elif _fork_directory_matches(
        target_skill_dir,
        str(journal.get("skills_digest") or ""),
        skill_api,
    ):
        shutil.rmtree(target_skill_dir, ignore_errors=True)
    if published_workspace is not None:
        skill_api._remove_request_owned_directory(published_workspace)
    elif _fork_directory_matches(
        target_workspace_root,
        str(journal.get("workspace_digest") or ""),
        skill_api,
        ignore_workspace_lock=True,
    ):
        shutil.rmtree(target_workspace_root, ignore_errors=True)
    shutil.rmtree(operation_dir, ignore_errors=True)


def _cleanup_uncommitted_fork_transaction(
    *,
    user_id: str,
    target_id: str,
    pending_operation_id: str,
    target_workspace_root: Path,
    target_skill_dir: Path,
    operation_dir: Path,
    journal: dict,
    skill_api,
    published_workspace: tuple[Path, int, int] | None = None,
    published_skills: tuple[Path, int, int] | None = None,
) -> None:
    try:
        _cleanup_uncommitted_fork_filesystem(
            target_workspace_root=target_workspace_root,
            target_skill_dir=target_skill_dir,
            operation_dir=operation_dir,
            journal=journal,
            skill_api=skill_api,
            published_workspace=published_workspace,
            published_skills=published_skills,
        )
    finally:
        workspace_store.clear_session_pending_fence(
            user_id,
            target_id,
            pending_operation_id,
        )


def _publish_fork_filesystem(
    *,
    target_workspace_root: Path,
    target_skill_dir: Path,
    staged_workspace_root: Path,
    staged_skill_root: Path,
    journal_path: Path,
    journal: dict,
    skill_api,
) -> tuple[
    tuple[Path, int, int] | None,
    tuple[Path, int, int] | None,
]:
    """Validate and publish both fork trees in one off-loop transaction."""

    expected_workspace_digest = str(
        journal.get("workspace_digest") or ""
    )
    expected_skills_digest = str(journal.get("skills_digest") or "")
    published_workspace: tuple[Path, int, int] | None = None
    published_skills: tuple[Path, int, int] | None = None
    if target_workspace_root.exists():
        if not _fork_directory_matches(
            target_workspace_root,
            expected_workspace_digest,
            skill_api,
            ignore_workspace_lock=True,
        ):
            raise HTTPException(
                status_code=409,
                detail="Published fork workspace drifted",
            )
    else:
        if not _fork_directory_matches(
            staged_workspace_root,
            expected_workspace_digest,
            skill_api,
            ignore_workspace_lock=True,
        ):
            raise HTTPException(
                status_code=409,
                detail="Staged fork workspace is incomplete",
            )
        target_workspace_root.parent.mkdir(parents=True, exist_ok=True)
        published_workspace = skill_api._publish_staged_directory(
            staged_workspace_root,
            target_workspace_root,
        )

    if target_skill_dir.exists():
        if not _fork_directory_matches(
            target_skill_dir,
            expected_skills_digest,
            skill_api,
        ):
            raise HTTPException(
                status_code=409,
                detail="Published fork Skill snapshot drifted",
            )
    else:
        if not _fork_directory_matches(
            staged_skill_root,
            expected_skills_digest,
            skill_api,
        ):
            raise HTTPException(
                status_code=409,
                detail="Staged fork Skill snapshot is incomplete",
            )
        target_skill_dir.parent.mkdir(parents=True, exist_ok=True)
        published_skills = skill_api._publish_staged_directory(
            staged_skill_root,
            target_skill_dir,
        )

    journal["state"] = "published"
    skill_api._atomic_write_json(journal_path, journal)
    return published_workspace, published_skills


def _verify_committed_fork_filesystem(
    *,
    target_workspace_root: Path,
    target_skill_dir: Path,
    journal: dict,
    skill_api,
) -> bool:
    return (
        _fork_directory_matches(
            target_workspace_root,
            str(journal.get("workspace_digest") or ""),
            skill_api,
            ignore_workspace_lock=True,
        )
        and _fork_directory_matches(
            target_skill_dir,
            str(journal.get("skills_digest") or ""),
            skill_api,
        )
    )


def _complete_fork_journal(
    *,
    operation_dir: Path,
    journal_path: Path,
    journal: dict,
    mcp_rebuild: dict,
    skill_api,
) -> None:
    shutil.rmtree(operation_dir / "staging", ignore_errors=True)
    journal["state"] = "completed"
    journal["mcp_status"] = str(
        (mcp_rebuild.get("mcp") or {}).get("status") or "unknown"
    )
    skill_api._atomic_write_json(journal_path, journal)


@router.post("/{cid}/fork")
async def fork_conversation(
    cid: str,
    title: str | None = None,
    include_messages: bool = True,
    fork_id: str | None = None,
    target_engine_id: str | None = None,
    target_model_id: str | None = None,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    return await _fork_conversation_impl(
        cid,
        title=title,
        include_messages=include_messages,
        fork_id=fork_id,
        target_engine_id=target_engine_id,
        target_model_id=target_model_id,
        user=user,
        db=db,
        source_maintenance_lease_already_held=False,
    )


async def _fork_conversation_impl(
    cid: str,
    *,
    title: str | None,
    include_messages: bool,
    fork_id: str | None,
    target_engine_id: str | None = None,
    target_model_id: str | None = None,
    user,
    db,
    source_maintenance_lease_already_held: bool,
):
    from routers import skill_router as skill_api

    if fork_id is None:
        target_id = uuid.uuid4().hex
    else:
        target_id = str(fork_id).strip().lower()
        if re.fullmatch(r"[a-f0-9]{32}", target_id) is None:
            raise HTTPException(
                status_code=400,
                detail="fork_id must be a 32-character lowercase hex identifier",
            )
    if target_id == cid:
        raise HTTPException(
            status_code=409,
            detail="A fork target must differ from its source conversation",
        )
    # An exact retry is the only operation allowed to enter a target that is
    # durably fenced by an earlier post-commit fork failure. Deletion remains
    # authoritative and is never bypassed.
    workspace_store.require_session_workspace_not_deleted(
        user.id,
        target_id,
    )

    operation_dir = skill_api._skill_operation_dir(
        user_id=user.id,
        kind="fork",
        identity_parts=[cid, target_id],
    )
    journal_path = operation_dir / "journal.json"
    target_workspace_root = (
        workspace_store.WORKSPACE_ROOT / user.id / target_id
    )
    target_skill_dir = skill_api.SKILLS_DATA_DIR / user.id / target_id
    caller_cancellation: BaseException | None = None
    idempotent = False

    # Retain both lifecycle locks through snapshot, DB projection, and MCP
    # reconciliation. Stable ordering prevents opposite-direction forks from
    # deadlocking, and delete/install use the same per-session lock.
    async with AsyncExitStack() as lifecycle_locks:
        from session_lifecycle import session_skill_lifecycle_lock

        for session_id in sorted({cid, target_id}):
            await lifecycle_locks.enter_async_context(
                session_skill_lifecycle_lock(
                    user.id,
                    session_id,
                    bounded=(
                        source_maintenance_lease_already_held
                        and session_id == cid
                    ),
                )
            )
        from routers.chat_router import conversation_maintenance_lease
        # A known target id must not admit chat between its DB commit and the
        # digest/MCP/journal tail. Hold both conversation barriers in the same
        # stable order as the Skill lifecycle locks until the fork transaction
        # is fully completed.
        for session_id in sorted({cid, target_id}):
            if (
                source_maintenance_lease_already_held
                and session_id == cid
            ):
                continue
            await lifecycle_locks.enter_async_context(
                conversation_maintenance_lease(session_id)
            )
        workspace_store.require_session_workspace_active(user.id, cid)
        workspace_store.require_session_workspace_not_deleted(
            user.id,
            target_id,
        )
        if True:
            source = await _conversation(cid, user.id, db)
            requested_target_engine = (
                str(target_engine_id).strip()
                if target_engine_id is not None
                else None
            )
            requested_target_model = (
                canonical_agent_model_id(str(target_model_id).strip())
                if target_model_id is not None
                else None
            )
            effective_target_engine = requested_target_engine or source.engine_id
            effective_target_model = requested_target_model or source.model_id
            from agent_engines.base import (
                ENGINE_ID_CLAUDE_CODE,
                ENGINE_ID_DEEPSEEK_HARNESS,
            )
            from agent_engines.registry import build_agent_engine_registry
            if (
                effective_target_engine == "legacy"
                and not settings.legacy_engine_new_runs_enabled
            ):
                raise HTTPException(
                    400,
                    "Legacy Harness execution is disabled for target Conversations",
                )
            try:
                build_agent_engine_registry().get(effective_target_engine)
            except LookupError as exc:
                raise HTTPException(
                    400,
                    "Requested target Agent Engine is not configured",
                ) from exc
            if not await _model_exists(effective_target_model, user.id, db):
                raise HTTPException(400, "Requested target model is not configured")
            if effective_target_engine == ENGINE_ID_CLAUDE_CODE:
                from routers.chat_router import (
                    claude_code_model_compatible,
                    resolve_model_config,
                )

                target_provider = await resolve_model_config(
                    effective_target_model,
                    user,
                    db,
                )
                if not claude_code_model_compatible(target_provider):
                    raise HTTPException(
                        400,
                        "The target model has no deployment-owned Claude Code provider profile",
                    )
            if effective_target_engine == ENGINE_ID_DEEPSEEK_HARNESS:
                from routers.chat_router import (
                    deepseek_harness_model_compatible,
                    resolve_model_config,
                )

                target_provider = await resolve_model_config(
                    effective_target_model,
                    user,
                    db,
                )
                if not deepseek_harness_model_compatible(target_provider):
                    raise HTTPException(
                        400,
                        "The target model has no deployment-owned DeepSeek Harness provider profile",
                    )
            target = (await db.execute(
                select(Conversation).where(
                    Conversation.id == target_id,
                    Conversation.user_id == user.id,
                )
            )).scalar_one_or_none()
            messages = list((await db.execute(
                select(Message)
                .where(Message.conversation_id == cid)
                .order_by(Message.created_at, Message.id)
            )).scalars().all())
            source_skills = list((await db.execute(
                select(SkillPackage).where(
                    SkillPackage.user_id == user.id,
                    SkillPackage.session_id == cid,
                ).order_by(SkillPackage.name, SkillPackage.id)
            )).scalars().all())

            journal = await run_sync_cancellation_safe(
                lambda: skill_api._read_operation_journal(journal_path)
            )
            expected_title = title or ((source.title or "会话") + " · 分支")
            recovering_committed = target is not None
            if recovering_committed:
                if (
                    target.forked_from_conversation_id != cid
                    or not target.fork_snapshot_sha256
                    or not isinstance(journal, dict)
                    or journal.get("version") != 1
                    or journal.get("kind") != "fork"
                    or journal.get("source_id") != cid
                    or journal.get("target_id") != target_id
                    or journal.get("title") != expected_title
                    or bool(journal.get("include_messages"))
                    != include_messages
                    or journal.get("target_engine_id")
                    != requested_target_engine
                    or journal.get("target_model_id")
                    != requested_target_model
                    or journal.get("snapshot_sha256")
                    != target.fork_snapshot_sha256
                    or journal.get("state")
                    not in {"published", "committed", "completed"}
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="fork_id already belongs to another or incomplete fork",
                    )
                idempotent = True
            elif isinstance(journal, dict) and (
                journal.get("version") != 1
                or journal.get("kind") != "fork"
                or journal.get("source_id") != cid
                or journal.get("target_id") != target_id
                or journal.get("title") != expected_title
                or bool(journal.get("include_messages")) != include_messages
                or journal.get("target_engine_id") != requested_target_engine
                or journal.get("target_model_id") != requested_target_model
                or journal.get("state") not in {"prepared", "published"}
            ):
                raise HTTPException(
                    status_code=409,
                    detail="A conflicting fork recovery record exists",
                )

            staging_root = operation_dir / "staging"
            staged_workspace_root = staging_root / "workspace-session"
            staged_skill_root = staging_root / "skills"
            published_workspace: tuple[Path, int, int] | None = None
            published_skills: tuple[Path, int, int] | None = None

            if not recovering_committed and journal is None:
                if (
                    target_workspace_root.exists()
                    or target_workspace_root.is_symlink()
                    or target_skill_dir.exists()
                    or target_skill_dir.is_symlink()
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="fork_id already has unowned filesystem state",
                    )
                if operation_dir.exists():
                    await run_sync_cancellation_safe(
                        lambda: shutil.rmtree(
                            operation_dir,
                            ignore_errors=True,
                        )
                    )
                try:
                    staged_workspace = staged_workspace_root / "workspace"
                    source_workspace = (
                        workspace_store.WORKSPACE_ROOT
                        / user.id
                        / cid
                        / "workspace"
                    )
                    if source_workspace.is_symlink():
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "Source workspace root must not be a "
                                "symbolic link"
                            ),
                        )
                    if source_workspace.is_dir():
                        def _copy_source_workspace(
                            locked_source_workspace: Path,
                        ) -> None:
                            if locked_source_workspace.is_symlink():
                                raise HTTPException(
                                    status_code=409,
                                    detail=(
                                        "Source workspace root changed to a "
                                        "symbolic link"
                                    ),
                                )
                            shutil.copytree(
                                locked_source_workspace,
                                staged_workspace,
                                symlinks=True,
                            )

                        await workspace_store.run_session_workspace_mutation_async(
                            user.id,
                            cid,
                            _copy_source_workspace,
                        )
                    else:
                        await run_sync_cancellation_safe(
                            lambda: _create_fork_bootstrap_workspace(
                                staged_workspace
                            )
                        )

                    source_skill_scope = (
                        skill_api.SKILLS_DATA_DIR / user.id / cid
                    )
                    journal = await run_sync_cancellation_safe(
                        lambda: _prepare_fork_skill_snapshot(
                            operation_dir=operation_dir,
                            staged_workspace_root=staged_workspace_root,
                            staged_skill_root=staged_skill_root,
                            source_skill_scope=source_skill_scope,
                            source=source,
                            messages=messages,
                            source_skills=source_skills,
                            expected_title=expected_title,
                            include_messages=include_messages,
                            user_id=user.id,
                            source_id=cid,
                            target_id=target_id,
                            target_engine_id=requested_target_engine,
                            target_model_id=requested_target_model,
                            skill_api=skill_api,
                        )
                    )
                except BaseException:
                    await run_sync_cancellation_safe(
                        lambda: shutil.rmtree(
                            operation_dir,
                            ignore_errors=True,
                        )
                    )
                    raise

            if not isinstance(journal, dict):
                raise HTTPException(
                    status_code=409,
                    detail="Fork recovery journal is missing or invalid",
                )
            pending_operation_id = _fork_lifecycle_operation_id(
                user_id=user.id,
                source_id=cid,
                target_id=target_id,
                snapshot_sha256=str(journal.get("snapshot_sha256") or ""),
            )
            await run_sync_cancellation_safe(
                lambda: workspace_store.claim_session_pending_fence(
                    user.id,
                    target_id,
                    pending_operation_id,
                )
            )
            if not recovering_committed:
                current_snapshot = await run_sync_cancellation_safe(
                    lambda: _fork_snapshot_sha256(
                        source=source,
                        messages=messages,
                        skills=source_skills,
                        title=expected_title,
                        include_messages=include_messages,
                        workspace_digest=str(
                            journal.get("workspace_digest") or ""
                        ),
                        skills_digest=str(
                            journal.get("skills_digest") or ""
                        ),
                        target_engine_id=requested_target_engine,
                        target_model_id=requested_target_model,
                    )
                )
                if current_snapshot != journal.get("snapshot_sha256"):
                    await run_sync_cancellation_safe(
                        lambda: _cleanup_uncommitted_fork_transaction(
                            user_id=user.id,
                            target_id=target_id,
                            pending_operation_id=pending_operation_id,
                            target_workspace_root=target_workspace_root,
                            target_skill_dir=target_skill_dir,
                            operation_dir=operation_dir,
                            journal=journal,
                            skill_api=skill_api,
                        )
                    )
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Source conversation changed after fork staging; "
                            "retry with a new fork_id"
                        ),
                )
                fork_committed = False
                try:
                    if not await run_sync_cancellation_safe(
                        lambda: workspace_store.session_pending_fence_matches(
                            user.id,
                            target_id,
                            pending_operation_id,
                        )
                    ):
                        raise RuntimeError(
                            "Fork target lifecycle fence is missing"
                        )
                    (
                        published_workspace,
                        published_skills,
                    ) = await run_sync_cancellation_safe(
                        lambda: _publish_fork_filesystem(
                            target_workspace_root=target_workspace_root,
                            target_skill_dir=target_skill_dir,
                            staged_workspace_root=staged_workspace_root,
                            staged_skill_root=staged_skill_root,
                            journal_path=journal_path,
                            journal=journal,
                            skill_api=skill_api,
                        )
                    )
                    fork = Conversation(
                        id=target_id,
                        user_id=user.id,
                        title=expected_title,
                        model_id=effective_target_model,
                        engine_id=effective_target_engine,
                        permission_preset=source.permission_preset,
                        enabled_tools=source.enabled_tools,
                        fallback_model_ids=source.fallback_model_ids,
                        enabled_user_skills=source.enabled_user_skills,
                        forked_from_conversation_id=cid,
                        fork_snapshot_sha256=str(
                            journal["snapshot_sha256"]
                        ),
                        goal_objective=source.goal_objective,
                        goal_status=(
                            "paused" if source.goal_objective else None
                        ),
                        goal_note=(
                            "Forked from another session."
                            if source.goal_objective
                            else None
                        ),
                        goal_token_budget=source.goal_token_budget,
                    )
                    db.add(fork)
                    if include_messages:
                        for message in messages:
                            db.add(Message(
                                conversation_id=target_id,
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
                    for skill in source_skills:
                        db.add(SkillPackage(
                            user_id=user.id,
                            session_id=target_id,
                            name=skill.name,
                            description=skill.description,
                            category=skill.category,
                            version=skill.version,
                            bundle_id=skill.bundle_id,
                            bundle_role=skill.bundle_role,
                            bundle_root_name=skill.bundle_root_name,
                            bundle_source_path=skill.bundle_source_path,
                        ))
                    if not await run_sync_cancellation_safe(
                        lambda: workspace_store.session_pending_fence_matches(
                            user.id,
                            target_id,
                            pending_operation_id,
                        )
                    ):
                        raise RuntimeError(
                            "Fork target lifecycle fence changed before commit"
                        )
                    caller_cancellation = (
                        await skill_api._finish_awaitable_despite_cancellation(
                            db.commit()
                        )
                    )
                    fork_committed = True
                    journal["state"] = "committed"
                    await run_sync_cancellation_safe(
                        lambda: skill_api._atomic_write_json(
                            journal_path,
                            journal,
                        )
                    )
                    target = fork
                except BaseException:
                    if not fork_committed:
                        await skill_api._rollback_database_best_effort(db)
                        await run_sync_cancellation_safe(
                            lambda: _cleanup_uncommitted_fork_transaction(
                                user_id=user.id,
                                target_id=target_id,
                                pending_operation_id=pending_operation_id,
                                target_workspace_root=target_workspace_root,
                                target_skill_dir=target_skill_dir,
                                operation_dir=operation_dir,
                                journal=journal,
                                skill_api=skill_api,
                                published_workspace=published_workspace,
                                published_skills=published_skills,
                            )
                        )
                    raise

            if target is None:
                raise HTTPException(
                    status_code=409,
                    detail="Fork database projection is incomplete",
                )
            if not await run_sync_cancellation_safe(
                lambda: workspace_store.session_pending_fence_matches(
                    user.id,
                    target_id,
                    pending_operation_id,
                )
            ):
                raise RuntimeError(
                    "Committed fork target lifecycle fence is missing"
                )
            filesystem_complete = await run_sync_cancellation_safe(
                lambda: _verify_committed_fork_filesystem(
                    target_workspace_root=target_workspace_root,
                    target_skill_dir=target_skill_dir,
                    journal=journal,
                    skill_api=skill_api,
                )
            )
            if not filesystem_complete:
                raise HTTPException(
                    status_code=409,
                    detail="Committed fork filesystem snapshot is incomplete",
                )

            target_skills = list((await db.execute(
                select(SkillPackage).where(
                    SkillPackage.user_id == user.id,
                    SkillPackage.session_id == target_id,
                ).order_by(SkillPackage.name)
            )).scalars().all())
            cloned_skills = [{
                "name": skill.name,
                "description": skill.description or "",
                "category": skill.category,
                "version": skill.version or "",
                "session_id": target_id,
                "bundle_id": skill.bundle_id,
                "bundle_role": skill.bundle_role,
                "bundle_root_name": skill.bundle_root_name,
                "bundle_source_path": skill.bundle_source_path,
            } for skill in target_skills]
            if cloned_skills:
                mcp_value, mcp_cancellation = (
                    await skill_api._finish_awaitable_with_result(
                        skill_api._rebuild_mcp_for_skills(
                            skills=cloned_skills,
                            target_dirs={
                                str(skill["name"]):
                                target_skill_dir / str(skill["name"])
                                for skill in cloned_skills
                            },
                            user_id=user.id,
                            session_id=target_id,
                            reason=(
                                "conversation_fork_recovery"
                                if idempotent
                                else "conversation_fork"
                            ),
                        )
                    )
                )
                if not isinstance(mcp_value, dict):
                    raise RuntimeError("Invalid fork MCP rebuild result")
                mcp_rebuild = mcp_value
                caller_cancellation = (
                    caller_cancellation or mcp_cancellation
                )
            else:
                mcp_rebuild = {
                    "mcp": {
                        "registered": [],
                        "skipped": [],
                        "errors": [],
                        "runtime": [],
                        "status": "not_required",
                        "reason": "conversation_fork",
                    },
                    "mcp_by_skill": {},
                    "runtime": [],
                }
            await run_sync_cancellation_safe(
                lambda: _complete_fork_journal(
                    operation_dir=operation_dir,
                    journal_path=journal_path,
                    journal=journal,
                    mcp_rebuild=mcp_rebuild,
                    skill_api=skill_api,
                )
            )
            pending_cleared = await run_sync_cancellation_safe(
                lambda: workspace_store.clear_session_pending_fence(
                    user.id,
                    target_id,
                    pending_operation_id,
                )
            )
            if not pending_cleared:
                raise RuntimeError(
                    "Completed fork lifecycle fence was not released"
                )

    if caller_cancellation is not None:
        raise caller_cancellation
    await emit_event(
        user.id, "session.forked",
        {
            "source_conversation_id": cid,
            "conversation_id": target_id,
            "snapshot_sha256": journal["snapshot_sha256"],
            "idempotent": idempotent,
            "engine_id": target.engine_id,
            "model_id": target.model_id,
        },
        target_id,
    )
    return {
        "id": target_id,
        "title": target.title,
        "source_conversation_id": cid,
        "cloned_skill_count": len(cloned_skills),
        "skill_mcp_rebuild": mcp_rebuild,
        "fork_snapshot_sha256": journal["snapshot_sha256"],
        "engine_id": target.engine_id,
        "model_id": target.model_id,
        "idempotent": idempotent,
        "operation_status": "completed",
    }


def _artifact_to_dict(artifact: Artifact) -> dict:
    metadata, metadata_truncated = _bounded_json_object(
        artifact.metadata_json,
    )
    summary = _bounded_text(artifact.summary, 2000)
    return {
        "id": artifact.id,
        "run_id": artifact.run_id,
        "root_run_id": artifact.root_run_id,
        "parent_run_id": artifact.parent_run_id,
        "kind": _bounded_text(artifact.kind, 32),
        "title": _bounded_text(artifact.title, 256) or None,
        "path": _bounded_text(artifact.path, 1024) or None,
        "mime_type": _bounded_text(artifact.mime_type, 128) or None,
        "preview_kind": _bounded_text(artifact.preview_kind, 32) or None,
        "size_bytes": artifact.size_bytes,
        "sha256": _bounded_text(artifact.sha256, 64) or None,
        "source_tool_name": (
            _bounded_text(artifact.source_tool_name, 128) or None
        ),
        "source_tool_call_id": (
            _bounded_text(artifact.source_tool_call_id, 128) or None
        ),
        "source_event_key": (
            _bounded_text(artifact.source_event_key, 192) or None
        ),
        "summary": summary or None,
        "summary_truncated": len(str(artifact.summary or "")) > 2000,
        "metadata": metadata,
        "metadata_truncated": metadata_truncated,
        "created_at": str(artifact.created_at),
    }


def _task_to_dict(task: TaskItem) -> dict:
    metadata, metadata_truncated = _bounded_json_object(
        task.metadata_json,
    )
    summary = _bounded_text(task.summary, 2000)
    error = _bounded_text(task.error, 4000)
    return {
        "id": task.id,
        "run_id": task.run_id,
        "root_run_id": task.root_run_id,
        "parent_run_id": task.parent_run_id,
        "task_key": _bounded_text(task.task_key, 192),
        "kind": _bounded_text(task.kind, 32),
        "title": _bounded_text(task.title, 256) or None,
        "status": _bounded_text(task.status, 24),
        "agent_name": _bounded_text(task.agent_name, 128) or None,
        "summary": summary or None,
        "summary_truncated": len(str(task.summary or "")) > 2000,
        "error": error or None,
        "error_truncated": len(str(task.error or "")) > 4000,
        "metadata": metadata,
        "metadata_truncated": metadata_truncated,
        "started_at": str(task.started_at),
        "ended_at": str(task.ended_at) if task.ended_at else None,
        "updated_at": str(task.updated_at),
    }


@router.get("/{cid}/artifacts")
async def list_artifacts(
    cid: str,
    run_id: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    query = select(Artifact).where(
        Artifact.conversation_id == cid,
        Artifact.user_id == user.id,
    )
    if run_id:
        query = query.where(Artifact.run_id == run_id)
    artifacts = (await db.execute(
        query.order_by(desc(Artifact.created_at)).limit(limit)
    )).scalars().all()
    return {"artifacts": [_artifact_to_dict(artifact) for artifact in artifacts]}


@router.get("/{cid}/artifacts/{artifact_id}")
async def get_artifact(
    cid: str,
    artifact_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    artifact = (await db.execute(
        select(Artifact).where(
            Artifact.id == artifact_id,
            Artifact.conversation_id == cid,
            Artifact.user_id == user.id,
        )
    )).scalar_one_or_none()
    if artifact is None:
        raise HTTPException(404, "Artifact not found")
    return _artifact_to_dict(artifact)


@router.get("/{cid}/tasks")
async def list_tasks(
    cid: str,
    limit: int = Query(500, ge=1, le=1000),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    tasks = (await db.execute(
        select(TaskItem)
        .where(TaskItem.conversation_id == cid, TaskItem.user_id == user.id)
        .order_by(desc(TaskItem.updated_at))
        .limit(limit)
    )).scalars().all()
    return {"tasks": [_task_to_dict(task) for task in tasks]}


def _run_to_dict(run: AgentRun) -> dict:
    requested_tools, requested_meta = _bounded_json_field(
        run.requested_tools,
        expected_type=list,
        empty=[],
    )
    effective_tools, effective_meta = _bounded_json_field(
        run.effective_tools,
        expected_type=list,
        empty=[],
    )
    policy, policy_meta = _bounded_json_field(
        run.policy,
        expected_type=dict,
        empty=None,
    )
    tool_events, tool_events_meta = _bounded_json_field(
        run.tool_events,
        expected_type=list,
        empty=[],
    )
    error_source_chars = len(str(run.error or ""))
    error_source_bytes = len(
        str(run.error or "").encode("utf-8", "replace")
    )
    error = _bounded_text(run.error, _RUN_DTO_ERROR_LIMIT)
    error_truncated = error_source_chars > _RUN_DTO_ERROR_LIMIT
    json_meta = {
        "requested_tools": requested_meta,
        "effective_tools": effective_meta,
        "policy": policy_meta,
        "tool_events": tool_events_meta,
    }
    dto_truncated_fields = [
        field
        for field, metadata in json_meta.items()
        if metadata["truncated"]
    ]
    if error_truncated:
        dto_truncated_fields.append("error")
    return {
        "id": run.id,
        "parent_run_id": run.parent_run_id,
        "root_run_id": run.root_run_id or run.id,
        "delegation_tool_call_id": run.delegation_tool_call_id,
        "agent_kind": run.agent_kind or "primary",
        "agent_name": run.agent_name,
        "depth": run.depth or 0,
        "workspace_scope": run.workspace_scope or "shared_session",
        "workspace_ref": run.workspace_ref,
        "source": run.source,
        "engine_id": run.engine_id,
        "engine_version": run.engine_version,
        "native_session_id": run.native_session_id,
        "requested_model_id": run.requested_model_id,
        "resolved_model_id": run.resolved_model_id,
        "status": run.status,
        "finish_reason": run.finish_reason,
        "error": error or None,
        "error_source_chars": error_source_chars,
        "error_source_bytes": error_source_bytes,
        "error_truncated": error_truncated,
        "requested_tools": requested_tools,
        "requested_tool_count": requested_meta["count"],
        "requested_tools_source_chars": requested_meta["source_chars"],
        "requested_tools_source_bytes": requested_meta["source_bytes"],
        "requested_tools_truncated": requested_meta["truncated"],
        "requested_tools_malformed": requested_meta["malformed"],
        "effective_tools": effective_tools,
        "effective_tool_count": effective_meta["count"],
        "effective_tools_source_chars": effective_meta["source_chars"],
        "effective_tools_source_bytes": effective_meta["source_bytes"],
        "effective_tools_truncated": effective_meta["truncated"],
        "effective_tools_malformed": effective_meta["malformed"],
        "policy": policy,
        "policy_item_count": policy_meta["count"],
        "policy_source_chars": policy_meta["source_chars"],
        "policy_source_bytes": policy_meta["source_bytes"],
        "policy_truncated": policy_meta["truncated"],
        "policy_malformed": policy_meta["malformed"],
        "tool_events": tool_events,
        "tool_event_count": tool_events_meta["count"],
        "tool_events_source_chars": tool_events_meta["source_chars"],
        "tool_events_source_bytes": tool_events_meta["source_bytes"],
        "tool_events_truncated": tool_events_meta["truncated"],
        "tool_events_malformed": tool_events_meta["malformed"],
        "dto_truncated": bool(dto_truncated_fields),
        "dto_truncated_fields": dto_truncated_fields,
        "usage": {
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "total_tokens": run.total_tokens,
        },
        "started_at": str(run.started_at),
        "ended_at": str(run.ended_at) if run.ended_at else None,
        "children": [],
    }


_ACTIVE_RUN_STATUSES = frozenset({
    "pending",
    "planned",
    "queued",
    "running",
    "committing",
})
_GENERIC_AGENT_NAME_RE = re.compile(
    r"^(?:agent|delegate|worker|child)(?:[-_ ]?\d+)?$",
    re.IGNORECASE,
)
_RUN_CARD_EVENT_TYPES = frozenset({
    "agent.spawned",
    "run.started",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "verifier.requested",
    "verifier.completed",
    "verifier.failed",
})
_RUN_CARD_TOOL_ATTEMPT_LIMIT = 24
_RUN_CARD_ARTIFACT_LIMIT = 24
_RUN_CARD_MAX_ACTIVE_RUNS = 512
_RUN_CARD_MAX_RUNS = 1000
_RUN_CARD_MAX_EVENTS = 10000
_RUN_CARD_MAX_TASKS = 10000
_RUN_CARD_MAX_ARTIFACTS = 5000
_DTO_JSON_SOURCE_LIMIT = 64 * 1024
_DTO_JSON_MAX_DEPTH = 4
_DTO_JSON_MAX_ITEMS = 64
_DTO_JSON_STRING_LIMIT = 1000
_RUN_DTO_ERROR_LIMIT = 4000


def _bounded_json_value(
    value: object,
    *,
    depth: int = 0,
) -> tuple[object, bool]:
    """Bound untrusted legacy JSON before returning it in query DTOs."""

    if depth >= _DTO_JSON_MAX_DEPTH:
        if isinstance(value, (dict, list)):
            return None, True
        if isinstance(value, str):
            return (
                _bounded_text(value, _DTO_JSON_STRING_LIMIT),
                len(value) > _DTO_JSON_STRING_LIMIT,
            )
        return value, False
    if isinstance(value, dict):
        bounded: dict[str, object] = {}
        truncated = len(value) > _DTO_JSON_MAX_ITEMS
        for key, item in list(value.items())[:_DTO_JSON_MAX_ITEMS]:
            bounded_key = _bounded_text(key, 128)
            bounded_item, item_truncated = _bounded_json_value(
                item,
                depth=depth + 1,
            )
            bounded[bounded_key] = bounded_item
            truncated = truncated or item_truncated
        return bounded, truncated
    if isinstance(value, list):
        bounded_list = []
        truncated = len(value) > _DTO_JSON_MAX_ITEMS
        for item in value[:_DTO_JSON_MAX_ITEMS]:
            bounded_item, item_truncated = _bounded_json_value(
                item,
                depth=depth + 1,
            )
            bounded_list.append(bounded_item)
            truncated = truncated or item_truncated
        return bounded_list, truncated
    if isinstance(value, str):
        return (
            _bounded_text(value, _DTO_JSON_STRING_LIMIT),
            len(value) > _DTO_JSON_STRING_LIMIT,
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    return _bounded_text(value, _DTO_JSON_STRING_LIMIT), True


def _bounded_json_field(
    value: str | None,
    *,
    expected_type: type,
    empty: object,
) -> tuple[object, dict]:
    """Parse one legacy Text JSON field into a bounded DTO value."""

    source = str(value or "")
    source_bytes = len(source.encode("utf-8", "replace"))
    metadata = {
        "source_chars": len(source),
        "source_bytes": source_bytes,
        "count": 0,
        "truncated": False,
        "malformed": False,
    }
    if not source:
        return empty, metadata
    if source_bytes > _DTO_JSON_SOURCE_LIMIT:
        metadata.update({
            "count": None,
            "truncated": True,
        })
        return empty, metadata
    try:
        parsed = json.loads(source)
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata.update({
            "count": None,
            "truncated": True,
            "malformed": True,
        })
        return empty, metadata
    if not isinstance(parsed, expected_type):
        metadata.update({
            "count": None,
            "truncated": True,
            "malformed": True,
        })
        return empty, metadata
    metadata["count"] = len(parsed)
    bounded, truncated = _bounded_json_value(parsed)
    metadata["truncated"] = truncated
    if not isinstance(bounded, expected_type):
        metadata.update({
            "count": None,
            "truncated": True,
            "malformed": True,
        })
        return empty, metadata
    return bounded, metadata


def _bounded_json_object(value: str | None) -> tuple[dict, bool]:
    bounded, metadata = _bounded_json_field(
        value,
        expected_type=dict,
        empty={},
    )
    return bounded if isinstance(bounded, dict) else {}, metadata["truncated"]


def _bounded_text(value: object, limit: int = 512) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _semantic_agent_name(
    run: AgentRun,
    spawn_payload: dict,
    task: TaskItem | None,
) -> str:
    """Return a stable role/step name without inventing domain semantics."""

    persisted_name = str(run.agent_name or "").strip()
    if persisted_name and not _GENERIC_AGENT_NAME_RE.fullmatch(persisted_name):
        return persisted_name

    for key in ("role_hint", "worker_id", "step_id"):
        candidate = str(spawn_payload.get(key) or "").strip()
        if candidate:
            return _bounded_text(candidate, 128)

    task_title = str(task.title or "").strip() if task is not None else ""
    if task_title and not _GENERIC_AGENT_NAME_RE.fullmatch(task_title):
        return _bounded_text(task_title, 128)

    goal = str(spawn_payload.get("goal") or "").strip()
    if goal:
        return _bounded_text(goal, 128)
    return persisted_name or str(run.agent_kind or "agent")


def _explicit_recovery(payload: dict) -> tuple[bool, str]:
    """Recognize only explicit recovery evidence already emitted by Harness."""

    if payload.get("recovered") is True:
        return True, _bounded_text(
            payload.get("recovery_reason") or "recovered",
        )
    try:
        recovery_count = int(payload.get("recovery_count") or 0)
    except (TypeError, ValueError):
        recovery_count = 0
    if recovery_count > 0:
        return True, _bounded_text(
            payload.get("recovery_reason") or f"recovery_count={recovery_count}",
        )
    for key in (
        "recovery_reason",
        "terminal_reason",
        "runtime_finish_reason",
        "finish_reason",
    ):
        value = str(payload.get(key) or "").strip()
        if value and ("recover" in value.casefold() or "salvage" in value.casefold()):
            return True, _bounded_text(value)
    warning = str(payload.get("runtime_warning") or "").strip()
    if warning and ("recover" in warning.casefold() or "salvage" in warning.casefold()):
        return True, _bounded_text(warning)
    return False, ""


def _unresolved_retrieval_affects_completion_quality(value: object) -> bool:
    """Keep legacy receipts fail-closed; only explicit advisory is neutral."""

    if value is None or value is False:
        return False
    return not (
        isinstance(value, dict)
        and value.get("quality_impact") == "advisory"
    )


def _event_payload(event: AgentRunEvent) -> dict:
    if not event.payload:
        return {}
    try:
        payload = json.loads(event.payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _tool_summaries(
    events: list[AgentRunEvent],
) -> tuple[list[dict], int, bool]:
    """Return distinct tool attempts without claiming unrelated recovery.

    A later successful call to the same tool may target a different endpoint
    or logical operation.  Preserve every call ID and keep the failed attempt
    failed unless the Harness emits explicit recovery linkage.
    """

    tools: dict[str, dict] = {}
    attempts_by_name: dict[str, int] = defaultdict(int)
    for order, event in enumerate(events):
        event_type = str(event.event_type or "")
        if event_type not in {"tool.started", "tool.completed", "tool.failed"}:
            continue
        payload = _event_payload(event)
        name = str(event.tool_name or payload.get("tool_name") or "").strip()
        if not name:
            name = "tool"
        call_id = str(
            event.tool_call_id
            or payload.get("tool_call_id")
            or ""
        ).strip()
        key = call_id
        if not key:
            running_candidates = [
                candidate_key
                for candidate_key, candidate in tools.items()
                if (
                    candidate["name"] == name
                    and candidate["status"] == "running"
                )
            ]
            key = (
                running_candidates[-1]
                if running_candidates and event_type != "tool.started"
                else f"{name}:{event.seq}:{event.id}"
            )
        item = tools.get(key)
        if item is None:
            attempts_by_name[name] += 1
            item = {
                "name": name,
                "tool_call_id": call_id or None,
                "attempt_index": attempts_by_name[name],
                "status": "running",
                "detail": "",
                "actual_dispatch_attempted": None,
                "later_success_same_tool": False,
                "_last_order": -1,
            }
            tools[key] = item
        item["_last_order"] = order
        if event_type == "tool.started":
            if item["status"] != "running":
                item["terminal_conflict"] = True
                continue
            item["status"] = "running"
            continue
        if item["status"] != "running":
            recovered, recovery_reason = _explicit_recovery(payload)
            if event_type == "tool.completed" and recovered:
                item["status"] = "recovered"
                item["recovery_reason"] = recovery_reason
            else:
                item["terminal_conflict"] = True
            continue
        if event_type == "tool.completed":
            item["status"] = "success"
            item["actual_dispatch_attempted"] = payload.get(
                "actual_dispatch_attempted"
            )
            for previous in tools.values():
                if (
                    previous is not item
                    and previous["name"] == name
                    and previous["status"] in {"failed", "rejected"}
                    and previous["_last_order"] < order
                ):
                    previous["later_success_same_tool"] = True
            continue

        rejected = bool(
            payload.get("actual_dispatch_attempted") is False
            or payload.get("actual_dispatch") is False
            or str(payload.get("outcome") or "").casefold()
            in {"rejected", "preflight_rejected", "not_dispatched"}
        )
        if rejected:
            item["status"] = "rejected"
        else:
            item["status"] = "failed"
        item["actual_dispatch_attempted"] = payload.get(
            "actual_dispatch_attempted"
        )
        item["detail"] = _bounded_text(
            payload.get("error")
            or payload.get("detail")
            or payload.get("reason")
            or payload.get("failure_class")
            or payload.get("terminal_reason"),
        )

    ordered = sorted(tools.values(), key=lambda item: item["_last_order"])
    for item in ordered:
        item.pop("_last_order", None)
    total = len(ordered)
    return (
        ordered[-_RUN_CARD_TOOL_ATTEMPT_LIMIT:],
        total,
        total > _RUN_CARD_TOOL_ATTEMPT_LIMIT,
    )


def _run_card(
    run: AgentRun,
    events: list[AgentRunEvent],
    tasks: list[TaskItem],
    artifacts: list[Artifact],
    *,
    artifact_count: int | None = None,
) -> dict:
    spawn_payload: dict = {}
    terminal_candidates: list[tuple[str, dict]] = []
    verifier: dict | None = None
    for event in events:
        payload = _event_payload(event)
        if event.event_type == "agent.spawned" and not spawn_payload:
            spawn_payload = payload
        if (
            event.event_type in {"run.completed", "run.failed", "run.cancelled"}
            and payload.get("authoritative") is not False
            and payload.get("provisional_terminal") is not True
        ):
            terminal_candidates.append((event.event_type, payload))
        if event.event_type in {"verifier.completed", "verifier.failed"}:
            verifier = {
                "status": (
                    "failed"
                    if event.event_type == "verifier.failed"
                    else str(payload.get("verdict") or "inconclusive")
                ),
                "reason": _bounded_text(
                    payload.get("reason") or payload.get("error"),
                    1000,
                ),
            }

    expected_terminal_type = {
        "succeeded": "run.completed",
        "failed": "run.failed",
        "cancelled": "run.cancelled",
    }.get(str(run.status or "").casefold())
    first_terminal_type, terminal_payload = (
        terminal_candidates[0]
        if terminal_candidates
        else (None, {})
    )
    terminal_projection_conflict = bool(
        first_terminal_type
        and expected_terminal_type
        and first_terminal_type != expected_terminal_type
    )
    terminal_event_conflict = bool(
        first_terminal_type
        and any(
            event_type != first_terminal_type
            for event_type, _payload in terminal_candidates[1:]
        )
    )

    run_task = next(
        (task for task in tasks if task.kind != "verification"),
        tasks[0] if tasks else None,
    )
    verifier_task = next(
        (task for task in reversed(tasks) if task.kind == "verification"),
        None,
    )
    if verifier is None and verifier_task is not None:
        verifier = {
            "status": verifier_task.status,
            "reason": _bounded_text(
                verifier_task.summary or verifier_task.error,
                1000,
            ),
        }

    completion_quality = str(
        terminal_payload.get("completion_quality") or ""
    ).strip().casefold()
    if (
        not completion_quality
        and _unresolved_retrieval_affects_completion_quality(
            terminal_payload.get("unresolved_retrieval")
        )
    ):
        completion_quality = "degraded"
    recovered, recovery_reason = _explicit_recovery(terminal_payload)
    persisted_status = str(run.status or "running").casefold()
    active = persisted_status in _ACTIVE_RUN_STATUSES
    if active:
        lifecycle_status = "running"
    elif first_terminal_type == "run.cancelled":
        lifecycle_status = "cancelled"
    elif first_terminal_type == "run.failed":
        lifecycle_status = "failed"
    elif (
        first_terminal_type == "run.completed"
        and completion_quality == "degraded"
    ):
        lifecycle_status = "degraded"
    elif first_terminal_type == "run.completed" and recovered:
        lifecycle_status = "recovered"
    elif first_terminal_type == "run.completed":
        lifecycle_status = "succeeded"
    elif persisted_status == "cancelled":
        lifecycle_status = "cancelled"
    elif persisted_status == "failed":
        lifecycle_status = "failed"
    else:
        lifecycle_status = "succeeded"

    run_dto = _run_to_dict(run)
    status_reason = (
        terminal_payload.get("terminal_reason")
        or terminal_payload.get("cancellation_reason")
        or terminal_payload.get("cancellation_source")
        or terminal_payload.get("failure_class")
        or terminal_payload.get("finish_reason")
        or (run_task.summary if run_task is not None else "")
        or run.finish_reason
    )
    tools, tool_attempt_count, tool_attempts_truncated = _tool_summaries(
        events
    )
    artifact_total = (
        max(0, int(artifact_count))
        if artifact_count is not None
        else len(artifacts)
    )
    visible_artifacts = artifacts[-_RUN_CARD_ARTIFACT_LIMIT:]
    return {
        **run_dto,
        "display_name": _semantic_agent_name(run, spawn_payload, run_task),
        "scheduler_name": run.agent_name,
        "active": active,
        "lifecycle_status": lifecycle_status,
        "completion_quality": completion_quality or None,
        "recovered": recovered,
        "recovery_reason": recovery_reason or None,
        "cancellation_source": (
            terminal_payload.get("cancellation_source") or None
        ),
        "failure_class": terminal_payload.get("failure_class") or None,
        "retryable": (
            terminal_payload.get("retryable")
            if isinstance(terminal_payload.get("retryable"), bool)
            else None
        ),
        "status_reason": _bounded_text(status_reason, 1000) or None,
        "terminal_projection_conflict": terminal_projection_conflict,
        "terminal_event_conflict": terminal_event_conflict,
        # Keep AgentRun fields identical between /runs and /run-cards.
        # Task failures remain available in the separately bounded task DTO.
        "error": run_dto["error"],
        "goal": _bounded_text(spawn_payload.get("goal"), 1000) or None,
        "worker_id": spawn_payload.get("worker_id"),
        "workflow_stage": spawn_payload.get("workflow_stage"),
        "step_type": spawn_payload.get("step_type"),
        "step_id": spawn_payload.get("step_id"),
        "delegation_batch_id": (
            spawn_payload.get("delegation_batch_id")
            or run.delegation_tool_call_id
        ),
        "delegation_slot": spawn_payload.get("delegation_slot"),
        "delegation_batch_size": spawn_payload.get("delegation_batch_size"),
        "tools": tools,
        "tool_attempt_count": tool_attempt_count,
        "tool_attempts_truncated": tool_attempts_truncated,
        "artifacts": [
            _artifact_to_dict(artifact)
            for artifact in visible_artifacts
        ],
        "artifact_count": artifact_total,
        "artifacts_truncated": (
            artifact_total > len(visible_artifacts)
        ),
        "verifier": verifier,
        "task": _task_to_dict(run_task) if run_task is not None else None,
    }


async def _run_turn_mappings(
    cid: str,
    root_runs: list[AgentRun],
    db,
) -> dict[str, dict]:
    """Map chat roots only when their trigger/assistant turn is unambiguous."""

    message_rows = (await db.execute(
        select(
            Message.id,
            Message.role,
            Message.source,
            Message.created_at,
        )
        .where(
            Message.conversation_id == cid,
            Message.role.in_(("user", "assistant")),
        )
        .order_by(Message.created_at, Message.id)
    )).all()
    mappings: dict[str, dict] = {}
    for root in root_runs:
        if root.source != "chat" or root.started_at is None:
            mappings[root.id] = {
                "trigger_message_id": None,
                "assistant_message_id": None,
                "mapping_status": "not_chat",
            }
            continue
        exact_users = [
            (index, row)
            for index, row in enumerate(message_rows)
            if (
                row.role == "user"
                and row.source == "chat"
                and row.created_at == root.started_at
            )
        ]
        if len(exact_users) != 1:
            mappings[root.id] = {
                "trigger_message_id": None,
                "assistant_message_id": None,
                "mapping_status": (
                    "ambiguous_trigger" if exact_users else "unmapped"
                ),
            }
            continue
        trigger_index, trigger = exact_users[0]
        assistants = []
        for row in message_rows[trigger_index + 1:]:
            if row.role == "user":
                break
            if row.role == "assistant" and row.source == "chat":
                assistants.append(row)
        mappings[root.id] = {
            "trigger_message_id": trigger.id,
            "assistant_message_id": (
                assistants[0].id if len(assistants) == 1 else None
            ),
            "mapping_status": (
                "exact"
                if len(assistants) == 1
                else "exact_no_assistant"
                if not assistants
                else "ambiguous_assistant"
            ),
        }
    return mappings


def _ranked_event_anchor_ids(
    cid: str,
    run_ids: set[str],
    event_types: tuple[str, ...],
    *,
    newest: bool = False,
    authoritative_terminal: bool = False,
):
    """Select one bounded lifecycle anchor per run.

    The main event page intentionally keeps only the newest global events.
    Lifecycle cards additionally need the first spawn, first authoritative
    terminal, and latest verifier result for every returned run.  A windowed
    ID subquery guarantees those anchors without loading an unbounded event
    history into Python.
    """

    order = (
        (
            desc(AgentRunEvent.seq),
            desc(AgentRunEvent.event_time),
            desc(AgentRunEvent.id),
        )
        if newest
        else (
            AgentRunEvent.seq,
            AgentRunEvent.event_time,
            AgentRunEvent.id,
        )
    )
    query = select(
        AgentRunEvent.id.label("event_id"),
        func.row_number().over(
            partition_by=AgentRunEvent.run_id,
            order_by=order,
        ).label("event_rank"),
    ).where(
        AgentRunEvent.conversation_id == cid,
        AgentRunEvent.run_id.in_(run_ids),
        AgentRunEvent.event_type.in_(event_types),
    )
    if authoritative_terminal:
        # AgentRunEvent stores the normalized lifecycle payload.  Invalid
        # legacy JSON is projected as an empty object elsewhere, so it retains
        # the historical default-authoritative meaning while never reaching
        # json_extract directly.
        valid_json = func.json_valid(
            func.coalesce(AgentRunEvent.payload, "{}")
        ) == 1
        authoritative = case(
            (
                valid_json,
                func.json_extract(
                    AgentRunEvent.payload,
                    "$.authoritative",
                ),
            ),
            else_=None,
        )
        provisional = case(
            (
                valid_json,
                func.json_extract(
                    AgentRunEvent.payload,
                    "$.provisional_terminal",
                ),
            ),
            else_=None,
        )
        query = query.where(
            or_(authoritative.is_(None), authoritative != 0),
            or_(provisional.is_(None), provisional != 1),
        )
    ranked = query.subquery()
    return select(ranked.c.event_id).where(ranked.c.event_rank == 1)


async def _run_card_anchor_events(
    db,
    cid: str,
    run_ids: set[str],
) -> list[AgentRunEvent]:
    if not run_ids:
        return []
    anchor_id_queries = (
        _ranked_event_anchor_ids(
            cid,
            run_ids,
            ("agent.spawned",),
        ),
        _ranked_event_anchor_ids(
            cid,
            run_ids,
            ("run.completed", "run.failed", "run.cancelled"),
            authoritative_terminal=True,
        ),
        _ranked_event_anchor_ids(
            cid,
            run_ids,
            ("verifier.completed", "verifier.failed"),
            newest=True,
        ),
    )
    anchor_ids: set[str] = set()
    for query in anchor_id_queries:
        anchor_ids.update(
            str(row[0])
            for row in (await db.execute(query)).all()
            if row[0]
        )
    if not anchor_ids:
        return []
    return list((await db.execute(
        select(AgentRunEvent).where(
            AgentRunEvent.conversation_id == cid,
            AgentRunEvent.id.in_(anchor_ids),
        )
    )).scalars().all())


@router.get("/{cid}/runs")
async def list_runs(
    cid: str,
    limit: int = Query(200, ge=1, le=500),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    latest_runs = (await db.execute(
        select(AgentRun)
        .where(AgentRun.conversation_id == cid, AgentRun.user_id == user.id)
        .order_by(desc(AgentRun.started_at), desc(AgentRun.id))
        .limit(limit)
    )).scalars().all()
    active_runs = (await db.execute(
        select(AgentRun).where(
            AgentRun.conversation_id == cid,
            AgentRun.user_id == user.id,
            AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
        )
        .order_by(desc(AgentRun.started_at), desc(AgentRun.id))
        .limit(_RUN_CARD_MAX_ACTIVE_RUNS + 1)
    )).scalars().all()
    global_has_active_runs = bool(active_runs)
    active_runs_truncated = len(active_runs) > _RUN_CARD_MAX_ACTIVE_RUNS
    active_runs = active_runs[:_RUN_CARD_MAX_ACTIVE_RUNS]
    run_by_id = {
        run.id: run
        for run in [*latest_runs, *active_runs]
    }
    # A page may contain descendants whose root/parent is older than the page.
    # Fetch only the missing lineage, instead of returning the oldest page or
    # silently orphaning an active/latest child.
    for _ in range(16):
        missing_lineage_ids = {
            lineage_id
            for run in run_by_id.values()
            for lineage_id in (run.parent_run_id, run.root_run_id)
            if lineage_id and lineage_id not in run_by_id
        }
        if not missing_lineage_ids:
            break
        lineage = (await db.execute(
            select(AgentRun).where(
                AgentRun.conversation_id == cid,
                AgentRun.user_id == user.id,
                AgentRun.id.in_(missing_lineage_ids),
            )
        )).scalars().all()
        if not lineage:
            break
        run_by_id.update({run.id: run for run in lineage})
    runs = sorted(
        run_by_id.values(),
        key=lambda run: (run.started_at, run.id),
    )
    nodes = {run.id: _run_to_dict(run) for run in runs}
    roots: list[dict] = []
    for run in runs:
        node = nodes[run.id]
        parent_id = run.parent_run_id
        if parent_id and parent_id in nodes:
            nodes[parent_id]["children"].append(node)
        else:
            roots.append(node)
    flat = sorted(nodes.values(), key=lambda item: item.get("started_at") or "", reverse=True)
    dto_truncated_count = sum(
        1 for item in flat if item.get("dto_truncated")
    )
    return {
        "runs": flat,
        "tree": roots,
        "requested_limit": limit,
        "returned_count": len(flat),
        "has_active_runs": (
            global_has_active_runs or active_runs_truncated
        ),
        "active_runs_truncated": active_runs_truncated,
        "dto_truncated_count": dto_truncated_count,
        "projection_truncated": {
            "active_runs": active_runs_truncated,
            "run_dtos": dto_truncated_count > 0,
        },
    }


@router.get("/{cid}/run-cards")
async def list_run_cards(
    cid: str,
    root_limit: int = Query(20, ge=1, le=50),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Return a bounded, refresh-safe projection for chat AgentRun cards."""

    await _conversation(cid, user.id, db)
    latest_roots = (await db.execute(
        select(AgentRun)
        .where(
            AgentRun.conversation_id == cid,
            AgentRun.user_id == user.id,
            AgentRun.parent_run_id.is_(None),
        )
        .order_by(desc(AgentRun.started_at), desc(AgentRun.id))
        .limit(root_limit)
    )).scalars().all()
    active_runs = (await db.execute(
        select(AgentRun).where(
            AgentRun.conversation_id == cid,
            AgentRun.user_id == user.id,
            AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
        )
        .order_by(desc(AgentRun.started_at), desc(AgentRun.id))
        .limit(_RUN_CARD_MAX_ACTIVE_RUNS + 1)
    )).scalars().all()
    global_has_active_runs = bool(active_runs)
    active_runs_truncated = len(active_runs) > _RUN_CARD_MAX_ACTIVE_RUNS
    active_runs = active_runs[:_RUN_CARD_MAX_ACTIVE_RUNS]
    active_root_ids = {
        str(run.root_run_id or run.id)
        for run in active_runs
    }
    root_by_id = {run.id: run for run in latest_roots}
    missing_active_roots = active_root_ids.difference(root_by_id)
    if missing_active_roots:
        active_roots = (await db.execute(
            select(AgentRun).where(
                AgentRun.conversation_id == cid,
                AgentRun.user_id == user.id,
                AgentRun.id.in_(missing_active_roots),
            )
        )).scalars().all()
        root_by_id.update({
            run.id: run
            for run in active_roots
            if run.parent_run_id is None
        })
    orphan_active_root_ids = active_root_ids.difference(root_by_id)
    orphan_representatives: dict[str, AgentRun] = {}
    for run in active_runs:
        group_id = str(run.root_run_id or run.id)
        if group_id in orphan_active_root_ids:
            orphan_representatives.setdefault(group_id, run)
    root_runs = sorted(
        root_by_id.values(),
        key=lambda run: (run.started_at, run.id),
        reverse=True,
    )
    if not root_runs and not orphan_representatives:
        return {
            "roots": [],
            "has_active_runs": (
                global_has_active_runs or active_runs_truncated
            ),
            "poll_after_ms": (
                2500
                if global_has_active_runs or active_runs_truncated
                else None
            ),
            "projection_truncated": {
                "active_runs": active_runs_truncated,
                "runs": False,
                "events": False,
                "tasks": False,
                "artifacts": False,
                "run_dtos": False,
            },
        }

    root_ids = set(root_by_id)
    selected_group_ids = root_ids.union(orphan_representatives)
    runs = (await db.execute(
        select(AgentRun)
        .where(
            AgentRun.conversation_id == cid,
            AgentRun.user_id == user.id,
            or_(
                AgentRun.id.in_(root_ids),
                AgentRun.root_run_id.in_(selected_group_ids),
            ),
        )
        .order_by(desc(AgentRun.started_at), desc(AgentRun.id))
        .limit(_RUN_CARD_MAX_RUNS + 1)
    )).scalars().all()
    runs_truncated = len(runs) > _RUN_CARD_MAX_RUNS
    runs = runs[:_RUN_CARD_MAX_RUNS]
    runs_by_id = {run.id: run for run in runs}
    # Root cards remain present even when an abnormally broad descendant tree
    # reaches the defensive cap.  Active rows are also global truth and cannot
    # disappear merely because a large terminal history filled the page.
    runs_by_id.update({run.id: run for run in root_runs})
    runs_by_id.update({
        run.id: run
        for run in active_runs
        if str(run.root_run_id or run.id) in selected_group_ids
    })
    runs = sorted(
        runs_by_id.values(),
        key=lambda run: (run.started_at, run.id),
    )
    run_ids = {run.id for run in runs}
    latest_events = (await db.execute(
        select(AgentRunEvent)
        .where(
            AgentRunEvent.conversation_id == cid,
            AgentRunEvent.run_id.in_(run_ids),
            AgentRunEvent.event_type.in_(_RUN_CARD_EVENT_TYPES),
        )
        .order_by(
            desc(AgentRunEvent.event_time),
            desc(AgentRunEvent.id),
        )
        .limit(_RUN_CARD_MAX_EVENTS + 1)
    )).scalars().all()
    events_truncated = len(latest_events) > _RUN_CARD_MAX_EVENTS
    latest_events = latest_events[:_RUN_CARD_MAX_EVENTS]
    anchor_events = await _run_card_anchor_events(
        db,
        cid,
        run_ids,
    )
    events_by_id = {
        event.id: event
        for event in [*latest_events, *anchor_events]
    }
    events = list(events_by_id.values())
    tasks = (await db.execute(
        select(TaskItem)
        .where(
            TaskItem.conversation_id == cid,
            TaskItem.user_id == user.id,
            or_(
                TaskItem.run_id.in_(run_ids),
                TaskItem.root_run_id.in_(root_ids),
            ),
        )
        .order_by(desc(TaskItem.updated_at), desc(TaskItem.id))
        .limit(_RUN_CARD_MAX_TASKS + 1)
    )).scalars().all()
    tasks_truncated = len(tasks) > _RUN_CARD_MAX_TASKS
    tasks = tasks[:_RUN_CARD_MAX_TASKS]
    artifacts = (await db.execute(
        select(Artifact)
        .where(
            Artifact.conversation_id == cid,
            Artifact.user_id == user.id,
            or_(
                Artifact.run_id.in_(run_ids),
                Artifact.root_run_id.in_(root_ids),
            ),
        )
        .order_by(desc(Artifact.created_at), desc(Artifact.id))
        .limit(_RUN_CARD_MAX_ARTIFACTS + 1)
    )).scalars().all()
    artifacts_truncated = len(artifacts) > _RUN_CARD_MAX_ARTIFACTS
    artifacts = artifacts[:_RUN_CARD_MAX_ARTIFACTS]
    artifact_counts = {
        str(run_id): int(count or 0)
        for run_id, count in (await db.execute(
            select(Artifact.run_id, func.count(Artifact.id))
            .where(
                Artifact.conversation_id == cid,
                Artifact.user_id == user.id,
                Artifact.run_id.in_(run_ids),
            )
            .group_by(Artifact.run_id)
        )).all()
    }

    events_by_run: dict[str, list[AgentRunEvent]] = defaultdict(list)
    tasks_by_run: dict[str, list[TaskItem]] = defaultdict(list)
    artifacts_by_run: dict[str, list[Artifact]] = defaultdict(list)
    runs_by_root: dict[str, list[AgentRun]] = defaultdict(list)
    for event in events:
        events_by_run[event.run_id].append(event)
    for task in tasks:
        tasks_by_run[task.run_id].append(task)
    for artifact in artifacts:
        artifacts_by_run[artifact.run_id].append(artifact)
    for run in runs:
        group_id = (
            run.id
            if run.id in root_ids
            else str(run.root_run_id or run.id)
        )
        if group_id in selected_group_ids:
            runs_by_root[group_id].append(run)
    for run_events in events_by_run.values():
        run_events.sort(
            key=lambda event: (
                int(event.seq or 0),
                str(event.event_time or ""),
                event.id,
            )
        )
    for run_tasks in tasks_by_run.values():
        run_tasks.sort(
            key=lambda task: (
                str(task.updated_at or ""),
                task.id,
            )
        )
    for run_artifacts in artifacts_by_run.values():
        run_artifacts.sort(
            key=lambda artifact: (
                str(artifact.created_at or ""),
                artifact.id,
            )
        )

    mappings = await _run_turn_mappings(cid, root_runs, db)
    root_payloads = []
    run_dtos_truncated = False
    run_dto_truncated_count = 0
    group_descriptors = [
        (root.id, root, False)
        for root in root_runs
    ] + [
        (group_id, representative, True)
        for group_id, representative in orphan_representatives.items()
    ]
    group_descriptors.sort(
        key=lambda item: (item[1].started_at, item[1].id),
        reverse=True,
    )
    for group_id, root, orphaned_root in group_descriptors:
        cards = [
            _run_card(
                run,
                events_by_run.get(run.id, []),
                tasks_by_run.get(run.id, []),
                artifacts_by_run.get(run.id, []),
                artifact_count=artifact_counts.get(run.id, 0),
            )
            for run in runs_by_root.get(group_id, [root])
        ]
        root_card = next(
            (card for card in cards if card["id"] == root.id),
            _run_card(
                root,
                [],
                [],
                [],
                artifact_count=artifact_counts.get(root.id, 0),
            ),
        )
        group_active = any(card["active"] for card in cards)
        group_dto_truncated_count = sum(
            1 for card in cards if card.get("dto_truncated")
        )
        run_dto_truncated_count += group_dto_truncated_count
        run_dtos_truncated = (
            run_dtos_truncated or group_dto_truncated_count > 0
        )
        root_payloads.append({
            "root_run_id": group_id,
            "status": root_card["lifecycle_status"],
            "active": group_active,
            "started_at": str(root.started_at),
            "ended_at": str(root.ended_at) if root.ended_at else None,
            "orphaned_root": orphaned_root,
            **(
                {
                    "trigger_message_id": None,
                    "assistant_message_id": None,
                    "mapping_status": "orphaned_root",
                }
                if orphaned_root
                else mappings.get(root.id, {
                    "trigger_message_id": None,
                    "assistant_message_id": None,
                    "mapping_status": "unmapped",
                })
            ),
            "runs": cards,
        })
    return {
        "roots": root_payloads,
        # Activity is a conversation-global fact, not an inference from the
        # currently visible root page.  A missing root record or defensive
        # projection cap must never unlock a conflicting new turn.
        "has_active_runs": (
            global_has_active_runs or active_runs_truncated
        ),
        "poll_after_ms": (
            2500
            if global_has_active_runs or active_runs_truncated
            else None
        ),
        "orphan_active_root_count": len(orphan_representatives),
        "run_dto_truncated_count": run_dto_truncated_count,
        "projection_truncated": {
            "active_runs": active_runs_truncated,
            "runs": runs_truncated,
            "events": events_truncated,
            "tasks": tasks_truncated,
            "artifacts": artifacts_truncated,
            "run_dtos": run_dtos_truncated,
        },
    }


@router.get("/{cid}/runs/{run_id}")
async def get_run(
    cid: str,
    run_id: str,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    run = (await db.execute(
        select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.conversation_id == cid,
            AgentRun.user_id == user.id,
        )
    )).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Run not found")
    return _run_to_dict(run)


@router.get("/{cid}/runs/{run_id}/events")
async def get_run_events(
    cid: str,
    run_id: str,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    await _conversation(cid, user.id, db)
    run = (await db.execute(
        select(AgentRun.id).where(
            AgentRun.id == run_id,
            AgentRun.conversation_id == cid,
            AgentRun.user_id == user.id,
        )
    )).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Run not found")
    events = (await db.execute(
        select(AgentRunEvent)
        .where(AgentRunEvent.run_id == run_id, AgentRunEvent.conversation_id == cid)
        .order_by(AgentRunEvent.seq, AgentRunEvent.event_time)
        .offset(offset)
        .limit(limit)
    )).scalars().all()
    return {
        "events": [
            {
                "id": event.id,
                "run_id": event.run_id,
                "parent_run_id": event.parent_run_id,
                "seq": event.seq,
                "event_type": event.event_type,
                "payload": json.loads(event.payload) if event.payload else {},
                "tool_name": event.tool_name,
                "tool_call_id": event.tool_call_id,
                "event_time": str(event.event_time),
            }
            for event in events
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/{cid}/runs/{run_id}/native-events")
async def get_native_run_events(
    cid: str,
    run_id: str,
    limit: int = Query(200, ge=1, le=1000),
    after: int = Query(0, ge=0),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Return the lossless/native engine ledger index for debugging."""

    await _conversation(cid, user.id, db)
    run = (await db.execute(
        select(AgentRun.id).where(
            AgentRun.id == run_id,
            AgentRun.conversation_id == cid,
            AgentRun.user_id == user.id,
        )
    )).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Run not found")
    events = (await db.execute(
        select(AgentEngineRawEvent)
        .where(
            AgentEngineRawEvent.run_id == run_id,
            AgentEngineRawEvent.conversation_id == cid,
            AgentEngineRawEvent.seq > after,
        )
        .order_by(AgentEngineRawEvent.seq)
        .limit(limit)
    )).scalars().all()
    return {
        "events": [
            {
                "seq": event.seq,
                "engine_id": event.engine_id,
                "native_event_id": event.native_event_id,
                "native_event_type": event.native_event_type,
                "payload": json.loads(event.payload),
                "payload_sha256": event.payload_sha256,
                "received_at": str(event.received_at),
            }
            for event in events
        ],
        "limit": limit,
        "after": after,
    }


@router.get("/{cid}/activity-events")
async def get_turn_activity_events(
    cid: str,
    root_run_id: str | None = Query(default=None, max_length=32),
    after: int = Query(default=0, ge=0),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=2000),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Replay the same safe ordered DTO used by the live SSE stream."""

    await _conversation(cid, user.id, db)
    if root_run_id is None and after:
        raise HTTPException(
            400,
            "The after cursor is root-run scoped; use offset for a Session replay",
        )
    query = select(TurnActivityEvent).where(
        TurnActivityEvent.conversation_id == cid,
        TurnActivityEvent.user_id == user.id,
    )
    if root_run_id:
        owned = (await db.execute(
            select(AgentRun.id).where(
                AgentRun.id == root_run_id,
                AgentRun.conversation_id == cid,
                AgentRun.user_id == user.id,
            )
        )).scalar_one_or_none()
        if owned is None:
            raise HTTPException(404, "Run not found")
        query = query.where(
            TurnActivityEvent.root_run_id == root_run_id,
            TurnActivityEvent.seq > after,
        )
    ordered = query.order_by(
            TurnActivityEvent.event_time,
            TurnActivityEvent.root_run_id,
            TurnActivityEvent.seq,
        )
    if not root_run_id:
        ordered = ordered.offset(offset)
    rows = (await db.execute(ordered.limit(limit + 1))).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "events": [
            {
                "schema": "chatds.turn-activity.v1",
                "event_id": row.id,
                "conversation_id": row.conversation_id,
                "root_run_id": row.root_run_id,
                "run_id": row.run_id,
                "seq": row.seq,
                "node_id": row.node_id,
                "kind": row.kind,
                "operation": row.operation,
                "payload": json.loads(row.payload),
                "event_time": str(row.event_time),
            }
            for row in rows
        ],
        "has_more": has_more,
        "next_after": rows[-1].seq if root_run_id and rows else None,
        "next_offset": (
            offset + len(rows) if not root_run_id and rows else None
        ),
    }


@router.post("/{cid}/runs/{run_id}/approvals/{request_id}")
async def decide_turn_approval(
    cid: str,
    run_id: str,
    request_id: str,
    payload: ApprovalDecision,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Resolve one native Claude permission request, once and fail-closed."""

    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
        raise HTTPException(400, "Invalid approval request id")
    conv = await _conversation(cid, user.id, db)
    run = (await db.execute(
        select(AgentRun).where(
            AgentRun.id == run_id,
            AgentRun.conversation_id == cid,
            AgentRun.user_id == user.id,
        )
    )).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Run not found")
    if conv.permission_preset != "workspace_write":
        raise HTTPException(
            409,
            "This Session permission preset does not accept interactive decisions",
        )
    rows = (await db.execute(
        select(TurnActivityEvent).where(
            TurnActivityEvent.conversation_id == cid,
            TurnActivityEvent.root_run_id == run_id,
            TurnActivityEvent.kind == "approval",
        ).order_by(TurnActivityEvent.seq)
    )).scalars().all()
    requested = None
    terminal_status = None
    for row in rows:
        value = json.loads(row.payload)
        if value.get("request_id") != request_id:
            continue
        if value.get("status") == "pending":
            requested = value
        elif value.get("status") in {"allowed", "denied"}:
            terminal_status = value.get("status")
    expected_status = "allowed" if payload.decision == "allow" else "denied"
    if terminal_status is not None:
        if terminal_status != expected_status:
            raise HTTPException(409, "Approval request already has another decision")
        return {"accepted": True, "idempotent": True, "status": terminal_status}
    if requested is None or requested.get("request_seq") != payload.request_seq:
        raise HTTPException(409, "Approval request is stale or not durable")
    if run.status not in {"queued", "running", "committing"}:
        raise HTTPException(409, "Run is no longer active")
    from agent_engines.base import AgentEngineError
    from agent_engines.registry import build_agent_engine_registry

    registry = build_agent_engine_registry()
    engine = registry.get(run.engine_id)
    if not hasattr(engine, 'decide_approval'):
        raise HTTPException(409, "Run engine does not support native approvals")
    try:
        result = await engine.decide_approval(
            user_id=str(user.id),
            conversation_id=cid,
            run_id=run_id,
            request_id=request_id,
            request_seq=payload.request_seq,
            decision=payload.decision,
        )
    except AgentEngineError as exc:
        raise HTTPException(503, str(exc)) from exc
    return result


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
    runs_payload = await list_runs(cid, 200, user, db)
    artifacts_payload = await list_artifacts(cid, None, 500, user, db)
    tasks_payload = await list_tasks(cid, 1000, user, db)
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
        "schema": "chat-ds-session-trajectory-v2",
        "conversation": {
            "id": conv.id,
            "title": conv.title,
            "model_id": conv.model_id,
            "created_at": str(conv.created_at),
            "updated_at": str(conv.updated_at),
        },
        "settings": {
            "enabled_tools": serialize_json_list(conv.enabled_tools, DEFAULT_NATIVE_TOOLS),
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
        "runs": redact_trajectory_value(runs_payload.get("runs", [])),
        "run_tree": redact_trajectory_value(runs_payload.get("tree", [])),
        "artifacts": redact_trajectory_value(artifacts_payload.get("artifacts", [])),
        "tasks": redact_trajectory_value(tasks_payload.get("tasks", [])),
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
