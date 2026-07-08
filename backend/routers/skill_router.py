"""Skill upload API — upload, list, and delete skill zip packages.

Endpoints:
  POST /api/skills/upload  — upload a skill zip package
  GET  /api/skills         — list installed skills
  DELETE /api/skills/{name} — delete a skill
  GET  /api/skills/optional — list optional built-in skills
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import Conversation, SkillPackage, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["skills"])

# ── Constants ─────────────────────────────────────────────────────────────────

SKILLS_DATA_DIR = Path("data/skills")
MAX_ZIP_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB per file inside zip

ALLOWED_EXTENSIONS = frozenset({
    ".md", ".py", ".yaml", ".yml", ".json", ".txt",
    ".js", ".ts", ".html", ".css", ".toml", ".sh",
    ".cfg", ".ini", ".env.example", ".csv", ".xml",
    ".sql", ".graphql", ".proto",
})

# Files inside a skill zip that shouldn't count against the text-only check
ALLOWED_BINARY_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico"})


# ── Helpers ───────────────────────────────────────────────────────────────────


def _validate_bundle_rel_path(zip_path: str, target_dir: Path) -> Path:
    """Resolve a zip entry path against target_dir, rejecting path traversal.

    Returns the resolved absolute path if safe, raises HTTPException otherwise.
    """
    resolved = (target_dir / zip_path).resolve()
    try:
        resolved.relative_to(target_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Path traversal detected: {zip_path}")
    return resolved


def _parse_frontmatter(text: str) -> dict:
    """Simple YAML frontmatter parser (regex-based fallback, no PyYAML needed)."""
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


def _validate_skill_name(name: str) -> str:
    """Validate skill name is safe for filesystem use."""
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$', name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid skill name '{name}'. Must be 1-64 chars, alphanumeric + . _ -",
        )
    return name


def _skill_dir_exists(user_id: str, name: str, session_id: str | None = None) -> bool:
    base = SKILLS_DATA_DIR / user_id
    if session_id:
        base = base / session_id
    return (base / name / "SKILL.md").is_file()


# ── Scan optional skills for listing ──────────────────────────────────────────


def _scan_optional_skills() -> list[dict]:
    """Scan the harness/skills/optional directory for available skill categories."""
    optional_dir = Path(__file__).resolve().parent.parent.parent / "harness" / "skills" / "optional"
    if not optional_dir.is_dir():
        return []

    results = []
    for category_dir in sorted(optional_dir.iterdir()):
        if not category_dir.is_dir() or category_dir.name.startswith("."):
            continue
        for skill_dir in sorted(category_dir.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")[:4000]
                fm = _parse_frontmatter(content)
                results.append({
                    "name": fm.get("name", skill_dir.name),
                    "description": fm.get("description", ""),
                    "category": category_dir.name,
                    "version": fm.get("version", ""),
                })
            except Exception:
                continue
    return results


# ── Endpoints ─────────────────────────────────────────────────────────────────


class SkillUploadJson(BaseModel):
    """JSON body for base64-encoded skill upload (bypasses DLP multipart filters)."""
    filename: str
    content_base64: str
    category: Optional[str] = None
    session_id: Optional[str] = None


def _zip_file_entries(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    entries: list[tuple[zipfile.ZipInfo, str]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        norm = info.filename.replace("\\", "/").lstrip("/")
        parts: list[str] = []
        for part in norm.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                raise HTTPException(status_code=400, detail=f"Path traversal detected: {info.filename}")
            parts.append(part)
        if parts:
            entries.append((info, "/".join(parts)))
    return entries


def _is_allowed_skill_file(rel_path: str) -> tuple[bool, bool]:
    lower = rel_path.lower()
    is_binary = any(lower.endswith(ext) for ext in ALLOWED_BINARY_EXTENSIONS)
    is_text = any(lower.endswith(ext) for ext in ALLOWED_EXTENSIONS)
    return is_text or is_binary, is_binary


def _discover_skill_manifests(
    zf: zipfile.ZipFile,
    entries: list[tuple[zipfile.ZipInfo, str]],
) -> list[dict]:
    manifests: list[dict] = []
    seen_names: dict[str, str] = {}
    for info, norm in entries:
        parts = norm.split("/")
        if parts[-1] != "SKILL.md":
            continue
        if info.file_size > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"SKILL.md too large: {norm}")

        skill_md_content = zf.read(info.filename).decode("utf-8", errors="replace")
        fm = _parse_frontmatter(skill_md_content)
        skill_name = fm.get("name", "").strip()
        if not skill_name:
            raise HTTPException(status_code=400, detail=f"{norm} frontmatter must have a 'name' field")
        _validate_skill_name(skill_name)
        if skill_name in seen_names:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate skill name '{skill_name}' in {seen_names[skill_name]} and {norm}",
            )
        seen_names[skill_name] = norm

        root = "/".join(parts[:-1])
        path_category = "/".join(parts[:-2]) or None
        manifests.append({
            "name": skill_name,
            "description": fm.get("description", "").strip(),
            "version": fm.get("version", ""),
            "root": root,
            "skill_md": norm,
            "skill_md_content": skill_md_content,
            "category": path_category,
        })

    if not manifests:
        raise HTTPException(status_code=400, detail="SKILL.md not found in zip")

    manifests.sort(key=lambda item: (item["root"].count("/"), item["root"], item["name"]))
    return manifests


def _entry_is_under_skill_root(entry_path: str, root: str) -> bool:
    return (entry_path == root or entry_path.startswith(f"{root}/")) if root else True


def _entry_is_under_nested_skill_root(entry_path: str, root: str, all_roots: list[str]) -> bool:
    for other_root in all_roots:
        if other_root == root:
            continue
        if root and not other_root.startswith(f"{root}/"):
            continue
        if _entry_is_under_skill_root(entry_path, other_root):
            return True
    return False


async def _process_skill_zip(
    contents: bytes,
    filename: str,
    category: str | None,
    session_id: str | None,
    user: User,
    db: AsyncSession,
):
    """Core logic shared by both multipart and JSON upload paths."""
    if len(contents) > MAX_ZIP_SIZE:
        raise HTTPException(status_code=400, detail=f"Zip file too large (max {MAX_ZIP_SIZE // 1024 // 1024}MB)")

    if session_id:
        conversation = (await db.execute(
            select(Conversation).where(
                Conversation.id == session_id,
                Conversation.user_id == user.id,
            )
        )).scalar_one_or_none()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

    try:
        zf = zipfile.ZipFile(io.BytesIO(contents))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file")

    created_dirs: list[Path] = []
    installed_skills: list[dict] = []
    target_dirs: dict[str, Path] = {}
    try:
        entries = _zip_file_entries(zf)
        manifests = _discover_skill_manifests(zf, entries)

        if session_id:
            user_skills_dir = SKILLS_DATA_DIR / user.id / session_id
        else:
            user_skills_dir = SKILLS_DATA_DIR / user.id

        scope_filter = (
            SkillPackage.session_id == session_id
            if session_id else SkillPackage.session_id.is_(None)
        )
        skill_names = [str(manifest["name"]) for manifest in manifests]
        skill_roots = [str(manifest["root"]) for manifest in manifests]
        existing_rows = (await db.execute(
            select(SkillPackage.name).where(
                SkillPackage.user_id == user.id,
                SkillPackage.name.in_(skill_names),
                scope_filter,
            )
        )).all()
        existing_names = {row[0] for row in existing_rows}
        existing_dirs = {
            name for name in skill_names
            if (user_skills_dir / name).exists()
        }
        conflicts = sorted(existing_names | existing_dirs)
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail=f"Skills already exist: {', '.join(conflicts)}. Delete them first to update.",
            )

        for manifest in manifests:
            skill_name = str(manifest["name"])
            root = str(manifest["root"])
            target_dir = user_skills_dir / skill_name
            target_dir.mkdir(parents=True, exist_ok=True)
            created_dirs.append(target_dir)
            target_dirs[skill_name] = target_dir

            for info, norm in entries:
                if not _entry_is_under_skill_root(norm, root):
                    continue
                if _entry_is_under_nested_skill_root(norm, root, skill_roots):
                    continue
                rel = norm[len(root):].lstrip("/") if root else norm
                if not rel:
                    continue

                allowed, is_binary = _is_allowed_skill_file(rel)
                if not allowed:
                    logger.warning("Skipping file with disallowed extension: %s", rel)
                    continue
                if info.file_size > MAX_FILE_SIZE:
                    logger.warning("Skipping oversized file (%d bytes): %s", info.file_size, rel)
                    continue

                safe_path = _validate_bundle_rel_path(rel, target_dir)
                file_bytes = zf.read(info.filename)
                if not is_binary:
                    try:
                        file_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        logger.warning("Skipping non-UTF-8 file: %s", rel)
                        continue

                safe_path.parent.mkdir(parents=True, exist_ok=True)
                safe_path.write_bytes(file_bytes)

            skill_category = category
            if skill_category is None and manifest.get("category"):
                skill_category = str(manifest["category"])[:64]

            description = str(manifest.get("description") or "")
            installed_skill = {
                "name": skill_name,
                "description": description,
                "category": skill_category,
                "version": str(manifest.get("version") or ""),
                "session_id": session_id,
            }
            installed_skills.append(installed_skill)
            db.add(SkillPackage(
                user_id=user.id,
                name=skill_name,
                session_id=session_id,
                description=description[:1024] if description else None,
                category=skill_category,
                version=str(manifest.get("version") or ""),
            ))

        await db.commit()
    except Exception:
        await db.rollback()
        import shutil
        for target_dir in created_dirs:
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
        raise
    finally:
        zf.close()

    try:
        _invalidate_skills_cache(user.id)
    except Exception:
        pass

    mcp_result = {"registered": [], "skipped": [], "errors": []}
    mcp_by_skill: dict[str, dict] = {}
    for skill in installed_skills:
        skill_name = str(skill["name"])
        try:
            skill_mcp = await _auto_register_mcp(
                str(target_dirs[skill_name]), user.id, session_id or "default"
            )
            mcp_by_skill[skill_name] = skill_mcp
            for key in ("registered", "skipped", "errors"):
                values = skill_mcp.get(key) or []
                if isinstance(values, list):
                    mcp_result[key].extend(values)
            if skill_mcp.get("registered"):
                logger.info(
                    "Auto MCP for skill '%s' (user=%s): registered=%s",
                    skill_name, user.id, skill_mcp["registered"],
                )
            if skill_mcp.get("errors"):
                logger.warning(
                    "Auto MCP for skill '%s' had errors: %s",
                    skill_name, skill_mcp["errors"],
                )
        except Exception:
            logger.exception("Auto MCP registration failed for skill '%s'", skill_name)
            error = f"Auto MCP registration failed for skill '{skill_name}'"
            mcp_result["errors"].append(error)
            mcp_by_skill[skill_name] = {"registered": [], "skipped": [], "errors": [error]}

    return {
        "success": True,
        "skill": installed_skills[0],
        "skills": installed_skills,
        "installed_count": len(installed_skills),
        "mcp": mcp_result,
        "mcp_by_skill": mcp_by_skill,
    }


@router.post("/upload")
async def upload_skill(
    file: UploadFile = File(None),
    category: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a skill zip package (multipart form).

    When session_id is provided, the skill is installed as a session-specific
    skill under data/skills/<user_id>/<session_id>/ (highest priority).
    Otherwise it's installed as a user-level skill.
    """
    if file is None:
        raise HTTPException(status_code=400, detail="No file provided")

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")

    contents = await file.read()
    return await _process_skill_zip(contents, file.filename, category, session_id, user, db)


