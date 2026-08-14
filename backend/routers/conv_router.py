import asyncio
import json
import logging
from pathlib import Path
import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from models import (
    AgentEngineRawEvent,
    AgentEngineSession,
    AgentRun,
    AgentRunEvent,
    TurnActivityEvent,
    Artifact,
    Conversation,
    EventHook,
    Message,
    ScheduledJob,
    ScheduledJobRun,
    SkillPackage,
    TaskItem,
    User,
)
from schemas import ConversationOut, ConversationTitle
from auth import get_current_user
from workspace import (
    atomic_write_bytes,
    ensure_workspace_async,
    publish_session_deletion_tombstone_async,
    run_session_workspace_mutation_async,
    safe_workspace_path_in_root,
)
from hooks import emit_event
from config import settings
from model_routing import DEFAULT_AGENT_MODEL_ID
from workspace_reconciler import cleanup_deleted_session_workspace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

# ── Session file upload ──────────────────────────────────────────────────

SANDBOX_BASE = Path("/nfs/temp/chat_ds")
MAX_UPLOAD_SIZE = 25 * 1024 * 1024  # 25 MB
ALLOWED_EXTENSIONS = frozenset({
    ".md", ".py", ".yaml", ".yml", ".json", ".txt",
    ".js", ".ts", ".html", ".css", ".toml", ".sh",
    ".cfg", ".ini", ".csv", ".xml", ".sql", ".zip",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".pdf", ".docx", ".xlsx", ".pptx",
})
ALLOWED_SKILL_EXTENSIONS = frozenset({
    ".md", ".py", ".yaml", ".yml", ".json", ".txt",
    ".js", ".ts", ".html", ".css", ".toml", ".sh",
    ".cfg", ".ini", ".env.example", ".csv", ".xml",
    ".sql", ".graphql", ".proto",
})
ALLOWED_SKILL_BINARY = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico"})
MAX_SKILL_FILE_SIZE = 1 * 1024 * 1024  # 1 MB per file inside zip

import io
import re
import zipfile


def _parse_frontmatter(text: str) -> dict:
    """Parse YAML frontmatter from a markdown string."""
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    if not m:
        return {}
    fm: dict = {}
    for line in m.group(1).split('\n'):
        line = line.strip()
        if ':' in line:
            k, v = line.split(':', 1)
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def _extract_skill_zip(zf: zipfile.ZipFile, target_dir: Path) -> dict | None:
    """Extract a skill zip to target_dir. Returns skill metadata or None."""
    skill_md_path = None
    for name in zf.namelist():
        norm = name.replace("\\", "/")
        parts = norm.rstrip("/").split("/")
        if parts[-1] == "SKILL.md" and len(parts) <= 2:
            skill_md_path = name
            break

    if skill_md_path is None:
        return None

    skill_md_content = zf.read(skill_md_path).decode("utf-8", errors="replace")
    fm = _parse_frontmatter(skill_md_content)
    skill_name = fm.get("name", "").strip()
    if not skill_name:
        return None

    root_parts = skill_md_path.replace("\\", "/").rstrip("/").split("/")
    zip_root = "" if len(root_parts) == 1 else "/".join(root_parts[:-1]) + "/"

    target_dir = target_dir / skill_name
    target_dir.mkdir(parents=True, exist_ok=True)

    for entry_name in zf.namelist():
        if entry_name.endswith("/"):
            continue
        if zip_root and not entry_name.startswith(zip_root):
            continue
        rel = entry_name[len(zip_root):] if zip_root else entry_name
        rel = rel.replace("\\", "/")
        if not rel:
            continue

        safe_path = (target_dir / rel).resolve()
        try:
            safe_path.relative_to(target_dir.resolve())
        except ValueError:
            logger.warning("Path traversal in skill zip: %s", rel)
            continue

        ext = Path(rel).suffix.lower()
        if ext not in ALLOWED_SKILL_EXTENSIONS and ext not in ALLOWED_SKILL_BINARY:
            continue

        file_bytes = zf.read(entry_name)
        if len(file_bytes) > MAX_SKILL_FILE_SIZE:
            continue

        if ext not in ALLOWED_SKILL_BINARY:
            try:
                file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue

        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_bytes(file_bytes)

    return {
        "name": skill_name,
        "description": fm.get("description", "").strip(),
        "version": fm.get("version", ""),
    }


