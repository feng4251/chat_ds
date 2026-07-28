"""Skill upload API — upload, list, and delete skill zip packages.

Endpoints:
  POST /api/skills/upload  — upload a skill zip package
  GET  /api/skills         — list installed skills
  DELETE /api/skills/{name} — delete a skill
  GET  /api/skills/optional — list optional built-in skills
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import weakref
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from config import settings
from database import get_db
from models import Conversation, SkillPackage, User
from skill_bundles import (
    legacy_bundle_projection,
    resolved_bundle_metadata,
)
from skill_frontmatter import SkillFrontmatterError, parse_skill_frontmatter
from workspace import safe_workspace_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["skills"])

# ── Constants ─────────────────────────────────────────────────────────────────

SKILLS_DATA_DIR = Path("data/skills")
MAX_ZIP_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_FILE_SIZE = 32 * 1024 * 1024  # bounded binary/template resources
MAX_UNCOMPRESSED_ZIP_SIZE = 64 * 1024 * 1024
MAX_ZIP_ENTRIES = 4096
MAX_ZIP_BASE64_SIZE = 4 * ((MAX_ZIP_SIZE + 2) // 3)
MAX_UPLOAD_FILENAME_BYTES = 255

# A Skill package is a resource bundle, not merely a text bundle.  Unknown
# extensions are preserved as inert resources so PDF/XLSX/DOCX templates and
# other standards-compliant assets are never silently discarded.  This set is
# only a presentation hint; execution remains controlled by explicit tools.
KNOWN_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".ico", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".gz", ".tgz", ".tar", ".7z", ".woff", ".woff2", ".ttf",
    ".otf", ".mp3", ".wav", ".mp4", ".mov", ".avi", ".parquet",
})

DEPENDENCY_MANIFEST_NAMES = frozenset({
    "pyproject.toml", "setup.cfg", "Pipfile", "environment.yml", "environment.yaml",
})

# Installation mutates both the canonical package registry and one filesystem
# scope.  Serialize those mutations per user/scope while still allowing
# unrelated users and sessions to install concurrently.  Weak references keep
# the registry bounded after an idle scope has no owner or waiter.
_INSTALL_LOCKS_GUARD = threading.Lock()
_INSTALL_LOCKS: weakref.WeakValueDictionary[
    tuple[str, str], asyncio.Lock
] = weakref.WeakValueDictionary()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _skill_install_lock(user_id: str, session_id: str | None) -> asyncio.Lock:
    key = (str(user_id), str(session_id) if session_id is not None else "<user>")
    with _INSTALL_LOCKS_GUARD:
        lock = _INSTALL_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _INSTALL_LOCKS[key] = lock
        return lock


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


def _dependency_manifest_entries(
    zf: zipfile.ZipFile,
    entries: list[tuple[zipfile.ZipInfo, str]],
    skill_roots: list[str],
) -> list[tuple[zipfile.ZipInfo, str]]:
    result: list[tuple[zipfile.ZipInfo, str]] = []
    for info, norm in entries:
        name = Path(norm).name
        if not (name.startswith("requirements") and name.endswith(".txt") or name in DEPENDENCY_MANIFEST_NAMES):
            continue
        if any(_entry_is_under_skill_root(norm, root) for root in skill_roots):
            continue
        result.append((info, norm))
    return result


def _copy_bundle_dependency_manifests(
    zf: zipfile.ZipFile,
    manifest_entries: list[tuple[zipfile.ZipInfo, str]],
    user_skills_dir: Path,
    bundle_ids: list[str] | None = None,
) -> list[str]:
    copied: list[str] = []
    if not manifest_entries:
        return copied
    owners = sorted(set(bundle_ids or ["legacy"]))
    for owner in owners:
        if re.fullmatch(r"[a-f0-9]{64}", owner) is None and owner != "legacy":
            raise HTTPException(
                status_code=400,
                detail="Invalid bundle identity for dependency manifests",
            )
        target_root = user_skills_dir / "_bundle_runtime" / owner
        target_root.mkdir(parents=True, exist_ok=True)
        for info, norm in manifest_entries:
            if info.file_size > MAX_FILE_SIZE:
                logger.warning(
                    "Skipping oversized bundle dependency manifest (%d bytes): %s",
                    info.file_size,
                    norm,
                )
                continue
            rel = norm.replace("/", "__")
            safe_path = _validate_bundle_rel_path(rel, target_root)
            data = zf.read(info.filename)
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning(
                    "Skipping non-UTF-8 bundle dependency manifest: %s",
                    norm,
                )
                continue
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            safe_path.write_bytes(data)
            copied.append(str(
                safe_path.relative_to(user_skills_dir.resolve())
            ))
    return copied


def _parse_frontmatter(text: str) -> dict:
    """Parse untrusted Skill YAML metadata with bounded SafeLoader semantics."""
    return parse_skill_frontmatter(text)


def _validate_skill_name(name: str) -> str:
    """Validate the standard Agent Skill canonical name grammar."""
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None or len(name) > 64:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid skill name '{name}'. Agent Skill names must be 1-64 "
                "lowercase ASCII letters/digits separated by single hyphens"
            ),
        )
    return name


def _validate_standard_manifest_fields(frontmatter: dict, source_path: str) -> None:
    """Fail upload early when standard Agent Skill metadata is malformed.

    The source ZIP directory may differ because installation canonicalizes it
    to ``name``; the installed package seen by scanner/loader always matches.
    Established harness compatibility extensions mirror the loader and remain
    bounded instead of being silently reinterpreted as standard metadata.
    """

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise HTTPException(
            status_code=400,
            detail=f"{source_path} frontmatter must have a non-empty string 'description' field",
        )
    if len(description) > 1024:
        raise HTTPException(
            status_code=400,
            detail=f"{source_path} frontmatter 'description' exceeds 1024 characters",
        )

    compatibility = frontmatter.get("compatibility")
    if compatibility is not None and (
        not isinstance(compatibility, str)
        or not compatibility.strip()
        or len(compatibility) > 500
    ):
        raise HTTPException(
            status_code=400,
            detail=f"{source_path} frontmatter 'compatibility' must be a 1-500 character string",
        )
    license_value = frontmatter.get("license")
    if license_value is not None and not isinstance(license_value, str):
        raise HTTPException(
            status_code=400,
            detail=f"{source_path} frontmatter 'license' must be a string",
        )

    metadata = frontmatter.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise HTTPException(
                status_code=400,
                detail=f"{source_path} frontmatter 'metadata' must be a string-to-string mapping",
            )
        for key, value in metadata.items():
            compatible_mapping = (
                key in {"hermes", "openclaw"} and isinstance(value, dict)
            )
            if not isinstance(key, str) or not (
                isinstance(value, str) or compatible_mapping
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{source_path} frontmatter 'metadata' values must be strings; "
                        "only bounded hermes/openclaw mappings are compatibility extensions"
                    ),
                )

    allowed_tools = frontmatter.get("allowed-tools")
    compatible_sequence = bool(
        isinstance(allowed_tools, list)
        and len(allowed_tools) <= 256
        and all(isinstance(item, str) for item in allowed_tools)
    )
    if allowed_tools is not None and not (
        isinstance(allowed_tools, str) or compatible_sequence
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{source_path} frontmatter 'allowed-tools' must be a space-separated "
                "string (or the bounded compatibility string list)"
            ),
        )


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
    filename: str = Field(min_length=1, max_length=MAX_UPLOAD_FILENAME_BYTES)
    content_base64: str = Field(min_length=1, max_length=MAX_ZIP_BASE64_SIZE)
    category: Optional[str] = Field(default=None, min_length=1, max_length=64)
    session_id: Optional[str] = Field(default=None, min_length=1, max_length=32)


def _validate_skill_archive_filename(filename: str) -> str:
    """Return a safe, user-visible leaf filename for a Skill archive."""
    if (
        not filename
        or filename != filename.strip()
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)
    ):
        raise HTTPException(
            status_code=400,
            detail="Filename must be a non-empty leaf name without path or control characters",
        )
    if len(filename.encode("utf-8")) > MAX_UPLOAD_FILENAME_BYTES:
        raise HTTPException(status_code=400, detail="Filename is too long")
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")
    return filename


def _decode_skill_archive_base64(encoded: str) -> bytes:
    """Decode one bounded canonical Base64 payload without ignoring garbage."""
    if not encoded or len(encoded) > MAX_ZIP_BASE64_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Zip file too large (max {MAX_ZIP_SIZE // 1024 // 1024}MB)",
        )
    try:
        contents = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 encoding") from exc
    if len(contents) > MAX_ZIP_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Zip file too large (max {MAX_ZIP_SIZE // 1024 // 1024}MB)",
        )
    return contents


def _validate_skill_archive(contents: bytes) -> tuple[list[dict], dict[str, dict[str, str]]]:
    """Validate a complete archive before making either storage mutation."""
    if len(contents) > MAX_ZIP_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Zip file too large (max {MAX_ZIP_SIZE // 1024 // 1024}MB)",
        )
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            entries = _zip_file_entries(archive)
            manifests = _discover_skill_manifests(archive, entries)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Invalid zip file") from exc
    return manifests, _bundle_manifest_metadata(contents, manifests)


async def _require_session_conversation(
    session_id: str,
    user: User,
    db: AsyncSession,
) -> Conversation:
    conversation = (await db.execute(
        select(Conversation).where(
            Conversation.id == session_id,
            Conversation.user_id == user.id,
        )
    )).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_create_bytes(path: Path, contents: bytes) -> bool:
    """Create ``path`` atomically without replacing an existing attachment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".chat_ds_skill_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Linking a fully fsynced private temporary file gives us atomic,
            # cross-process no-clobber semantics on the workspace filesystem.
            os.link(temp_name, path)
            return True
        except FileExistsError:
            return False
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def _persist_session_skill_archive(
    *,
    contents: bytes,
    filename: str,
    user_id: str,
    session_id: str,
) -> tuple[dict, Path]:
    """Persist the byte-identical source ZIP as a visible workspace attachment."""
    try:
        destination = safe_workspace_path(user_id, session_id, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    archive_sha256 = hashlib.sha256(contents).hexdigest()
    created = _atomic_create_bytes(destination, contents)
    if not created:
        try:
            existing_stat = destination.lstat()
        except FileNotFoundError:
            # A concurrent delete won the race. One bounded retry retains the
            # same no-clobber semantics without hiding a genuine conflict.
            created = _atomic_create_bytes(destination, contents)
            if created:
                existing_stat = None
            else:
                existing_stat = destination.lstat()
        if not created:
            if (
                existing_stat is None
                or not stat.S_ISREG(existing_stat.st_mode)
                or existing_stat.st_size != len(contents)
                or _sha256_file(destination) != archive_sha256
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Workspace attachment '{filename}' already exists with "
                        "different content. Rename the archive or remove the existing file."
                    ),
                )

    attachment = {
        "kind": "skill_archive",
        "filename": filename,
        "path": filename,
        "size": len(contents),
        "sha256": archive_sha256,
        "status": "created" if created else "unchanged",
    }
    return attachment, destination