@router.post("/upload/json")
async def upload_skill_json(
    body: SkillUploadJson,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a skill zip package via JSON (base64-encoded, bypasses DLP filters).

    Use this endpoint from browsers where multipart/form-data file uploads
    are intercepted by corporate DLP/数据防泄密 gateways.
    """
    if not body.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")

    try:
        contents = base64.b64decode(body.content_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 encoding")

    return await _process_skill_zip(
        contents, body.filename, body.category, body.session_id, user, db
    )


@router.get("")
async def list_skills(
    session_id: Optional[str] = None,
    enabled_user_skills: Optional[list[str]] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List user skills, optionally limited to one session plus user scope.

    When session_id and enabled_user_skills are both provided, user-level
    skills are filtered to only those in enabled_user_skills (opt-in behavior).
    Session-specific skills for session_id are always returned.
    """
    query = select(SkillPackage).where(SkillPackage.user_id == user.id)
    if session_id:
        if enabled_user_skills is not None:
            query = query.where(
                (SkillPackage.session_id == session_id)
                | (
                    SkillPackage.session_id.is_(None)
                    & SkillPackage.name.in_(enabled_user_skills)
                )
            )
        else:
            query = query.where(
                (SkillPackage.session_id == session_id)
                | SkillPackage.session_id.is_(None)
            )
    result = await db.execute(query.order_by(SkillPackage.created_at.desc()))
    skills = result.scalars().all()

    # Legacy rows created before session_id existed may be duplicated. Expose
    # one effective row per scope/name while preserving the newest metadata.
    seen: set[tuple[str | None, str]] = set()
    output = []
    for skill in skills:
        key = (skill.session_id, skill.name)
        if key in seen:
            continue
        seen.add(key)
        exists = _skill_dir_exists(user.id, skill.name, skill.session_id)
        output.append({
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "category": skill.category,
            "version": skill.version,
            "session_id": skill.session_id,
            "scope": "session" if skill.session_id else "user",
            "available": exists,
            "warning": None if exists else "Skill files are missing; reinstall this skill.",
            "created_at": skill.created_at.isoformat() if skill.created_at else None,
        })
    return output


@router.delete("/{name}")
async def delete_skill(
    name: str,
    session_id: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a skill from exactly one user/session scope."""
    _validate_skill_name(name)

    scope_filter = (
        SkillPackage.session_id == session_id
        if session_id else SkillPackage.session_id.is_(None)
    )
    result = await db.execute(
        select(SkillPackage).where(
            SkillPackage.user_id == user.id,
            SkillPackage.name == name,
            scope_filter,
        )
    )
    skills = result.scalars().all()
    if not skills:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    for skill in skills:
        await db.delete(skill)
    await db.commit()

    # Remove the skill-owned MCP runtime/config before deleting its files.
    target_dir = SKILLS_DATA_DIR / user.id
    if session_id:
        target_dir = target_dir / session_id
    target_dir = target_dir / name
    mcp_cleanup = await _remove_skill_mcp(
        str(target_dir), user.id, session_id or "default"
    )
    if target_dir.exists():
        import shutil
        shutil.rmtree(target_dir)

    # Invalidate skills cache
    try:
        _invalidate_skills_cache(user.id)
    except Exception:
        pass

    return {
        "success": True,
        "name": name,
        "session_id": session_id,
        "action": "deleted",
        "mcp": mcp_cleanup,
    }


@router.post("/{name}/promote")
async def promote_skill(
    name: str,
    session_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Promote a session-specific skill to user-level.

    Copies the skill files from data/skills/<user>/<session>/<name>/ to
    data/skills/<user>/<name>/, creates a user-level SkillPackage, then
    deletes the original session-level skill. The new user-level skill is
    auto-enabled in this session's enabled_user_skills so it continues to
    show up in the SkillBar.
    """
    _validate_skill_name(name)

    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == session_id,
            Conversation.user_id == user.id,
        )
    )).scalar_one_or_none()
    if conv is None:
        raise HTTPException(404, "Conversation not found")

    session_skill = (await db.execute(
        select(SkillPackage).where(
            SkillPackage.user_id == user.id,
            SkillPackage.name == name,
            SkillPackage.session_id == session_id,
        )
    )).scalar_one_or_none()
    if session_skill is None:
        raise HTTPException(404, f"Session skill '{name}' not found")

    existing_user_skill = (await db.execute(
        select(SkillPackage).where(
            SkillPackage.user_id == user.id,
            SkillPackage.name == name,
            SkillPackage.session_id.is_(None),
        )
    )).scalar_one_or_none()
    if existing_user_skill is not None:
        raise HTTPException(409, f"User-level skill '{name}' already exists")

    import shutil
    src_dir = SKILLS_DATA_DIR / user.id / session_id / name
    dst_dir = SKILLS_DATA_DIR / user.id / name

    if not src_dir.exists():
        raise HTTPException(404, f"Skill directory not found: {src_dir}")

    if dst_dir.exists():
        raise HTTPException(409, f"User-level skill directory already exists")

    shutil.copytree(src_dir, dst_dir)

    new_skill = SkillPackage(
        user_id=user.id,
        name=name,
        session_id=None,
        description=session_skill.description,
        category=session_skill.category,
        version=session_skill.version,
    )
    db.add(new_skill)

    # Remove the now-promoted session-level skill (both DB row and files).
    await db.execute(
        delete(SkillPackage).where(
            SkillPackage.user_id == user.id,
            SkillPackage.name == name,
            SkillPackage.session_id == session_id,
        )
    )

    # Auto-enable the new user-level skill in this session so the user sees
    # a seamless transition from session-scope to user-scope.
    enabled_list = []
    if conv.enabled_user_skills:
        try:
            parsed = json.loads(conv.enabled_user_skills)
            if isinstance(parsed, list):
                enabled_list = [str(n) for n in parsed if n]
        except (json.JSONDecodeError, TypeError):
            enabled_list = []
    if name not in enabled_list:
        enabled_list.append(name)
        conv.enabled_user_skills = json.dumps(enabled_list, ensure_ascii=False)

    await db.commit()

    # Clean up session-level skill files now that the DB row is gone.
    try:
        shutil.rmtree(src_dir, ignore_errors=True)
    except OSError:
        pass

    try:
        _invalidate_skills_cache(user.id)
    except Exception:
        pass

    return {
        "success": True,
        "name": name,
        "promoted_to": "user",
    }


