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
import hashlib
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

from skills.path_safety import (
    validate_skill_resource,
    validate_skill_root,
)

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
RUNNABLE_SKILL_SCRIPT_EXTENSIONS = frozenset({
    ".py", ".sh", ".bash", ".js", ".mjs", ".cjs",
})
MAX_RUNNABLE_SCRIPT_BYTES = 10_000_000
# Collection roots may contain arbitrary category/grouping directories.  Keep
# discovery domain-neutral while bounding traversal against pathological trees.
MAX_SKILL_SCAN_DEPTH = 32


def is_excluded_path(path: Path) -> bool:
    """True if any component of *path* is in EXCLUDED_SKILL_DIRS."""
    return any(part in EXCLUDED_SKILL_DIRS for part in path.parts)


def iter_skill_index_files(skills_dir: Path, filename: str = "SKILL.md") -> Iterator[Path]:
    """Walk *skills_dir* yielding sorted paths matching *filename*.

    Excludes VCS, virtualenv, and cache directories.
    """
    root_check = validate_skill_root(skills_dir)
    if not root_check.valid or root_check.path is None:
        return
    scan_root = root_check.path
    matches: list[Path] = []
    for walk_root, dirs, files in os.walk(scan_root, followlinks=False):
        walk_path = Path(walk_root)
        try:
            walk_depth = len(walk_path.relative_to(scan_root).parts)
        except ValueError:
            dirs[:] = []
            continue
        if walk_depth >= MAX_SKILL_SCAN_DEPTH:
            dirs[:] = []
        retained_dirs: list[str] = []
        for directory in dirs:
            if directory in EXCLUDED_SKILL_DIRS:
                continue
            checked_dir = validate_skill_resource(
                scan_root,
                walk_path / directory,
                expected_kind="directory",
            )
            if checked_dir.valid:
                retained_dirs.append(directory)
        dirs[:] = retained_dirs
        if filename not in files:
            continue
        checked_file = validate_skill_resource(
            scan_root,
            walk_path / filename,
            expected_kind="file",
        )
        if checked_file.valid and checked_file.path is not None:
            matches.append(checked_file.path)
    yield from sorted(matches, key=lambda path: str(path.relative_to(scan_root)))


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
    if not (2 <= len(rel_parts) <= MAX_SKILL_SCAN_DEPTH + 1):
        return False
    user_root_check = validate_skill_root(USER_SKILLS_BASE / user_id)
    user_root = user_root_check.path if user_root_check.valid else None
    if user_root is not None and scan_dir == user_root and _SESSION_DIR_RE.fullmatch(rel_parts[0]):
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
    from skills.loader import (
        FrontmatterParseError,
        parse_frontmatter,
        read_skill_frontmatter_source,
        validate_skill_manifest,
    )

    skills: list[dict] = []
    # Keep the winning record, not only its name, so a shadowed declaration
    # can leave an actionable diagnostic on the deterministically selected
    # package.  Scan-root priority and the sorted relative path order from
    # ``iter_skill_index_files`` together define the winner.
    seen_skills: dict[str, dict] = {}
    user_dir = USER_SKILLS_BASE / user_id
    session_dir = USER_SKILLS_BASE / user_id / session_id if session_id and session_id != "default" else None

    for scan_dir in get_skills_dirs(user_id, session_id, include_optional=include_optional):
        scan_root_check = validate_skill_root(scan_dir)
        if not scan_root_check.valid or scan_root_check.path is None:
            continue
        scan_root = scan_root_check.path
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
            if not _is_allowed_candidate(skill_md, scan_root, user_id):
                continue

            skill_dir = skill_md.parent

            try:
                content = read_skill_frontmatter_source(skill_md)
                frontmatter, _body = parse_frontmatter(content, strict=True)
                manifest_diagnostics = validate_skill_manifest(
                    frontmatter,
                    directory_name=skill_dir.name,
                    enforce_directory_match=True,
                )
            except (UnicodeDecodeError, PermissionError, OSError, FrontmatterParseError):
                continue
            except Exception:
                continue
            if manifest_diagnostics.get("errors"):
                continue

            name = frontmatter["name"]
            diagnostics: list[dict[str, str]] = []
            for level in ("warnings", "info"):
                diagnostics.extend(
                    {**item, "level": level[:-1] if level.endswith("s") else level}
                    for item in manifest_diagnostics.get(level) or []
                    if isinstance(item, dict)
                )

            # Filter user-level skills by enabled_user_skills whitelist
            if is_user_level_scan and enabled_user_skills is not None:
                if name not in enabled_user_skills:
                    continue

            if name in seen_skills:
                winner = seen_skills[name]
                winner.setdefault("diagnostics", []).append({
                    "code": "duplicate_skill_name_shadowed",
                    "level": "warning",
                    "message": (
                        "Another package declares the same canonical Skill "
                        "name and was ignored by deterministic scan priority."
                    ),
                    "ignored_path": str(skill_md),
                    "ignored_scope": scope,
                })
                continue

            description = frontmatter["description"]

            # Extract category from directory structure
            try:
                rel = skill_md.relative_to(scan_root)
                parts = rel.parts
                category = parts[0] if len(parts) >= 3 else None
            except ValueError:
                category = None

            skill = {
                "name": name,
                "description": description,
                "category": category,
                "scope": scope,
                "path": str(skill_md),
                "skill_dir": str(skill_dir),
            }
            if diagnostics:
                skill["diagnostics"] = diagnostics
            seen_skills[name] = skill
            skills.append(skill)

    skills.sort(key=lambda s: (s.get("category") or "", s["name"]))
    return skills