@router.post("")
async def create_conversation(
    cur_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Create a new empty conversation."""
    conv = Conversation(
        user_id=cur_user.id,
        model_id=DEFAULT_AGENT_MODEL_ID,
        engine_id=settings.default_agent_engine_id,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    await ensure_workspace_async(cur_user.id, conv.id)
    await emit_event(
        cur_user.id, "session.created",
        {"conversation_id": conv.id}, conv.id,
    )
    return {"id": conv.id, "title": conv.title, "model_id": conv.model_id,
            "engine_id": conv.engine_id,
            "created_at": str(conv.created_at), "updated_at": str(conv.updated_at)}


@router.post("/{cid}/upload")
async def upload_session_file(
    cid: str,
    file: UploadFile = File(...),
    cur_user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Upload a file to a session's workspace. Zip files with SKILL.md are
    auto-extracted as session-specific skills."""
    # Verify conversation belongs to user
    conv = (await db.execute(
        select(Conversation).where(Conversation.id == cid, Conversation.user_id == cur_user.id)
    )).scalar_one_or_none()
    if not conv:
        raise HTTPException(404, "Conversation not found")

    if not file.filename:
        raise HTTPException(400, "No filename provided")
    filename = Path(file.filename).name
    if filename != file.filename or filename in {"", ".", ".."}:
        raise HTTPException(400, "Filename must not contain path components")

    # Validate extension
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"File type '{ext}' not allowed")

    # Read file
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_SIZE:
        raise HTTPException(400, f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)")

    # Resolve the attachment path before any mutation. Skill archives are
    # persisted by the canonical installer so workspace, package directories,
    # and registry rows share one rollback/cancellation boundary.
    workspace = await ensure_workspace_async(cur_user.id, cid)
    try:
        dest_path = safe_workspace_path_in_root(workspace, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    result = {
        "success": True,
        "filename": filename,
        "size": len(contents),
        "path": str(dest_path.relative_to(SANDBOX_BASE / cur_user.id / cid)),
    }

    # Auto-detect skill zip
    if ext == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(contents)) as zf:
                has_skill = any(
                    entry.replace("\\", "/").rstrip("/").split("/")[-1] == "SKILL.md"
                    for entry in zf.namelist()
                )
            if has_skill:
                # Use the canonical installer so filesystem, DB metadata, MCP
                # registration, and cache invalidation remain one transaction.
                from routers.skill_router import _process_skill_zip
                install_result = await _process_skill_zip(
                    contents, filename, None, cid, cur_user, db
                )
                result["skill"] = install_result["skill"]
                result["skills"] = install_result.get("skills", [install_result["skill"]])
                result["installed_count"] = install_result.get("installed_count", len(result["skills"]))
                result["mcp"] = install_result["mcp"]
                if result["installed_count"] == 1:
                    result["message"] = (
                        f"Skill '{install_result['skill']['name']}' installed for this session"
                    )
                else:
                    result["message"] = (
                        f"Installed {result['installed_count']} skills for this session"
                    )
                result["workspace_attachment"] = install_result.get(
                    "workspace_attachment"
                )
                return result
        except zipfile.BadZipFile:
            pass

    def _write_upload(workspace: Path) -> None:
        # Re-resolve after acquiring the shared lock; the earlier path was
        # presentation metadata, not mutation authority.
        locked_destination = safe_workspace_path_in_root(workspace, filename)
        atomic_write_bytes(locked_destination, contents)

    await run_session_workspace_mutation_async(
        cur_user.id,
        cid,
        _write_upload,
    )
    return result

@router.get("", response_model=list[ConversationOut])
async def list_convs(cur_user=Depends(get_current_user), db=Depends(get_db)):
    r = await db.execute(
        select(Conversation).where(Conversation.user_id==cur_user.id)
        .order_by(desc(Conversation.updated_at))
    )
    convs = r.scalars().all()
    out = []
    for c in convs:
        lm = (await db.execute(
            select(Message).where(Message.conversation_id==c.id)
            .order_by(desc(Message.created_at)).limit(1)
        )).scalar_one_or_none()
        cnt = (await db.execute(
            select(func.count(Message.id)).where(Message.conversation_id==c.id)
        )).scalar() or 0
        out.append(ConversationOut(
            id=c.id, title=c.title, model_id=c.model_id,
            engine_id=c.engine_id,
            created_at=c.created_at, updated_at=c.updated_at,
            last_message=(lm.content[:100] if lm and lm.content else None),
            message_count=cnt,
        ))
    return out

