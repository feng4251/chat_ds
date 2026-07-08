import json
import logging
import shutil
from pathlib import Path
import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import select, func, desc, delete
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from models import User, Conversation, Message, SkillPackage
from schemas import ConversationOut, ConversationTitle
from auth import get_current_user
from workspace import atomic_write_bytes, ensure_workspace, safe_workspace_path
from hooks import emit_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

# ── Session file upload ──────────────────────────────────────────────────

SANDBOX_BASE = Path("/nfs/temp/chat_ds")
SKILLS_DATA_DIR = Path("data/skills")
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
    conv = Conversation(user_id=cur_user.id, model_id="deepseek_v4_pro")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    ensure_workspace(cur_user.id, conv.id)
    await emit_event(
        cur_user.id, "session.created",
        {"conversation_id": conv.id}, conv.id,
    )
    return {"id": conv.id, "title": conv.title, "model_id": conv.model_id,
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

    # Save to session workspace
    try:
        dest_path = safe_workspace_path(cur_user.id, cid, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    atomic_write_bytes(dest_path, contents)

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
        except zipfile.BadZipFile:
            pass

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
            created_at=c.created_at, updated_at=c.updated_at,
            last_message=(lm.content[:100] if lm and lm.content else None),
            message_count=cnt,
        ))
    return out

@router.patch("/{cid}/title")
async def rename_conv(cid: str, data: ConversationTitle,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    conv = (await db.execute(
        select(Conversation).where(Conversation.id==cid, Conversation.user_id==cur_user.id)
    )).scalar_one_or_none()
    if not conv: raise HTTPException(404, "Not found")
    conv.title = data.title
    await db.commit()
    return {"ok": True}

@router.delete("/{cid}")
async def delete_conv(cid: str,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    conv = (await db.execute(
        select(Conversation).where(Conversation.id==cid, Conversation.user_id==cur_user.id)
    )).scalar_one_or_none()
    if not conv: raise HTTPException(404, "Not found")

    await emit_event(
        cur_user.id, "session.deleted",
        {"conversation_id": cid, "title": conv.title}, cid,
    )
    runtime_cleanup = await _cleanup_harness_session(cur_user.id, cid)
    await db.execute(
        delete(SkillPackage).where(
            SkillPackage.user_id == cur_user.id,
            SkillPackage.session_id == cid,
        )
    )
    await db.delete(conv)
    await db.commit()

    # Clean up session sandbox directories
    for sub in ["files", "sandbox", "workspace", "browser", "results"]:
        d = SANDBOX_BASE / cur_user.id / cid / sub
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    # Clean up session skills
    session_skills = SKILLS_DATA_DIR / cur_user.id / cid
    if session_skills.exists():
        shutil.rmtree(session_skills, ignore_errors=True)
    # Remove parent session dir if empty
    session_dir = SANDBOX_BASE / cur_user.id / cid
    try:
        session_dir.rmdir()
    except OSError:
        pass

    # Invalidate harness skills cache
    try:
        from skills.manager import get_manager
        get_manager().invalidate(cur_user.id)
    except Exception:
        pass

    return {"ok": True, "runtime_cleanup": runtime_cleanup}


async def _cleanup_harness_session(user_id: str, session_id: str) -> dict:
    """Best-effort teardown of MCP subprocesses and session config."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "http://harness:8020/internal/session/cleanup",
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
        "source": m.source,
        "usage": {
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "total_tokens": m.total_tokens,
        },
        "created_at": str(m.created_at),
    } for m in msgs]