def resolve_skill_path(
    name: str,
    user_id: str = "default",
    session_id: str = "default",
    include_optional: bool = False,
    enabled_user_skills: list[str] | None = None,
) -> Optional[Path]:
    """Resolve a skill name to its SKILL.md path.

    Searches directories in priority order (session > user > builtin > optional).
    Returns the path to SKILL.md or None if not found.
    """
    candidate_name = str(name or "")
    if not candidate_name:
        return None

    # Resolve from the same canonical registry that is exposed by the
    # scanner.  Previously discovery advertised the frontmatter ``name``
    # while resolution searched only directory names.  Apart from making
    # valid Skills unloadable, a directory whose own declaration named a
    # different Skill could steal the lookup.  Canonical-name matching makes
    # every advertised entry resolvable and deliberately does not create an
    # ambiguous directory-name alias.
    for skill in find_all_skills(
        user_id,
        session_id,
        include_optional=include_optional,
        enabled_user_skills=enabled_user_skills,
    ):
        if skill.get("name") != candidate_name:
            continue
        path = Path(str(skill.get("path") or ""))
        if path:
            return path

    return None


def skill_runnable_script_resources(
    name: str,
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return exact ``(relative_path, sha256)`` runnable script resources.

    The inventory is rooted at the exact canonical package selected by
    ``find_all_skills`` (session > enabled user > builtin).  It is therefore a
    closed list of concrete files and content hashes, not ambient authority to
    traverse a scope directory.  Dispatch revalidates the same canonical
    package and hash before the isolated executor receives a snapshot.

    Scripts need not have their Unix executable bit set because the harness
    selects an allowlisted interpreter from the extension. Symlinks,
    excluded/cache directories, and paths escaping the resolved session root
    are ignored.
    """
    candidate_name = str(name or "").strip()
    rel = PurePosixPath(candidate_name)
    if (
        not candidate_name
        or rel.is_absolute()
        or any(part in {"", ".", ".."} for part in rel.parts)
    ):
        return ()

    # Runnable authority is stricter than catalog discovery.  A missing
    # runtime-owned whitelist never means "all private Skills"; it means no
    # user-scope package is executable. Session and builtin packages remain
    # discoverable and still require an exact content-hash grant at dispatch.
    executable_user_skills = (
        list(enabled_user_skills) if enabled_user_skills is not None else []
    )
    skill_md = resolve_skill_path(
        candidate_name,
        user_id,
        session_id,
        enabled_user_skills=executable_user_skills,
    )
    if skill_md is None:
        return ()

    # Canonical Agent-Skills names need not equal their physical directory
    # names (and valid packages may live below a category directory).  Resolve
    # the advertised canonical identity first, then inventory only below that
    # exact validated package root.
    execution_root_path = skill_md.parent
    execution_skill_md = execution_root_path / "SKILL.md"
    try:
        execution_root = execution_root_path.resolve(strict=True)
        resolved_skill_md = skill_md.resolve(strict=True)
        resolved_execution_skill_md = execution_skill_md.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return ()

    if resolved_skill_md != resolved_execution_skill_md:
        return ()

    resources: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(execution_root_path, followlinks=False):
        root_path = Path(root)
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in EXCLUDED_SKILL_DIRS
            and not directory.startswith(".")
            and not (root_path / directory).is_symlink()
        ]
        for filename in files:
            suffix = Path(filename).suffix
            if (
                suffix not in RUNNABLE_SKILL_SCRIPT_EXTENSIONS
                or filename.startswith(".")
            ):
                continue
            path = root_path / filename
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(execution_root)
                if resolved.stat().st_size > MAX_RUNNABLE_SCRIPT_BYTES:
                    continue
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                continue
            if path.is_symlink() or not resolved.is_file():
                continue
            try:
                digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            except OSError:
                continue
            resources.append((str(resolved.relative_to(execution_root)), digest))
    return tuple(sorted(resources))


def skill_runnable_script_extensions(
    name: str,
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
) -> frozenset[str]:
    """Return safe runnable script suffixes for compatibility/tool selection."""
    return frozenset(
        Path(relative_path).suffix
        for relative_path, _digest in skill_runnable_script_resources(
            name,
            user_id,
            session_id,
            enabled_user_skills,
        )
    )


def skill_has_runnable_python(
    name: str,
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
) -> bool:
    """Return whether *name* has a Python entrypoint runnable by the bridge."""
    return ".py" in skill_runnable_script_extensions(
        name,
        user_id,
        session_id,
        enabled_user_skills,
    )