def _rollback_created_skill_archive(
    attachment: dict,
    destination: Path,
) -> bool:
    """Remove only the still-identical file created by the failed request."""
    if attachment.get("status") != "created":
        return False
    try:
        current_stat = destination.lstat()
        if (
            not stat.S_ISREG(current_stat.st_mode)
            or current_stat.st_size != int(attachment["size"])
            or _sha256_file(destination) != attachment["sha256"]
        ):
            return False
        destination.unlink()
        return True
    except (FileNotFoundError, OSError):
        return False


def _zip_file_entries(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    entries: list[tuple[zipfile.ZipInfo, str]] = []
    total_uncompressed = 0
    seen_paths: dict[str, str] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise HTTPException(
                status_code=400,
                detail=f"Symbolic links are not allowed in Skill packages: {info.filename}",
            )
        if info.file_size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Skill resource too large: {info.filename} "
                    f"(max {MAX_FILE_SIZE // 1024 // 1024}MB)"
                ),
            )
        total_uncompressed += int(info.file_size)
        if total_uncompressed > MAX_UNCOMPRESSED_ZIP_SIZE:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Skill package expands beyond the uncompressed size limit "
                    f"({MAX_UNCOMPRESSED_ZIP_SIZE // 1024 // 1024}MB)"
                ),
            )
        norm = info.filename.replace("\\", "/").lstrip("/")
        parts: list[str] = []
        for part in norm.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                raise HTTPException(status_code=400, detail=f"Path traversal detected: {info.filename}")
            parts.append(part)
        if parts:
            normalized = "/".join(parts)
            folded = normalized.casefold()
            if folded in seen_paths:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Skill package contains duplicate or case-colliding paths: "
                        f"{seen_paths[folded]} and {normalized}"
                    ),
                )
            seen_paths[folded] = normalized
            entries.append((info, normalized))
            if len(entries) > MAX_ZIP_ENTRIES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Skill package contains more than {MAX_ZIP_ENTRIES} files",
                )
    return entries


def _is_allowed_skill_file(rel_path: str) -> tuple[bool, bool]:
    """Return storage eligibility and a best-effort binary presentation hint.

    All safely named, bounded regular files are eligible.  Extension filtering
    used to silently drop valid Skill assets; callers must not use this hint as
    an execution authorization decision.
    """
    lower = rel_path.lower()
    is_binary = any(lower.endswith(ext) for ext in KNOWN_BINARY_EXTENSIONS)
    return True, is_binary


def _discover_skill_manifests(
    zf: zipfile.ZipFile,
    entries: list[tuple[zipfile.ZipInfo, str]],
) -> list[dict]:
    """Discover pairwise non-overlapping Skill roots in one ZIP bundle.

    A recognized Skill owns its full directory resource closure.  In
    particular, a tutorial or fixture named ``references/.../SKILL.md`` is a
    resource of that parent, not an implicit nested package.  Multiple sibling
    roots remain an explicit ZIP bundle and are installed independently.
    """
    manifests: list[dict] = []
    seen_names: dict[str, str] = {}
    recognized_roots: list[str] = []
    candidates: list[tuple[int, str, zipfile.ZipInfo]] = []
    for info, norm in entries:
        parts = norm.split("/")
        if parts[-1] == "SKILL.md":
            candidates.append((len(parts) - 1, norm, info))

    # Parents must be recognized before descendants can be classified as
    # ordinary resources.  ZIP member ordering is neither semantic nor stable.
    candidates.sort(key=lambda item: (item[0], item[1]))
    for _depth, norm, info in candidates:
        parts = norm.split("/")
        root = "/".join(parts[:-1])
        if any(
            _entry_is_under_skill_root(root, parent_root)
            for parent_root in recognized_roots
        ):
            continue
        if info.file_size > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"SKILL.md too large: {norm}")

        try:
            skill_md_content = zf.read(info.filename).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"SKILL.md must be valid UTF-8: {norm}",
            ) from exc
        try:
            fm = _parse_frontmatter(skill_md_content)
        except SkillFrontmatterError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid YAML frontmatter in {norm}: {exc}",
            ) from exc

        raw_name = fm.get("name", "")
        if raw_name is not None and not isinstance(raw_name, str):
            raise HTTPException(
                status_code=400,
                detail=f"{norm} frontmatter 'name' must be a string",
            )
        skill_name = str(raw_name or "").strip()
        if not skill_name:
            raise HTTPException(status_code=400, detail=f"{norm} frontmatter must have a 'name' field")
        _validate_skill_name(skill_name)
        _validate_standard_manifest_fields(fm, norm)
        if skill_name in seen_names:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate skill name '{skill_name}' in {seen_names[skill_name]} and {norm}",
            )
        seen_names[skill_name] = norm

        raw_description = fm.get("description", "")
        if raw_description is not None and not isinstance(raw_description, str):
            raise HTTPException(
                status_code=400,
                detail=f"{norm} frontmatter 'description' must be a string",
            )
        raw_version = fm.get("version", "")
        if isinstance(raw_version, (dict, list, tuple, set)):
            raise HTTPException(
                status_code=400,
                detail=f"{norm} frontmatter 'version' must be a scalar",
            )

        path_category = "/".join(parts[:-2]) or None
        manifests.append({
            "name": skill_name,
            "description": str(raw_description or "").strip(),
            "version": "" if raw_version is None else str(raw_version),
            "root": root,
            "skill_md": norm,
            "skill_md_content": skill_md_content,
            "category": path_category,
        })
        recognized_roots.append(root)

    if not manifests:
        raise HTTPException(status_code=400, detail="SKILL.md not found in zip")

    manifests.sort(key=lambda item: (item["root"].count("/"), item["root"], item["name"]))
    return manifests