@router.patch("/{cid}/title")
async def rename_conv(cid: str, data: ConversationTitle,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    from session_lifecycle import session_control_plane_mutation
    from workspace import require_session_workspace_active

    async with session_control_plane_mutation(
        cur_user.id,
        cid,
    ) as (mutation_db, conv):
        conv.title = data.title
        require_session_workspace_active(cur_user.id, cid)
        await mutation_db.commit()
    return {"ok": True}

@router.delete("/{cid}")
async def delete_conv(cid: str,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    from routers import skill_router as skill_api

    async with skill_api._skill_install_lock(cur_user.id, cid):
        return await _delete_conv_locked(cid, cur_user, db, skill_api)


async def _delete_conv_locked(cid: str, cur_user, db, skill_api):
    conv = (await db.execute(
        select(Conversation).where(Conversation.id==cid, Conversation.user_id==cur_user.id)
    )).scalar_one_or_none()
    if not conv: raise HTTPException(404, "Not found")

    await emit_event(
        cur_user.id, "session.deleted",
        {"conversation_id": cid, "title": conv.title}, cid,
    )
    try:
        # ``publish_*_async`` drains its worker before re-delivering request
        # cancellation. Once durable, the marker is retained as delete intent
        # until an exact retry converges; only periodic pre-boot recovery may
        # clear an abandoned marker whose Conversation is still live.
        await publish_session_deletion_tombstone_async(cur_user.id, cid)
        from routers.chat_router import (
            cancel_conversation_producers,
            conversation_maintenance_lease,
        )

        async def _cleanup_all_session_execution():
            return await asyncio.gather(
                cancel_conversation_producers(cid),
                _cleanup_agent_runtimes(cur_user.id, cid),
                return_exceptions=True,
            )

        cleanup_values, caller_cancellation = (
            await skill_api._finish_awaitable_with_result(
                _cleanup_all_session_execution()
            )
        )
        backend_execution_cleanup, runtime_cleanup = cleanup_values
        if isinstance(backend_execution_cleanup, BaseException):
            raise backend_execution_cleanup
        if isinstance(runtime_cleanup, BaseException):
            raise runtime_cleanup
        if backend_execution_cleanup.get("success") is not True:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Conversation execution cancellation did not converge; "
                    "the session remains deletion-fenced for retry."
                ),
            )
        execution_revocation = runtime_cleanup.get(
            "execution_revocation",
        )
        if (
            not isinstance(execution_revocation, dict)
            or execution_revocation.get("success") is not True
        ):
            raise HTTPException(
                status_code=503,
                detail=(
                    "Harness session execution revocation was not proven; "
                    "the session remains deletion-fenced for retry."
                ),
            )
        if runtime_cleanup.get("success") is not True:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Harness session runtime cleanup did not converge; "
                    "the session remains deletion-fenced for retry."
                ),
            )
        if caller_cancellation is not None:
            raise caller_cancellation
        async with conversation_maintenance_lease(cid):
            scheduled_job_ids = select(ScheduledJob.id).where(
                ScheduledJob.conversation_id == cid
            )
            await db.execute(
                delete(ScheduledJobRun).where(
                    or_(
                        ScheduledJobRun.conversation_id == cid,
                        ScheduledJobRun.job_id.in_(scheduled_job_ids),
                    )
                )
            )
            # Explicit dependency order is intentional. Historic SQLite
            # databases may have been created before FK clauses existed, so
            # delete correctness never depends solely on ON DELETE CASCADE.
            for model in (
                AgentEngineRawEvent,
                AgentEngineSession,
                AgentRunEvent,
                TurnActivityEvent,
                Artifact,
                TaskItem,
                AgentRun,
                Message,
            ):
                await db.execute(
                    delete(model).where(model.conversation_id == cid)
                )
            await db.execute(
                delete(SkillPackage).where(
                    SkillPackage.user_id == cur_user.id,
                    SkillPackage.session_id == cid,
                )
            )
            await db.execute(
                delete(EventHook).where(
                    EventHook.conversation_id == cid
                )
            )
            await db.execute(
                delete(ScheduledJob).where(
                    ScheduledJob.conversation_id == cid
                )
            )
            await db.execute(
                update(Conversation)
                .where(Conversation.forked_from_conversation_id == cid)
                .values(forked_from_conversation_id=None)
            )
            deleted_conversation = await db.execute(
                delete(Conversation).where(
                    Conversation.id == cid,
                    Conversation.user_id == cur_user.id,
                )
            )
            deleted_rowcount = getattr(
                deleted_conversation,
                "rowcount",
                None,
            )
            if (
                isinstance(deleted_rowcount, int)
                and deleted_rowcount != 1
            ):
                raise RuntimeError(
                    "Conversation ownership changed before delete commit."
                )
            await db.commit()
    except BaseException:
        # The marker is a durable delete intent, not a transient request flag.
        # Keep it fail-closed after cancellation, cleanup uncertainty, or DB
        # ambiguity; an exact retry can safely resume under the lifecycle lock.
        try:
            await db.rollback()
        except BaseException:
            logger.exception(
                "Could not roll back failed session deletion user=%s "
                "session=%s",
                cur_user.id,
                cid,
            )
        raise

    # This cancellation-drained worker removes the session Skill scope first,
    # then the workspace tree and sibling lock. A crash/retry therefore never
    # loses orphan-Skill discovery by deleting the workspace coordinate first.
    workspace_cleanup_outcome = await cleanup_deleted_session_workspace(
        cur_user.id,
        cid,
    )
    workspace_cleanup = {
        "status": (
            "removed"
            if workspace_cleanup_outcome == "removed"
            else "deferred"
        ),
        "reason": (
            None
            if workspace_cleanup_outcome == "removed"
            else workspace_cleanup_outcome
        ),
    }
    # Invalidate harness skills cache
    try:
        from skills.manager import get_manager
        get_manager().invalidate(cur_user.id)
    except Exception:
        pass

    return {
        "ok": True,
        "backend_execution_cleanup": backend_execution_cleanup,
        "runtime_cleanup": runtime_cleanup,
        "workspace_cleanup": workspace_cleanup,
    }


