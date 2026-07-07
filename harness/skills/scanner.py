"""Skill scanner — discover SKILL.md files across skills directories.

Simplified from hermes-agent/agent/skill_utils.py:
- No platform matching (server-side Linux only)
- No external skills dirs
- No disabled-skills config
- No plugin/namespace system

Scan paths (priority order):
  1. data/skills/<user_id>/<session_id>/ — session-specific skills (highest)
  2. data/skills/<user_id>/   — user-created private skills
  3. harness/skills/builtin/   — shared built-in skills
  4. harness/skills/optional/  — optional skills (not loaded by default)
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator, Optional

# Directories excluded from skill scanning.
EXCLUDED_SKILL_DIRS = frozenset({
    ".git", ".github", ".hub", ".archive",
    ".venv", "venv", "node_modules", "site-packages",
    "__pycache__", ".tox", ".nox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})

# Default scan roots relative to harness directory.
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "builtin"
OPTIONAL_SKILLS_DIR = Path(__file__).resolve().parent / "optional"
USER_SKILLS_BASE = Path("data/skills")
_SESSION_DIR_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def is_excluded_path(path: Path) -> bool:
    """True if any component of *path* is in EXCLUDED_SKILL_DIRS."""
    return any(part in EXCLUDED_SKILL_DIRS for part in path.parts)


def iter_skill_index_files(skills_dir: Path, filename: str = "SKILL.md") -> Iterator[Path]:
    """Walk *skills_dir* yielding sorted paths matching *filename*.

    Excludes VCS, virtualenv, and cache directories.
    """
    if not skills_dir.is_dir():
        return
    matches = []
    for root, dirs, files in os.walk(skills_dir, followlinks=True):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS]
        if filename in files:
            matches.append(Path(root) / filename)
    for path in sorted(matches, key=lambda p: str(p.relative_to(skills_dir))):
        yield path


def _is_allowed_candidate(
    skill_md: Path,
    scan_dir: Path,
    user_id: str,
) -> bool:
    """Allow direct/category skills while excluding sibling session trees."""
    try:
        rel_parts = skill_md.relative_to(scan_dir).parts
    except ValueError:
        return False
    if len(rel_parts) not in (2, 3):
        return False
    user_root = USER_SKILLS_BASE / user_id
    if scan_dir == user_root and _SESSION_DIR_RE.fullmatch(rel_parts[0]):
        return False
    return True


def get_skills_dirs(user_id: str = "default", session_id: str = "default", include_optional: bool = False) -> list[Path]:
    """Return the ordered list of skills directories to scan.

    Args:
        user_id: User identifier for per-user skills isolation.
        session_id: Session identifier for per-session skills isolation.
        include_optional: If True, include the optional skills directory.

    Returns:
        List of existing directory paths in priority order.
    """
    dirs: list[Path] = []

    # 0. Session-specific skills (highest priority — per-session isolation)
    if session_id and session_id != "default":
        session_dir = USER_SKILLS_BASE / user_id / session_id
        if session_dir.is_dir():
            dirs.append(session_dir)

    # 1. User private skills (can shadow builtins)
    user_dir = USER_SKILLS_BASE / user_id
    if user_dir.is_dir():
        dirs.append(user_dir)

    # 2. Built-in shared skills
    if BUILTIN_SKILLS_DIR.is_dir():
        dirs.append(BUILTIN_SKILLS_DIR)

    # 3. Optional skills (lowest priority, only when requested)
    if include_optional and OPTIONAL_SKILLS_DIR.is_dir():
        dirs.append(OPTIONAL_SKILLS_DIR)

    return dirs


def find_all_skills(
    user_id: str = "default",
    session_id: str = "default",
    include_optional: bool = False,
    enabled_user_skills: list[str] | None = None,
) -> list[dict]:
    """Find all skills across configured directories.

    When a skill with the same name exists in multiple directories,
    the first occurrence wins (session > user > builtin > optional).

    Args:
        user_id: User identifier for per-user skills isolation.
        session_id: Session identifier for per-session skills isolation.
        include_optional: If True, also scan optional skills.
        enabled_user_skills: Whitelist of user-level skill names to include.
            When provided, user-level skills not in this list are excluded.
            Session-level, builtin, and optional skills are always included.

    Returns:
        List of skill metadata dicts with keys: name, description, category, path.
    """
    from skills.loader import parse_frontmatter

    skills: list[dict] = []
    seen_names: set[str] = set()
    user_dir = USER_SKILLS_BASE / user_id
    session_dir = USER_SKILLS_BASE / user_id / session_id if session_id and session_id != "default" else None

    for scan_dir in get_skills_dirs(user_id, session_id, include_optional=include_optional):
        is_user_level_scan = (scan_dir == user_dir)
        is_session_scan = (session_dir is not None and scan_dir == session_dir)
        is_builtin_scan = (scan_dir == BUILTIN_SKILLS_DIR)
        is_optional_scan = (scan_dir == OPTIONAL_SKILLS_DIR)
        if is_session_scan:
            scope = "session"
        elif is_user_level_scan:
            scope = "user"
        elif is_builtin_scan:
            scope = "builtin"
        elif is_optional_scan:
            scope = "optional"
        else:
            scope = "unknown"
        for skill_md in iter_skill_index_files(scan_dir, "SKILL.md"):
            if is_excluded_path(skill_md):
                continue
            if not _is_allowed_candidate(skill_md, scan_dir, user_id):
                continue

            skill_dir = skill_md.parent

            try:
                content = skill_md.read_text(encoding="utf-8")[:4000]
                frontmatter, body = parse_frontmatter(content)
            except (UnicodeDecodeError, PermissionError):
                continue
            except Exception:
                continue

            name = str(frontmatter.get("name") or skill_dir.name)[:64]

            # Filter user-level skills by enabled_user_skills whitelist
            if is_user_level_scan and enabled_user_skills is not None:
                if name not in enabled_user_skills:
                    continue

            if name in seen_names:
                continue

            description = str(frontmatter.get("description", ""))
            if not description:
                # Fall back to first non-heading, non-empty line of body
                for line in body.strip().split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        description = line
                        break

            if len(description) > 1024:
                description = description[:1021] + "..."

            # Extract category from directory structure
            try:
                rel = skill_md.relative_to(scan_dir)
                parts = rel.parts
                category = parts[0] if len(parts) >= 3 else None
            except ValueError:
                category = None

            seen_names.add(name)
            skills.append({
                "name": name,
                "description": description,
                "category": category,
                "scope": scope,
                "path": str(skill_md),
                "skill_dir": str(skill_dir),
            })

    skills.sort(key=lambda s: (s.get("category") or "", s["name"]))
    return skills


def resolve_skill_path(
    name: str,
    user_id: str = "default",
    session_id: str = "default",
    include_optional: bool = False,
) -> Optional[Path]:
    """Resolve a skill name to its SKILL.md path.

    Searches directories in priority order (session > user > builtin > optional).
    Returns the path to SKILL.md or None if not found.
    """
    for scan_dir in get_skills_dirs(user_id, session_id, include_optional=include_optional):
        # Direct path match
        direct = scan_dir / name / "SKILL.md"
        if (
            direct.exists()
            and not is_excluded_path(direct)
            and _is_allowed_candidate(direct, scan_dir, user_id)
        ):
            return direct

        # Recursive search by directory name
        for found in iter_skill_index_files(scan_dir, "SKILL.md"):
            if (
                found.parent.name == name
                and not is_excluded_path(found)
                and _is_allowed_candidate(found, scan_dir, user_id)
            ):
                return found

    return None