def _bundle_manifest_metadata(
    contents: bytes,
    manifests: list[dict],
) -> dict[str, dict[str, str]]:
    """Return stable, non-guessing bundle identity for discovered manifests.

    A ZIP with one Skill is a one-member bundle.  For a multi-Skill archive,
    a unique shallowest manifest is an unambiguous primary and all deeper
    manifests are supporting members.  Equal-depth roots do not establish a
    primary, so they remain independent top-level Skills with distinct IDs
    rather than being grouped under an invented owner.
    """

    archive_digest = hashlib.sha256(contents).hexdigest()
    if not manifests:
        return {}

    primary: dict | None = None
    if len(manifests) == 1:
        primary = manifests[0]
    else:
        minimum_depth = min(str(item["root"]).count("/") for item in manifests)
        shallowest = [
            item
            for item in manifests
            if str(item["root"]).count("/") == minimum_depth
        ]
        if len(shallowest) == 1:
            primary = shallowest[0]

    result: dict[str, dict[str, str]] = {}
    if primary is not None:
        root_name = str(primary["name"])
        for manifest in manifests:
            name = str(manifest["name"])
            result[name] = {
                "bundle_id": archive_digest,
                "bundle_role": "primary" if manifest is primary else "supporting",
                "bundle_root_name": root_name,
                "bundle_source_path": str(manifest["skill_md"]),
            }
        return result

    for manifest in manifests:
        name = str(manifest["name"])
        member_id = hashlib.sha256(
            f"{archive_digest}\0{manifest['root']}\0{name}".encode("utf-8")
        ).hexdigest()
        result[name] = {
            "bundle_id": member_id,
            "bundle_role": "primary",
            "bundle_root_name": name,
            "bundle_source_path": str(manifest["skill_md"]),
        }
    return result


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


def _installed_skill_files_match_archive(
    *,
    contents: bytes,
    manifest: dict,
    all_manifests: list[dict],
    target_dir: Path,
) -> bool:
    """Prove that one canonical Skill directory still matches the archive."""
    if not target_dir.is_dir() or target_dir.is_symlink():
        return False
    root = str(manifest["root"])
    all_roots = [str(item["root"]) for item in all_manifests]
    expected: dict[str, tuple[int, str]] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            entries = _zip_file_entries(archive)
            for info, normalized in entries:
                if not _entry_is_under_skill_root(normalized, root):
                    continue
                if _entry_is_under_nested_skill_root(normalized, root, all_roots):
                    continue
                relative = normalized[len(root):].lstrip("/") if root else normalized
                if not relative:
                    continue
                data = archive.read(info.filename)
                expected[relative] = (len(data), hashlib.sha256(data).hexdigest())
    except (HTTPException, OSError, zipfile.BadZipFile, RuntimeError):
        return False

    actual_paths: set[str] = set()
    try:
        for path in target_dir.rglob("*"):
            if path.is_symlink():
                return False
            if not path.is_file():
                continue
            relative = str(path.relative_to(target_dir)).replace("\\", "/")
            actual_paths.add(relative)
            expected_file = expected.get(relative)
            if expected_file is None:
                return False
            size, digest = expected_file
            if path.stat().st_size != size or _sha256_file(path) != digest:
                return False
    except OSError:
        return False
    return actual_paths == set(expected)


async def _exact_existing_bundle_result(
    *,
    contents: bytes,
    manifests: list[dict],
    bundle_metadata: dict[str, dict[str, str]],
    category: str | None,
    session_id: str | None,
    user: User,
    db: AsyncSession,
) -> dict | None:
    """Return an idempotent result only for the exact immutable installed bundle."""
    names = [str(manifest["name"]) for manifest in manifests]
    scope_filter = (
        SkillPackage.session_id == session_id
        if session_id is not None
        else SkillPackage.session_id.is_(None)
    )
    rows = (await db.execute(
        select(SkillPackage).where(
            SkillPackage.user_id == user.id,
            scope_filter,
            SkillPackage.name.in_(names),
        )
    )).scalars().all()
    rows_by_name: dict[str, list[SkillPackage]] = {}
    for row in rows:
        rows_by_name.setdefault(str(row.name), []).append(row)

    installed: list[dict] = []
    for manifest in manifests:
        name = str(manifest["name"])
        metadata = bundle_metadata[name]
        expected_category = category
        if expected_category is None and manifest.get("category"):
            expected_category = str(manifest["category"])[:64]
        matching_rows = [
            row
            for row in rows_by_name.get(name, [])
            if row.bundle_id == metadata["bundle_id"]
            and row.bundle_role == metadata["bundle_role"]
            and row.bundle_root_name == metadata["bundle_root_name"]
            and row.bundle_source_path == metadata["bundle_source_path"]
            and (row.category or None) == (expected_category or None)
            and (row.version or "") == str(manifest.get("version") or "")
            and (row.description or "") == str(manifest.get("description") or "")
        ]
        if len(matching_rows) != 1:
            return None
        target_dir = SKILLS_DATA_DIR / user.id
        if session_id is not None:
            target_dir = target_dir / session_id
        target_dir = target_dir / name
        if not _installed_skill_files_match_archive(
            contents=contents,
            manifest=manifest,
            all_manifests=manifests,
            target_dir=target_dir,
        ):
            return None
        installed.append({
            "name": name,
            "description": str(manifest.get("description") or ""),
            "category": expected_category,
            "version": str(manifest.get("version") or ""),
            "session_id": session_id,
            **metadata,
        })

    return {
        "success": True,
        "skill": installed[0],
        "skills": installed,
        "installed_count": len(installed),
        "mcp": {
            "registered": [],
            "skipped": [],
            "errors": [],
            "runtime": [],
            "status": "unchanged",
        },
        "mcp_by_skill": {},
        "runtime": [],
        "idempotent": True,
        "installation_status": "already_installed",
    }


async def _finish_awaitable_with_result(
    awaitable,
) -> tuple[object, BaseException | None]:
    """Finish one durability operation even if its caller is cancelled.

    ``asyncio.shield`` alone returns to the cancelled caller before the inner
    operation finishes.  For a database commit that leaves an unsafe ambiguity:
    deleting files while the commit completes in the background can create a
    durable registry row pointing at nothing.  This helper waits for the inner
    task to reach a real terminal state and reports any caller cancellation only
    after that state is known.
    """

    task = asyncio.create_task(awaitable)
    caller_cancellation: BaseException | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            caller_cancellation = caller_cancellation or exc
    if task.cancelled():
        raise asyncio.CancelledError()
    error = task.exception()
    if error is not None:
        raise error
    return task.result(), caller_cancellation


async def _finish_awaitable_despite_cancellation(awaitable) -> BaseException | None:
    _result, caller_cancellation = await _finish_awaitable_with_result(
        awaitable
    )
    return caller_cancellation