async def _cleanup_harness_session(user_id: str, session_id: str) -> dict:
    """Teardown Legacy MCP/process/session state."""
    try:
        # Revocation can spend up to 30s draining fence-owned resources and
        # persistent process cleanup has a bounded 60s close phase. Keep the
        # Backend deadline strictly above the Harness transaction's bound.
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
            response = await client.post(
                f"{settings.harness_url}/internal/session/cleanup",
                headers={
                    "X-Internal-Token": settings.internal_api_token,
                },
                params={"user_id": user_id, "session_id": session_id},
            )
        if response.status_code < 400:
            return response.json()
        return {"success": False, "error": f"harness HTTP {response.status_code}"}
    except Exception as exc:
        logger.warning(
            "Harness session cleanup failed for user=%s session=%s: %s",
            user_id, session_id, exc,
        )
        return {"success": False, "error": str(exc)}


async def _cleanup_agent_runtimes(user_id: str, session_id: str) -> dict:
    """Revoke every configured runtime before deleting Session authority."""

    legacy = await _cleanup_harness_session(user_id, session_id)
    from agent_engines.base import (
        ENGINE_ID_CLAUDE_CODE,
        ENGINE_ID_DEEPSEEK_HARNESS,
    )
    from agent_engines.registry import build_agent_engine_registry

    native: dict[str, dict] = {}
    configured_native = (
        (ENGINE_ID_CLAUDE_CODE, settings.claude_code_engine_enabled),
        (ENGINE_ID_DEEPSEEK_HARNESS, settings.deepseek_harness_engine_enabled),
    )
    registry = build_agent_engine_registry()
    for engine_id, enabled in configured_native:
        result = {"success": True, "execution_revocation": {"success": True}}
        if enabled:
            try:
                result = dict(await registry.get(engine_id).cleanup_session(
                    user_id=user_id,
                    conversation_id=session_id,
                ))
            except Exception as exc:
                logger.warning(
                    "Native Runner session cleanup failed engine=%s user=%s session=%s type=%s",
                    engine_id, user_id, session_id, type(exc).__name__,
                )
                result = {
                    "success": False,
                    "execution_revocation": {"success": False},
                }
        native[engine_id] = result
    legacy_revocation = legacy.get("execution_revocation")
    legacy_revoked = bool(
        isinstance(legacy_revocation, dict)
        and legacy_revocation.get("success") is True
    )
    native_revoked = {
        engine_id: result.get("execution_revocation")
        for engine_id, result in native.items()
    }
    native_revocation_success = all(
        isinstance(value, dict) and value.get("success") is True
        for value in native_revoked.values()
    )
    return {
        "success": legacy.get("success") is True and all(
            result.get("success") is True for result in native.values()
        ),
        "execution_revocation": {
            "success": legacy_revoked and native_revocation_success,
            "legacy": legacy_revocation,
            **native_revoked,
        },
        "legacy": legacy,
        **native,
    }

@router.get("/{cid}/messages")
async def get_msgs(cid: str,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    conv = (await db.execute(
        select(Conversation).where(Conversation.id==cid, Conversation.user_id==cur_user.id)
    )).scalar_one_or_none()
    if not conv: raise HTTPException(404, "Not found")
    msgs = (await db.execute(
        select(Message).where(Message.conversation_id==cid).order_by(Message.created_at)
    )).scalars().all()
    return [{
        "id": m.id, "role": m.role, "content": m.content,
        "reasoning": m.reasoning,
        "tool_progress": m.tool_progress,
        "image_urls": json.loads(m.image_urls) if m.image_urls else None,
        "model_id": m.model_id,
        "run_id": m.run_id,
        "source": m.source,
        "usage": {
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "total_tokens": m.total_tokens,
        },
        "created_at": str(m.created_at),
    } for m in msgs]