@router.get("/optional")
async def list_optional_skills(user: User = Depends(get_current_user)):
    """List optional built-in skills available for activation."""
    return {"skills": _scan_optional_skills()}


async def _auto_register_mcp(
    skill_dir: str,
    user_id: str,
    session_id: str = "default",
) -> dict:
    """Ask the harness control plane to discover, persist, and connect MCP."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                "http://harness:8020/internal/mcp/auto-register",
                params={
                    "skill_dir": skill_dir,
                    "user_id": user_id,
                    "session_id": session_id,
                },
            )
        if response.status_code >= 400:
            return {
                "registered": [],
                "skipped": [],
                "errors": [f"harness HTTP {response.status_code}: {response.text[:300]}"],
            }
        return response.json()
    except Exception as exc:
        return {"registered": [], "skipped": [], "errors": [str(exc)]}



async def _remove_skill_mcp(
    skill_dir: str,
    user_id: str,
    session_id: str = "default",
) -> dict:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "http://harness:8020/internal/mcp/remove-skill",
                params={
                    "skill_dir": skill_dir,
                    "user_id": user_id,
                    "session_id": session_id,
                },
            )
        return response.json() if response.status_code < 400 else {
            "removed": [], "errors": [f"harness HTTP {response.status_code}"]
        }
    except Exception as exc:
        return {"removed": [], "errors": [str(exc)]}

def _invalidate_skills_cache(user_id: str):
    """Notify SkillsManager to invalidate its cache for this user."""
    try:
        from skills.manager import get_manager
        mgr = get_manager()
        mgr.invalidate(user_id)
    except ImportError:
        pass