async def _rollback_database_best_effort(db: AsyncSession) -> None:
    try:
        await _finish_awaitable_despite_cancellation(db.rollback())
    except BaseException:
        logger.exception("Failed to roll back Skill installation transaction")


def _scope_skills_dir(user_id: str, session_id: str | None) -> Path:
    root = SKILLS_DATA_DIR / user_id
    return root / session_id if session_id is not None else root


def _skill_operation_dir(
    *,
    user_id: str,
    kind: str,
    identity_parts: list[str],
) -> Path:
    identity = "\0".join([kind, user_id, *identity_parts])
    operation_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return (
        SKILLS_DATA_DIR
        / user_id
        / ".chatds_operations"
        / f"{kind}-{operation_id}"
    )


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fd, temporary = tempfile.mkstemp(
        prefix=".operation-",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _read_operation_journal(path: Path) -> dict | None:
    try:
        raw = path.read_bytes()
        if len(raw) > 256 * 1024:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _directory_digest(path: Path) -> str:
    if not path.is_dir() or path.is_symlink():
        raise HTTPException(
            status_code=409,
            detail=f"Expected immutable Skill directory is missing: {path.name}",
        )
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise HTTPException(
                status_code=409,
                detail=f"Symbolic link found in immutable Skill directory: {path.name}",
            )
        if not child.is_file():
            continue
        relative = str(child.relative_to(path)).replace("\\", "/")
        data_digest = _sha256_file(child)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(child.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(data_digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _restore_quarantined_paths(
    *,
    quarantine_root: Path,
    scope_root: Path,
    member_names: list[str],
    runtime_bundle_ids: list[str],
) -> bool:
    restored = True
    for member_name in member_names:
        quarantined = quarantine_root / "skills" / member_name
        canonical = scope_root / member_name
        if not quarantined.exists():
            continue
        if canonical.exists() or canonical.is_symlink():
            restored = False
            continue
        os.rename(quarantined, canonical)
    for bundle_id in runtime_bundle_ids:
        quarantined = quarantine_root / "runtime" / bundle_id
        canonical = scope_root / "_bundle_runtime" / bundle_id
        if not quarantined.exists():
            continue
        canonical.parent.mkdir(parents=True, exist_ok=True)
        if canonical.exists() or canonical.is_symlink():
            restored = False
            continue
        os.rename(quarantined, canonical)
    return restored


def _remove_request_owned_directory(ownership: tuple[Path, int, int]) -> bool:
    """Remove a published directory only while its original inode is retained."""
    path, device, inode = ownership
    try:
        current = path.lstat()
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_dev != device
            or current.st_ino != inode
        ):
            return False
        shutil.rmtree(path)
        return True
    except (FileNotFoundError, OSError):
        return False


def _remove_request_owned_file(
    ownership: tuple[Path, int, str],
) -> bool:
    """Remove an atomically-created file only if its content is still ours."""
    path, size, digest = ownership
    try:
        current = path.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_size != size
            or _sha256_file(path) != digest
        ):
            return False
        path.unlink()
        return True
    except (FileNotFoundError, OSError):
        return False


def _publish_staged_directory(
    staged: Path,
    destination: Path,
) -> tuple[Path, int, int]:
    """Publish a complete directory without intentionally replacing a target."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(str(destination))
    os.rename(staged, destination)
    published = destination.lstat()
    if not stat.S_ISDIR(published.st_mode):
        raise RuntimeError("Published Skill target is not a directory")
    return destination, published.st_dev, published.st_ino


def _publish_runtime_manifests(
    staging_root: Path,
    scope_root: Path,
) -> list[tuple[Path, int, str]]:
    """Atomically merge staged bundle-level manifests without clobbering."""
    staged_runtime = staging_root / "_bundle_runtime"
    if not staged_runtime.is_dir():
        return []
    created: list[tuple[Path, int, str]] = []
    for staged_file in sorted(staged_runtime.rglob("*")):
        if not staged_file.is_file() or staged_file.is_symlink():
            continue
        relative = staged_file.relative_to(staging_root)
        destination = scope_root / relative
        contents = staged_file.read_bytes()
        digest = hashlib.sha256(contents).hexdigest()
        was_created = _atomic_create_bytes(destination, contents)
        if not was_created:
            try:
                current = destination.lstat()
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"Bundle runtime resource changed concurrently: {relative}",
                ) from exc
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_size != len(contents)
                or _sha256_file(destination) != digest
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"Bundle runtime resource already exists with different content: {relative}",
                )
        else:
            created.append((destination, len(contents), digest))
    return created


async def _rebuild_mcp_for_skills(
    *,
    skills: list[dict],
    target_dirs: dict[str, Path],
    user_id: str,
    session_id: str | None,
    reason: str,
) -> dict:
    """Reconcile MCP state from committed immutable Skill directories."""
    mcp_result: dict[str, object] = {
        "registered": [],
        "skipped": [],
        "errors": [],
        "runtime": [],
        "status": "reconciling",
        "reason": reason,
    }
    mcp_by_skill: dict[str, dict] = {}
    for skill in skills:
        skill_name = str(skill["name"])
        try:
            skill_mcp = await _auto_register_mcp(
                str(target_dirs[skill_name]),
                user_id,
                session_id or "default",
            )
            mcp_by_skill[skill_name] = skill_mcp
            for key in ("registered", "skipped", "errors"):
                values = skill_mcp.get(key) or []
                if isinstance(values, list):
                    cast_values = mcp_result[key]
                    assert isinstance(cast_values, list)
                    cast_values.extend(values)
            runtime = skill_mcp.get("runtime")
            if isinstance(runtime, dict):
                runtime_values = mcp_result["runtime"]
                assert isinstance(runtime_values, list)
                runtime_values.append({"skill": skill_name, **runtime})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auto MCP registration failed for skill '%s'", skill_name)
            error = f"Auto MCP registration failed for skill '{skill_name}'"
            errors = mcp_result["errors"]
            assert isinstance(errors, list)
            errors.append(error)
            mcp_by_skill[skill_name] = {
                "registered": [],
                "skipped": [],
                "errors": [error],
            }
    errors = mcp_result["errors"]
    mcp_result["status"] = (
        "degraded" if isinstance(errors, list) and errors else "reconciled"
    )
    return {
        "mcp": mcp_result,
        "mcp_by_skill": mcp_by_skill,
        "runtime": mcp_result["runtime"],
    }


async def _process_skill_zip(
    contents: bytes,
    filename: str,
    category: str | None,
    session_id: str | None,
    user: User,
    db: AsyncSession,
):
    """Install one immutable archive through the canonical transaction path.

    Session multipart, JSON, and conversation uploads all call this function.
    Files are fully staged before publication; the database commit is the
    durability boundary.  A cancellation before that boundary removes only
    request-owned paths, while cancellation after it deliberately leaves the
    committed package intact for an exact idempotent retry.
    """
    filename = _validate_skill_archive_filename(filename)
    if len(contents) > MAX_ZIP_SIZE:
        raise HTTPException(status_code=400, detail=f"Zip file too large (max {MAX_ZIP_SIZE // 1024 // 1024}MB)")

    if session_id:
        await _require_session_conversation(session_id, user, db)

    manifests, bundle_metadata = _validate_skill_archive(contents)
    scope_root = _scope_skills_dir(user.id, session_id)
    scope_filter = (
        SkillPackage.session_id == session_id
        if session_id is not None
        else SkillPackage.session_id.is_(None)
    )
    skill_names = [str(manifest["name"]) for manifest in manifests]
    installed_skills: list[dict] = []
    target_dirs: dict[str, Path] = {}
    attachment: dict | None = None
    archive_destination: Path | None = None
    idempotent_result: dict | None = None

    async with _skill_install_lock(user.id, session_id):
        committed = False
        staging_root: Path | None = None
        published_dirs: list[tuple[Path, int, int]] = []
        created_runtime_files: list[tuple[Path, int, str]] = []
        try:
            if session_id is not None:
                attachment, archive_destination = _persist_session_skill_archive(
                    contents=contents,
                    filename=filename,
                    user_id=user.id,
                    session_id=session_id,
                )

            existing_rows = (await db.execute(
                select(SkillPackage.name).where(
                    SkillPackage.user_id == user.id,
                    SkillPackage.name.in_(skill_names),
                    scope_filter,
                )
            )).all()
            existing_names = {str(row[0]) for row in existing_rows}
            existing_dirs = {
                name for name in skill_names
                if (scope_root / name).exists() or (scope_root / name).is_symlink()
            }
            conflicts = sorted(existing_names | existing_dirs)
            if conflicts:
                idempotent_result = await _exact_existing_bundle_result(
                    contents=contents,
                    manifests=manifests,
                    bundle_metadata=bundle_metadata,
                    category=category,
                    session_id=session_id,
                    user=user,
                    db=db,
                )
                if idempotent_result is None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Skills already exist: {', '.join(conflicts)}. "
                            "Delete them first to update."
                        ),
                    )
                installed_skills = list(idempotent_result["skills"])
                target_dirs = {
                    str(skill["name"]): scope_root / str(skill["name"])
                    for skill in installed_skills
                }
            else:
                scope_root.mkdir(parents=True, exist_ok=True)
                staging_root = Path(tempfile.mkdtemp(
                    prefix=".skill-install-",
                    dir=str(scope_root),
                ))
                with zipfile.ZipFile(io.BytesIO(contents)) as archive:
                    entries = _zip_file_entries(archive)
                    skill_roots = [
                        str(manifest["root"]) for manifest in manifests
                    ]
                    dependency_entries = _dependency_manifest_entries(
                        archive,
                        entries,
                        skill_roots,
                    )
                    _copy_bundle_dependency_manifests(
                        archive,
                        dependency_entries,
                        staging_root,
                        sorted({
                            str(metadata["bundle_id"])
                            for metadata in bundle_metadata.values()
                        }),
                    )

                    for manifest in manifests:
                        skill_name = str(manifest["name"])
                        root = str(manifest["root"])
                        staged_skill_dir = staging_root / skill_name
                        staged_skill_dir.mkdir(parents=True, exist_ok=False)
                        for info, normalized in entries:
                            if not _entry_is_under_skill_root(normalized, root):
                                continue
                            if _entry_is_under_nested_skill_root(
                                normalized,
                                root,
                                skill_roots,
                            ):
                                continue
                            relative = (
                                normalized[len(root):].lstrip("/")
                                if root
                                else normalized
                            )
                            if not relative:
                                continue
                            allowed, _binary_hint = _is_allowed_skill_file(relative)
                            if not allowed:
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"Unsupported Skill resource: {relative}",
                                )
                            safe_path = _validate_bundle_rel_path(
                                relative,
                                staged_skill_dir,
                            )
                            safe_path.parent.mkdir(parents=True, exist_ok=True)
                            safe_path.write_bytes(archive.read(info.filename))

                        skill_category = category
                        if skill_category is None and manifest.get("category"):
                            skill_category = str(manifest["category"])[:64]
                        description = str(manifest.get("description") or "")
                        metadata = bundle_metadata[skill_name]
                        installed_skill = {
                            "name": skill_name,
                            "description": description,
                            "category": skill_category,
                            "version": str(manifest.get("version") or ""),
                            "session_id": session_id,
                            **metadata,
                        }
                        installed_skills.append(installed_skill)

                # Recheck immediately before publication. This is redundant for
                # one Backend process but preserves no-clobber behavior if a
                # legacy writer bypasses the in-memory coordinator.
                late_conflicts = [
                    name
                    for name in skill_names
                    if (scope_root / name).exists()
                    or (scope_root / name).is_symlink()
                ]
                if late_conflicts:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Skill directories appeared during installation: "
                            + ", ".join(sorted(late_conflicts))
                        ),
                    )

                created_runtime_files = _publish_runtime_manifests(
                    staging_root,
                    scope_root,
                )
                for skill in installed_skills:
                    skill_name = str(skill["name"])
                    destination = scope_root / skill_name
                    ownership = _publish_staged_directory(
                        staging_root / skill_name,
                        destination,
                    )
                    published_dirs.append(ownership)
                    target_dirs[skill_name] = destination
                    db.add(SkillPackage(
                        user_id=user.id,
                        name=skill_name,
                        session_id=session_id,
                        description=(
                            str(skill["description"])[:1024]
                            if skill["description"]
                            else None
                        ),
                        category=skill["category"],
                        version=str(skill["version"]),
                        bundle_id=skill["bundle_id"],
                        bundle_role=skill["bundle_role"],
                        bundle_root_name=skill["bundle_root_name"],
                        bundle_source_path=skill["bundle_source_path"],
                    ))

                caller_cancellation = await _finish_awaitable_despite_cancellation(
                    db.commit()
                )
                committed = True
                if caller_cancellation is not None:
                    raise caller_cancellation
        except BaseException:
            if not committed and idempotent_result is None:
                await _rollback_database_best_effort(db)
                for ownership in reversed(published_dirs):
                    _remove_request_owned_directory(ownership)
                for ownership in reversed(created_runtime_files):
                    _remove_request_owned_file(ownership)
                if attachment is not None and archive_destination is not None:
                    _rollback_created_skill_archive(
                        attachment,
                        archive_destination,
                    )
            raise
        finally:
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)

    try:
        _invalidate_skills_cache(user.id)
    except Exception:
        pass

    # Reacquiring an uncontended asyncio lock does not yield, so management
    # operations cannot interleave between the committed publish and its MCP
    # reconciliation. If cancellation wins at this boundary, no MCP mutation
    # starts and an exact retry can safely perform it later.
    async with _skill_install_lock(user.id, session_id):
        mcp_state = await _rebuild_mcp_for_skills(
            skills=installed_skills,
            target_dirs=target_dirs,
            user_id=user.id,
            session_id=session_id,
            reason=(
                "exact_retry"
                if idempotent_result is not None
                else "new_install"
            ),
        )
    result = idempotent_result or {
        "success": True,
        "skill": installed_skills[0],
        "skills": installed_skills,
        "installed_count": len(installed_skills),
        "idempotent": False,
        "installation_status": "installed",
    }
    result.update(mcp_state)
    if attachment is not None:
        result["workspace_attachment"] = attachment
    return result


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

    filename = _validate_skill_archive_filename(file.filename or "")
    contents = await file.read()
    return await _process_skill_zip(contents, filename, category, session_id, user, db)


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
    filename = _validate_skill_archive_filename(body.filename)
    contents = _decode_skill_archive_base64(body.content_base64)
    _validate_skill_archive(contents)
    return await _process_skill_zip(
        contents,
        filename,
        body.category,
        body.session_id,
        user,
        db,
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
    legacy_bundle_metadata = legacy_bundle_projection(skills)
    for skill in skills:
        key = (skill.session_id, skill.name)
        if key in seen:
            continue
        seen.add(key)
        exists = _skill_dir_exists(user.id, skill.name, skill.session_id)
        bundle = resolved_bundle_metadata(skill, legacy_bundle_metadata)
        bundle_id = bundle["bundle_id"]
        bundle_role = bundle["bundle_role"]
        bundle_root_name = bundle["bundle_root_name"]
        bundle_source_path = bundle["bundle_source_path"]
        output.append({
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "category": skill.category,
            "version": skill.version,
            "session_id": skill.session_id,
            "scope": "session" if skill.session_id else "user",
            "bundle_id": bundle_id,
            "bundle_role": bundle_role,
            "bundle_root_name": bundle_root_name,
            "bundle_source_path": bundle_source_path,
            "is_bundle_child": bundle_role == "supporting",
            "available": exists,
            "warning": None if exists else "Skill files are missing; reinstall this skill.",
            "created_at": skill.created_at.isoformat() if skill.created_at else None,
        })
    return output


async def _bundle_members_for_management(
    *,
    name: str,
    session_id: str | None,
    user: User,
    db: AsyncSession,
) -> tuple[SkillPackage, list[SkillPackage], dict[str, str | None]]:
    """Resolve one complete bundle cohort or fail closed on ambiguous metadata."""
    scope_filter = (
        SkillPackage.session_id == session_id
        if session_id is not None
        else SkillPackage.session_id.is_(None)
    )
    rows = (await db.execute(
        select(SkillPackage).where(
            SkillPackage.user_id == user.id,
            scope_filter,
        )
    )).scalars().all()
    matches = [row for row in rows if str(row.name) == name]
    if not matches:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    if len(matches) != 1:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Skill '{name}' has ambiguous duplicate registry rows; "
                "restart after the scope-identity migration before modifying it"
            ),
        )

    target = matches[0]
    projected = legacy_bundle_projection(rows)
    metadata = resolved_bundle_metadata(target, projected)
    explicit_bundle = target.bundle_id is not None
    role = metadata.get("bundle_role")
    bundle_id = metadata.get("bundle_id")
    root_name = metadata.get("bundle_root_name")

    if explicit_bundle and (
        role not in {"primary", "supporting"}
        or not isinstance(bundle_id, str)
        or not bundle_id
        or not isinstance(root_name, str)
        or not root_name
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Skill '{name}' has incomplete bundle metadata",
        )

    # Historic ungrouped single Skills remain compatible with the old
    # management API. Conservatively projected historic bundles receive the
    # same protections as newly persisted bundles.
    if not isinstance(bundle_id, str) or not bundle_id:
        return target, [target], {
            "bundle_id": None,
            "bundle_role": "primary",
            "bundle_root_name": name,
            "bundle_source_path": metadata.get("bundle_source_path"),
        }

    cohort: list[SkillPackage] = []
    cohort_metadata: list[dict[str, str | None]] = []
    for row in rows:
        resolved = resolved_bundle_metadata(row, projected)
        if resolved.get("bundle_id") == bundle_id:
            cohort.append(row)
            cohort_metadata.append(resolved)
    primary_rows = [
        row
        for row, resolved in zip(cohort, cohort_metadata)
        if resolved.get("bundle_role") == "primary"
    ]
    if (
        not cohort
        or len(primary_rows) != 1
        or any(
            resolved.get("bundle_role") not in {"primary", "supporting"}
            or resolved.get("bundle_root_name") != root_name
            for resolved in cohort_metadata
        )
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Skill bundle containing '{name}' has an ambiguous registry",
        )
    return target, cohort, metadata


@router.delete("/{name}")
async def delete_skill(
    name: str,
    session_id: Optional[str] = None,
    delete_bundle: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a standalone Skill or explicitly delete a complete bundle."""
    _validate_skill_name(name)

    async with _skill_install_lock(user.id, session_id):
        scope_root = _scope_skills_dir(user.id, session_id)
        operation_dir = _skill_operation_dir(
            user_id=user.id,
            kind="delete",
            identity_parts=[session_id or "<user>", name],
        )
        journal_path = operation_dir / "journal.json"
        quarantine_root = operation_dir / "quarantine"
        journal = _read_operation_journal(journal_path)
        cohort: list[SkillPackage] = []
        metadata: dict[str, str | None]
        registry_present = True
        try:
            _target, cohort, metadata = await _bundle_members_for_management(
                name=name,
                session_id=session_id,
                user=user,
                db=db,
            )
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            registry_present = False
            if (
                not isinstance(journal, dict)
                or journal.get("version") != 1
                or journal.get("kind") != "delete"
                or journal.get("user_id") != user.id
                or journal.get("session_id") != (session_id or "")
                or journal.get("requested_name") != name
                or journal.get("state")
                not in {"prepared", "committed", "completed"}
            ):
                raise
            metadata = {
                "bundle_id": journal.get("bundle_id") or None,
                "bundle_role": "primary",
                "bundle_root_name": name,
                "bundle_source_path": None,
            }

        if registry_present:
            role = metadata.get("bundle_role")
            if role == "supporting":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Supporting Skill '{name}' cannot be deleted independently; "
                        f"delete primary '{metadata.get('bundle_root_name')}' as a bundle"
                    ),
                )
            if len(cohort) > 1 and not delete_bundle:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Primary Skill '{name}' owns a {len(cohort)}-member bundle; "
                        "retry with delete_bundle=true to delete the complete bundle"
                    ),
                )
            deleted_names = sorted(str(skill.name) for skill in cohort)
            bundle_id = metadata.get("bundle_id")
            runtime_bundle_ids = (
                [str(bundle_id)]
                if isinstance(bundle_id, str) and bundle_id
                else []
            )
            # A completed receipt from an earlier generation must not suppress
            # deletion of a newly installed row with the same name.
            if isinstance(journal, dict) and journal.get("state") == "completed":
                shutil.rmtree(operation_dir, ignore_errors=True)
                journal = None
            if journal is None:
                if operation_dir.exists():
                    shutil.rmtree(operation_dir, ignore_errors=True)
                member_digests = {
                    member_name: _directory_digest(scope_root / member_name)
                    for member_name in deleted_names
                }
                runtime_digests = {
                    owner: _directory_digest(
                        scope_root / "_bundle_runtime" / owner
                    )
                    for owner in runtime_bundle_ids
                    if (
                        scope_root / "_bundle_runtime" / owner
                    ).is_dir()
                }
                journal = {
                    "version": 1,
                    "kind": "delete",
                    "user_id": user.id,
                    "session_id": session_id or "",
                    "requested_name": name,
                    "bundle_id": bundle_id or "",
                    "member_names": deleted_names,
                    "member_digests": member_digests,
                    "runtime_bundle_ids": runtime_bundle_ids,
                    "runtime_digests": runtime_digests,
                    "state": "prepared",
                }
                _atomic_write_json(journal_path, journal)
            elif (
                journal.get("member_names") != deleted_names
                or journal.get("bundle_id") != (bundle_id or "")
                or journal.get("state") not in {"prepared", "committed"}
            ):
                raise HTTPException(
                    status_code=409,
                    detail="A conflicting Skill deletion recovery record exists",
                )
        else:
            deleted_names = [
                str(item)
                for item in journal.get("member_names", [])
                if isinstance(item, str)
            ]
            runtime_bundle_ids = [
                str(item)
                for item in journal.get("runtime_bundle_ids", [])
                if isinstance(item, str)
            ]
            if not deleted_names or name not in deleted_names:
                raise HTTPException(
                    status_code=409,
                    detail="Skill deletion recovery record is incomplete",
                )
            if len(deleted_names) > 1 and not delete_bundle:
                raise HTTPException(
                    status_code=409,
                    detail="Retry bundle deletion with delete_bundle=true",
                )

        caller_cancellation: BaseException | None = None
        committed = not registry_present
        if registry_present:
            try:
                quarantine_skills = quarantine_root / "skills"
                for member_name in deleted_names:
                    canonical = scope_root / member_name
                    quarantined = quarantine_skills / member_name
                    expected_digest = str(
                        journal["member_digests"].get(member_name) or ""
                    )
                    if quarantined.exists():
                        if canonical.exists() or canonical.is_symlink():
                            raise HTTPException(
                                status_code=409,
                                detail=(
                                    f"Both canonical and quarantined copies exist "
                                    f"for Skill '{member_name}'"
                                ),
                            )
                        if _directory_digest(quarantined) != expected_digest:
                            raise HTTPException(
                                status_code=409,
                                detail=f"Quarantined Skill '{member_name}' drifted",
                            )
                        continue
                    if _directory_digest(canonical) != expected_digest:
                        raise HTTPException(
                            status_code=409,
                            detail=f"Skill '{member_name}' changed during deletion",
                        )
                    quarantined.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(canonical, quarantined)

                quarantine_runtime = quarantine_root / "runtime"
                runtime_digests = journal.get("runtime_digests") or {}
                for owner in runtime_bundle_ids:
                    canonical = scope_root / "_bundle_runtime" / owner
                    quarantined = quarantine_runtime / owner
                    expected_digest = str(runtime_digests.get(owner) or "")
                    if quarantined.exists():
                        if canonical.exists() or canonical.is_symlink():
                            raise HTTPException(
                                status_code=409,
                                detail="Bundle runtime exists in two locations",
                            )
                        if (
                            expected_digest
                            and _directory_digest(quarantined) != expected_digest
                        ):
                            raise HTTPException(
                                status_code=409,
                                detail="Quarantined bundle runtime drifted",
                            )
                        continue
                    if not canonical.exists():
                        continue
                    if (
                        expected_digest
                        and _directory_digest(canonical) != expected_digest
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="Bundle runtime changed during deletion",
                        )
                    quarantined.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(canonical, quarantined)

                for skill in cohort:
                    await db.delete(skill)
                caller_cancellation = (
                    await _finish_awaitable_despite_cancellation(db.commit())
                )
                committed = True
                journal["state"] = "committed"
                _atomic_write_json(journal_path, journal)
            except BaseException:
                if not committed:
                    await _rollback_database_best_effort(db)
                    restored = _restore_quarantined_paths(
                        quarantine_root=quarantine_root,
                        scope_root=scope_root,
                        member_names=deleted_names,
                        runtime_bundle_ids=runtime_bundle_ids,
                    )
                    if restored:
                        shutil.rmtree(operation_dir, ignore_errors=True)
                raise

        mcp_cleanups: dict[str, dict] = {}
        for member_name in deleted_names:
            cleanup, cleanup_cancellation = (
                await _finish_awaitable_with_result(_remove_skill_mcp(
                    str(scope_root / member_name),
                    user.id,
                    session_id or "default",
                ))
            )
            if isinstance(cleanup, dict):
                mcp_cleanups[member_name] = cleanup
            else:
                mcp_cleanups[member_name] = {
                    "success": False,
                    "error": "Invalid MCP cleanup response",
                }
            caller_cancellation = (
                caller_cancellation or cleanup_cancellation
            )

        # Quarantined content is no longer reachable by the loader. Removing it
        # only after DB and MCP completion closes the DB-first crash window.
        shutil.rmtree(quarantine_root, ignore_errors=True)
        journal["state"] = "completed"
        journal["mcp_status"] = (
            "degraded"
            if any(
                cleanup.get("success") is False
                for cleanup in mcp_cleanups.values()
            )
            else "reconciled"
        )
        _atomic_write_json(journal_path, journal)

    # Invalidate skills cache
    try:
        _invalidate_skills_cache(user.id)
    except Exception:
        pass

    if caller_cancellation is not None:
        raise caller_cancellation

    return {
        "success": True,
        "name": name,
        "session_id": session_id,
        "action": "bundle_deleted" if len(deleted_names) > 1 else "deleted",
        "deleted_skills": deleted_names,
        "bundle_id": metadata.get("bundle_id"),
        "mcp": mcp_cleanups.get(name, {}),
        "mcp_by_skill": mcp_cleanups,
        "idempotent": not registry_present,
        "operation_status": "completed",
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

    # Promotion spans two scopes; always acquire them in user→session order so
    # concurrent installs cannot deadlock or observe an intermediate registry.
    async with _skill_install_lock(user.id, None):
        async with _skill_install_lock(user.id, session_id):
            conv = (await db.execute(
                select(Conversation).where(
                    Conversation.id == session_id,
                    Conversation.user_id == user.id,
                )
            )).scalar_one_or_none()
            if conv is None:
                raise HTTPException(404, "Conversation not found")

            operation_dir = _skill_operation_dir(
                user_id=user.id,
                kind="promote",
                identity_parts=[session_id, name],
            )
            journal_path = operation_dir / "journal.json"
            journal = _read_operation_journal(journal_path)
            session_skill: SkillPackage | None = None
            cohort: list[SkillPackage] = []
            metadata: dict[str, str | None] = {}
            try:
                session_skill, cohort, metadata = (
                    await _bundle_members_for_management(
                        name=name,
                        session_id=session_id,
                        user=user,
                        db=db,
                    )
                )
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise

            existing_user_skill = (await db.execute(
                select(SkillPackage).where(
                    SkillPackage.user_id == user.id,
                    SkillPackage.name == name,
                    SkillPackage.session_id.is_(None),
                )
            )).scalar_one_or_none()

            recovering_committed = (
                session_skill is None
                and existing_user_skill is not None
                and isinstance(journal, dict)
                and journal.get("version") == 1
                and journal.get("kind") == "promote"
                and journal.get("user_id") == user.id
                and journal.get("source_session_id") == session_id
                and journal.get("name") == name
                and journal.get("bundle_id")
                == str(existing_user_skill.bundle_id or "")
                and journal.get("state")
                in {"published", "committed", "completed"}
            )
            if session_skill is None and not recovering_committed:
                raise HTTPException(404, f"Session skill '{name}' not found")

            if session_skill is not None and metadata.get(
                "bundle_role"
            ) == "supporting":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Supporting Skill '{name}' cannot be promoted independently"
                    ),
                )
            if session_skill is not None and len(cohort) > 1:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Primary Skill '{name}' belongs to a multi-member bundle; "
                        "bundle promotion is not supported"
                    ),
                )

            if existing_user_skill is not None and not recovering_committed:
                raise HTTPException(
                    409,
                    f"User-level skill '{name}' already exists",
                )

            src_dir = SKILLS_DATA_DIR / user.id / session_id / name
            dst_dir = SKILLS_DATA_DIR / user.id / name
            source_scope = _scope_skills_dir(user.id, session_id)
            target_scope = _scope_skills_dir(user.id, None)
            caller_cancellation: BaseException | None = None
            committed = recovering_committed
            published_ownership: tuple[Path, int, int] | None = None
            runtime_ownership: tuple[Path, int, int] | None = None
            target_runtime: Path | None = None

            if session_skill is not None:
                # A completed receipt belongs to an older generation when a
                # session row exists again.
                if isinstance(journal, dict) and journal.get(
                    "state"
                ) == "completed":
                    shutil.rmtree(operation_dir, ignore_errors=True)
                    journal = None

                if journal is None:
                    if not src_dir.exists() or src_dir.is_symlink():
                        raise HTTPException(
                            404,
                            "Session Skill directory is missing",
                        )
                    if dst_dir.exists() or dst_dir.is_symlink():
                        raise HTTPException(
                            409,
                            "User-level Skill directory already exists",
                        )
                    if operation_dir.exists():
                        shutil.rmtree(operation_dir, ignore_errors=True)
                    bundle_id = str(session_skill.bundle_id or "")
                    target_runtime_before = (
                        target_scope / "_bundle_runtime" / bundle_id
                        if bundle_id
                        else None
                    )
                    staging_root = operation_dir / "staging"
                    staging_skill = staging_root / "skill"
                    try:
                        staging_root.mkdir(parents=True, exist_ok=False)
                        shutil.copytree(src_dir, staging_skill)
                        source_runtime = (
                            source_scope / "_bundle_runtime" / bundle_id
                            if bundle_id
                            else None
                        )
                        if (
                            source_runtime is not None
                            and source_runtime.is_dir()
                        ):
                            shutil.copytree(
                                source_runtime,
                                staging_root / "runtime",
                            )
                    except BaseException:
                        shutil.rmtree(operation_dir, ignore_errors=True)
                        raise
                    journal = {
                        "version": 1,
                        "kind": "promote",
                        "user_id": user.id,
                        "source_session_id": session_id,
                        "name": name,
                        "bundle_id": bundle_id,
                        "skill_digest": _directory_digest(staging_skill),
                        "runtime_digest": (
                            _directory_digest(staging_root / "runtime")
                            if (staging_root / "runtime").is_dir()
                            else ""
                        ),
                        "target_runtime_preexisting": bool(
                            target_runtime_before is not None
                            and target_runtime_before.exists()
                        ),
                        "state": "prepared",
                    }
                    _atomic_write_json(journal_path, journal)
                elif (
                    journal.get("state") not in {"prepared", "published"}
                    or journal.get("bundle_id")
                    != str(session_skill.bundle_id or "")
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="A conflicting Skill promotion recovery record exists",
                    )

                try:
                    staging_skill = operation_dir / "staging" / "skill"
                    expected_skill_digest = str(
                        journal.get("skill_digest") or ""
                    )
                    if dst_dir.exists():
                        if (
                            not dst_dir.is_dir()
                            or _directory_digest(dst_dir)
                            != expected_skill_digest
                        ):
                            raise HTTPException(
                                409,
                                "Published user-level Skill does not match recovery record",
                            )
                    else:
                        if (
                            not staging_skill.is_dir()
                            or _directory_digest(staging_skill)
                            != expected_skill_digest
                        ):
                            raise HTTPException(
                                409,
                                "Staged Skill does not match recovery record",
                            )
                        published_ownership = _publish_staged_directory(
                            staging_skill,
                            dst_dir,
                        )

                    bundle_id = str(journal.get("bundle_id") or "")
                    staged_runtime = operation_dir / "staging" / "runtime"
                    target_runtime = (
                        target_scope / "_bundle_runtime" / bundle_id
                        if bundle_id
                        else None
                    )
                    expected_runtime_digest = str(
                        journal.get("runtime_digest") or ""
                    )
                    if target_runtime is not None and expected_runtime_digest:
                        if target_runtime.exists():
                            if (
                                not target_runtime.is_dir()
                                or _directory_digest(target_runtime)
                                != expected_runtime_digest
                            ):
                                raise HTTPException(
                                    409,
                                    "User-level bundle runtime conflicts with promotion",
                                )
                        else:
                            target_runtime.parent.mkdir(
                                parents=True,
                                exist_ok=True,
                            )
                            runtime_ownership = _publish_staged_directory(
                                staged_runtime,
                                target_runtime,
                            )

                    journal["state"] = "published"
                    _atomic_write_json(journal_path, journal)
                    db.add(SkillPackage(
                        user_id=user.id,
                        name=name,
                        session_id=None,
                        description=session_skill.description,
                        category=session_skill.category,
                        version=session_skill.version,
                        bundle_id=session_skill.bundle_id,
                        bundle_role=session_skill.bundle_role,
                        bundle_root_name=session_skill.bundle_root_name,
                        bundle_source_path=session_skill.bundle_source_path,
                    ))
                    await db.delete(session_skill)

                    enabled_list = []
                    if conv.enabled_user_skills:
                        try:
                            parsed = json.loads(conv.enabled_user_skills)
                            if isinstance(parsed, list):
                                enabled_list = [
                                    str(item) for item in parsed if item
                                ]
                        except (json.JSONDecodeError, TypeError):
                            enabled_list = []
                    if name not in enabled_list:
                        enabled_list.append(name)
                        conv.enabled_user_skills = json.dumps(
                            enabled_list,
                            ensure_ascii=False,
                        )

                    caller_cancellation = (
                        await _finish_awaitable_despite_cancellation(
                            db.commit()
                        )
                    )
                    committed = True
                    journal["state"] = "committed"
                    _atomic_write_json(journal_path, journal)
                    existing_user_skill = (await db.execute(
                        select(SkillPackage).where(
                            SkillPackage.user_id == user.id,
                            SkillPackage.name == name,
                            SkillPackage.session_id.is_(None),
                        )
                    )).scalar_one()
                except BaseException:
                    if not committed:
                        await _rollback_database_best_effort(db)
                        if published_ownership is not None:
                            _remove_request_owned_directory(
                                published_ownership
                            )
                        elif (
                            dst_dir.is_dir()
                            and _directory_digest(dst_dir)
                            == str(journal.get("skill_digest") or "")
                        ):
                            shutil.rmtree(dst_dir, ignore_errors=True)
                        if runtime_ownership is not None:
                            _remove_request_owned_directory(
                                runtime_ownership
                            )
                        elif (
                            target_runtime is not None
                            and not journal.get(
                                "target_runtime_preexisting"
                            )
                            and target_runtime.is_dir()
                            and _directory_digest(target_runtime)
                            == str(journal.get("runtime_digest") or "")
                        ):
                            shutil.rmtree(
                                target_runtime,
                                ignore_errors=True,
                            )
                        shutil.rmtree(operation_dir, ignore_errors=True)
                    raise

            if not isinstance(journal, dict) or existing_user_skill is None:
                raise HTTPException(
                    status_code=409,
                    detail="Promotion recovery state is incomplete",
                )
            if (
                not dst_dir.is_dir()
                or _directory_digest(dst_dir)
                != str(journal.get("skill_digest") or "")
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Committed promoted Skill directory is incomplete",
                )

            # Complete every post-commit side effect while both source and
            # target locks remain held. A retry with the same session/name can
            # resume from the durable journal after a process crash.
            shutil.rmtree(src_dir, ignore_errors=True)
            bundle_id = str(journal.get("bundle_id") or "")
            if bundle_id:
                shutil.rmtree(
                    source_scope / "_bundle_runtime" / bundle_id,
                    ignore_errors=True,
                )
            session_mcp_cleanup, cleanup_cancellation = (
                await _finish_awaitable_with_result(_remove_skill_mcp(
                    str(src_dir),
                    user.id,
                    session_id,
                ))
            )
            caller_cancellation = (
                caller_cancellation or cleanup_cancellation
            )
            promoted_skill = {
                "name": name,
                "description": existing_user_skill.description or "",
                "category": existing_user_skill.category,
                "version": existing_user_skill.version or "",
                "session_id": None,
                "bundle_id": existing_user_skill.bundle_id,
                "bundle_role": existing_user_skill.bundle_role,
                "bundle_root_name": existing_user_skill.bundle_root_name,
                "bundle_source_path": existing_user_skill.bundle_source_path,
            }
            mcp_state_value, rebuild_cancellation = (
                await _finish_awaitable_with_result(
                    _rebuild_mcp_for_skills(
                        skills=[promoted_skill],
                        target_dirs={name: dst_dir},
                        user_id=user.id,
                        session_id=None,
                        reason=(
                            "promotion_recovery"
                            if recovering_committed
                            else "promotion"
                        ),
                    )
                )
            )
            caller_cancellation = (
                caller_cancellation or rebuild_cancellation
            )
            if not isinstance(mcp_state_value, dict):
                raise RuntimeError("Invalid MCP promotion reconciliation result")
            mcp_state = mcp_state_value
            shutil.rmtree(operation_dir / "staging", ignore_errors=True)
            journal["state"] = "completed"
            journal["mcp_status"] = str(
                (mcp_state.get("mcp") or {}).get("status") or "unknown"
            )
            _atomic_write_json(journal_path, journal)

    try:
        _invalidate_skills_cache(user.id)
    except Exception:
        pass

    if caller_cancellation is not None:
        raise caller_cancellation

    return {
        "success": True,
        "name": name,
        "promoted_to": "user",
        "mcp_cleanup": session_mcp_cleanup,
        "idempotent": recovering_committed,
        "operation_status": "completed",
        **mcp_state,
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
                f"{settings.harness_url}/internal/mcp/auto-register",
                headers={
                    "X-Internal-Token": settings.internal_api_token,
                },
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
                f"{settings.harness_url}/internal/mcp/remove-skill",
                headers={
                    "X-Internal-Token": settings.internal_api_token,
                },
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
