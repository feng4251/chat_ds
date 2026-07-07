"""Skill management tool — agent-driven skill CRUD.

Allows the agent to create, update, and delete skills, turning successful
approaches into reusable procedural knowledge.

Simplified from hermes-agent/tools/skill_manager_tool.py:
- No multi-profile support
- No security scanning (skills_guard)
- No pinned-skill protection
- No telemetry/skill_usage
- Skills stored in data/skills/<user_id>/
- No credential file registration
- No env passthrough

Actions:
  create     — Create a new skill (SKILL.md + directory structure)
  edit       — Replace the SKILL.md content of a user skill (full rewrite)
  patch      — Targeted find-and-replace within SKILL.md or any supporting file
  delete     — Remove a user skill entirely
  write_file — Add/overwrite a supporting file (reference, template, script, asset)
  remove_file— Remove a supporting file from a user skill
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from skills.scanner import USER_SKILLS_BASE, EXCLUDED_SKILL_DIRS
from skills.manager import get_manager

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000  # ~36k tokens at 2.75 chars/token
MAX_SKILL_FILE_BYTES = 1_048_576   # 1 MiB per supporting file

# Characters allowed in skill names (filesystem-safe, URL-friendly)
VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SESSION_DIR_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)

# Subdirectories allowed for write_file/remove_file
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}


# ── Validation helpers ────────────────────────────────────────────────────

def _validate_name(name: str) -> str | None:
    """Validate a skill name. Returns error message or None if valid."""
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


def _validate_category(category: str | None) -> str | None:
    """Validate an optional category name."""
    if category is None:
        return None
    if not isinstance(category, str):
        return "Category must be a string."
    category = category.strip()
    if not category:
        return None
    if "/" in category or "\\" in category:
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    if len(category) > MAX_NAME_LENGTH:
        return f"Category exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(category):
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores."
        )
    return None


def _validate_frontmatter(content: str) -> str | None:
    """Validate that SKILL.md content has proper frontmatter."""
    if not content.strip():
        return "Content cannot be empty."
    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    yaml_content = content[3:end_match.start() + 3]
    from skills.loader import parse_frontmatter
    frontmatter, _ = parse_frontmatter(content)

    if not frontmatter:
        return "Could not parse YAML frontmatter."

    if "name" not in frontmatter:
        return "Frontmatter must include 'name' field."
    if "description" not in frontmatter:
        return "Frontmatter must include 'description' field."
    if len(str(frontmatter["description"])) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."

    body = content[end_match.end() + 3:].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> str | None:
    """Check content doesn't exceed the character limit."""
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files."
        )
    return None


def _validate_file_path(file_path: str) -> str | None:
    """Validate a file_path for write_file/remove_file."""
    if not file_path:
        return "file_path is required."
    if ".." in file_path:
        return "Path traversal ('..') is not allowed."
    normalized = Path(file_path)
    if not normalized.parts or normalized.parts[0] not in ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"
    if len(normalized.parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{normalized.parts[0]}/myfile.md'"
    return None


# ── Path helpers ──────────────────────────────────────────────────────────

def _skills_dir_for(
    user_id: str,
    session_id: str = "default",
    scope: str = "session",
) -> Path:
    """Return the exact mutable skill scope; never search sibling sessions."""
    base = USER_SKILLS_BASE / user_id
    if scope == "user":
        return base
    return base / session_id


def _resolve_skill_dir(name: str, skills_base: Path, category: str | None = None) -> Path:
    """Build the directory path for a new skill."""
    if category:
        return skills_base / category / name
    return skills_base / name


def _find_skill(name: str, skills_base: Path) -> dict[str, Any] | None:
    """Find a skill by name only within the selected mutable scope.

    Returns {"path": Path} or None.
    """
    if not skills_base.is_dir():
        return None
    direct = skills_base / name / "SKILL.md"
    if direct.is_file():
        return {"path": direct.parent}

    is_user_scope = skills_base.parent == USER_SKILLS_BASE
    for category_dir in skills_base.iterdir():
        if not category_dir.is_dir():
            continue
        if category_dir.name in EXCLUDED_SKILL_DIRS:
            continue
        if is_user_scope and SESSION_DIR_RE.fullmatch(category_dir.name):
            continue
        candidate = category_dir / name / "SKILL.md"
        if candidate.is_file():
            return {"path": candidate.parent}
    return None


def _atomic_write_text(file_path: Path, content: str) -> None:
    """Atomically write text content using temp file + rename."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".{file_path.name}.tmp.",
        suffix="",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, file_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


# ── Core actions ──────────────────────────────────────────────────────────

async def _create_skill(
    name: str,
    content: str,
    user_id: str,
    session_id: str,
    scope: str,
    skills_base: Path,
    category: str | None = None,
) -> dict[str, Any]:
    """Create a new skill in one explicit scope."""
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}

    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}

    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name, skills_base)
    if existing:
        return {
            "success": False,
            "error": f"A skill named '{name}' already exists at {existing['path']}.",
        }

    skill_dir = _resolve_skill_dir(name, skills_base, category)
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_md = skill_dir / "SKILL.md"
    _atomic_write_text(skill_md, content)

    # Invalidate skills cache
    get_manager().invalidate(user_id)

    # Auto-register MCP servers from the skill
    try:
        from tools.mcp_auto import auto_register_skill_mcp
        mcp_result = await auto_register_skill_mcp(
            str(skill_dir), user_id,
            session_id if scope == "session" else "default",
        )
        if mcp_result.get("registered"):
            logger.info(
                "Auto MCP for skill '%s' (user=%s): registered=%s",
                name, user_id, mcp_result["registered"],
            )
    except Exception:
        logger.exception("Auto MCP registration failed for skill '%s'", name)

    try:
        rel_path = str(skill_dir.relative_to(skills_base))
    except ValueError:
        rel_path = str(skill_dir)

    result: dict[str, Any] = {
        "success": True,
        "message": f"Skill '{name}' created.",
        "path": rel_path,
        "skill_md": str(skill_md),
        "scope": scope,
    }
    if category:
        result["category"] = category
    result["hint"] = (
        "To add reference files, templates, or scripts, use "
        f"skill_manage(action='write_file', name='{name}', "
        "file_path='references/example.md', file_content='...')"
    )
    return result


def _edit_skill(
    name: str, content: str, user_id: str, skills_base: Path,
) -> dict[str, Any]:
    """Replace the SKILL.md of an existing skill (full rewrite)."""
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name, skills_base)
    if not existing:
        return {
            "success": False,
            "error": f"Skill '{name}' not found. Create it first with action='create'.",
        }

    skill_md = existing["path"] / "SKILL.md"
    _atomic_write_text(skill_md, content)

    get_manager().invalidate(user_id)

    return {
        "success": True,
        "message": f"Skill '{name}' updated.",
        "skill_md": str(skill_md),
    }


def _patch_skill(
    name: str,
    user_id: str,
    skills_base: Path,
    file_path: str | None = None,
    old_text: str = "",
    new_text: str = "",
) -> dict[str, Any]:
    """Targeted find-and-replace within a skill file."""
    if not old_text:
        return {"success": False, "error": "old_text is required for patch."}

    existing = _find_skill(name, skills_base)
    if not existing:
        return {
            "success": False,
            "error": f"Skill '{name}' not found. Create it first with action='create'.",
        }

    # Determine target file
    if file_path:
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target = existing["path"] / file_path
        # Security check
        try:
            target.resolve().relative_to(existing["path"].resolve())
        except ValueError:
            return {
                "success": False,
                "error": "Path escapes the skill directory.",
            }
        if not target.exists():
            return {
                "success": False,
                "error": f"File '{file_path}' does not exist in skill '{name}'.",
            }
    else:
        target = existing["path"] / "SKILL.md"

    try:
        current = target.read_text(encoding="utf-8")
    except Exception as e:
        return {"success": False, "error": f"Failed to read file: {e}"}

    # Size check for non-SKILL.md files
    if file_path:
        err = _validate_content_size(new_text, label=file_path)
        if err:
            return {"success": False, "error": err}

    if old_text not in current:
        return {
            "success": False,
            "error": "old_text not found in the target file. "
            "Check the exact text (whitespace, punctuation).",
        }

    count = current.count(old_text)
    if count > 1:
        return {
            "success": False,
            "error": f"old_text appears {count} times in the file. "
            "Use a longer, more specific string to target a single occurrence.",
        }

    updated = current.replace(old_text, new_text, 1)

    if not file_path:
        err = _validate_frontmatter(updated)
        if err:
            return {"success": False, "error": err}

    _atomic_write_text(target, updated)

    get_manager().invalidate(user_id)

    target_label = str(target.relative_to(existing["path"]))
    return {
        "success": True,
        "message": f"Patched '{target_label}' in skill '{name}'.",
    }


async def _delete_skill(
    name: str,
    user_id: str,
    session_id: str,
    scope: str,
    skills_base: Path,
) -> dict[str, Any]:
    """Remove a user skill entirely."""
    existing = _find_skill(name, skills_base)
    if not existing:
        return {
            "success": False,
            "error": f"Skill '{name}' not found.",
        }

    skill_dir = existing["path"]
    try:
        from tools.mcp_auto import remove_skill_mcp
        await remove_skill_mcp(
            str(skill_dir), user_id,
            session_id if scope == "session" else "default",
        )
    except Exception:
        logger.exception("MCP cleanup failed for skill '%s'", name)
    shutil.rmtree(skill_dir, ignore_errors=True)

    get_manager().invalidate(user_id)

    # Clean up empty category directory
    parent = skill_dir.parent
    try:
        if parent != skills_base and parent.is_dir():
            if not any(parent.iterdir()):
                parent.rmdir()
    except OSError:
        pass

    return {
        "success": True,
        "message": f"Skill '{name}' deleted.",
    }


def _write_file(
    name: str,
    file_path: str,
    file_content: str,
    user_id: str,
    skills_base: Path,
) -> dict[str, Any]:
    """Add or overwrite a supporting file in a skill."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name, skills_base)
    if not existing:
        return {
            "success": False,
            "error": f"Skill '{name}' not found. Create it first with action='create'.",
        }

    if not file_content and file_content != "":
        return {"success": False, "error": "file_content is required."}

    # Check file size for binary content
    content_bytes = file_content.encode("utf-8")
    if len(content_bytes) > MAX_SKILL_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {len(content_bytes):,} bytes "
                f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB)."
            ),
        }

    target = existing["path"] / file_path
    # Security check
    try:
        target.resolve().relative_to(existing["path"].resolve())
    except ValueError:
        return {
            "success": False,
            "error": "Path escapes the skill directory.",
        }

    _atomic_write_text(target, file_content)

    get_manager().invalidate(user_id)

    rel = str(target.relative_to(existing["path"]))
    return {
        "success": True,
        "message": f"File '{rel}' written in skill '{name}'.",
        "path": rel,
    }


def _remove_file(
    name: str,
    file_path: str,
    user_id: str,
    skills_base: Path,
) -> dict[str, Any]:
    """Remove a supporting file from a skill."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name, skills_base)
    if not existing:
        return {
            "success": False,
            "error": f"Skill '{name}' not found.",
        }

    target = existing["path"] / file_path
    # Security check
    try:
        target.resolve().relative_to(existing["path"].resolve())
    except ValueError:
        return {
            "success": False,
            "error": "Path escapes the skill directory.",
        }

    if not target.exists():
        return {
            "success": False,
            "error": f"File '{file_path}' does not exist in skill '{name}'.",
        }

    target.unlink()

    get_manager().invalidate(user_id)

    # Clean up empty subdirectory
    parent = target.parent
    try:
        if parent != existing["path"] and parent.is_dir():
            if not any(parent.iterdir()):
                parent.rmdir()
    except OSError:
        pass

    return {
        "success": True,
        "message": f"File '{file_path}' removed from skill '{name}'.",
    }


# ── Main tool handler ─────────────────────────────────────────────────────

async def skill_manage(
    action: str,
    name: str,
    user_id: str = "default",
    content: str = "",
    category: str | None = None,
    file_path: str | None = None,
    file_content: str = "",
    old_text: str = "",
    new_text: str = "",
    session_id: str = "default",
    scope: str = "session",
) -> str:
    """Create, edit, or delete skills in one explicit private scope.

    Args:
        action: One of 'create', 'edit', 'patch', 'delete', 'write_file', 'remove_file'.
        name: The skill name.
        content: Full SKILL.md content (required for create, edit).
        category: Optional category name (for create; creates category/name/).
        file_path: Relative path within skill dir (for patch, write_file, remove_file).
        file_content: Content for write_file.
        old_text: Substring to replace (for patch).
        new_text: Replacement text (for patch).
        scope: ``session`` (default) or ``user`` for cross-session reuse.

    Returns:
        JSON result with success status.
    """
    if action not in ("create", "edit", "patch", "delete", "write_file", "remove_file"):
        return json.dumps({
            "success": False,
            "error": f"Unknown action '{action}'. Use: create, edit, patch, delete, write_file, remove_file.",
        })

    if not name or not name.strip():
        return json.dumps({"success": False, "error": "name is required."})

    if scope not in ("session", "user"):
        return json.dumps({
            "success": False,
            "error": "scope must be either 'session' or 'user'.",
        })
    if scope == "session" and (not session_id or session_id == "default"):
        return json.dumps({
            "success": False,
            "error": "A real session_id is required for session-scoped skills.",
        })

    name = name.strip()
    skills_base = _skills_dir_for(user_id, session_id, scope)

    try:
        if action == "create":
            result = await _create_skill(
                name, content, user_id, session_id, scope, skills_base, category
            )
        elif action == "edit":
            result = _edit_skill(name, content, user_id, skills_base)
        elif action == "patch":
            result = _patch_skill(
                name, user_id, skills_base, file_path, old_text, new_text
            )
        elif action == "delete":
            result = await _delete_skill(
                name, user_id, session_id, scope, skills_base
            )
        elif action == "write_file":
            result = _write_file(
                name, file_path or "", file_content, user_id, skills_base
            )
        elif action == "remove_file":
            result = _remove_file(
                name, file_path or "", user_id, skills_base
            )
        else:
            result = {"success": False, "error": f"Unknown action: {action}"}
    except Exception as e:
        logger.exception("skill_manage error for action=%s name=%s", action, name)
        result = {"success": False, "error": f"Unexpected error: {e}"}

    return json.dumps(result, ensure_ascii=False)


# ── JSON Schema ────────────────────────────────────────────────────────────

SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    "description": (
        "Create, edit, or delete skills in your private skills library. "
        "Changes default to the current session; choose user scope only when "
        "the procedure should be reusable across sessions. "
        "Skills capture reusable procedural knowledge — when you discover "
        "a successful approach to a task, save it as a skill.\n\n"
        "ACTIONS:\n"
        "- create: New skill with full SKILL.md content (YAML frontmatter required)\n"
        "- edit: Replace a skill's SKILL.md content entirely\n"
        "- patch: Find-and-replace a specific string within a skill file\n"
        "- delete: Remove a skill entirely\n"
        "- write_file: Add/overwrite a supporting file (references/, templates/, scripts/, assets/)\n"
        "- remove_file: Delete a supporting file\n\n"
        "SKILL.md FORMAT:\n"
        "---\n"
        "name: my-skill\n"
        "description: Brief description of what this skill does\n"
        "---\n\n"
        "# Skill Title\n\n"
        "Full instructions here..."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "edit", "patch", "delete", "write_file", "remove_file"],
                "description": "The action to perform.",
            },
            "name": {
                "type": "string",
                "description": "Skill name (lowercase letters, numbers, hyphens, dots, underscores).",
            },
            "content": {
                "type": "string",
                "description": "Full SKILL.md content with YAML frontmatter. Required for 'create' and 'edit'.",
            },
            "category": {
                "type": "string",
                "description": "Optional category for organization (used with 'create').",
            },
            "file_path": {
                "type": "string",
                "description": "Relative path within skill dir, e.g. 'references/api.md'. Used with 'patch', 'write_file', 'remove_file'.",
            },
            "file_content": {
                "type": "string",
                "description": "Content for the supporting file. Used with 'write_file'.",
            },
            "old_text": {
                "type": "string",
                "description": "Exact substring to find and replace. Used with 'patch'.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text. Used with 'patch'.",
            },
            "scope": {
                "type": "string",
                "enum": ["session", "user"],
                "default": "session",
                "description": "Storage scope. Defaults to the current session.",
            },
        },
        "required": ["action", "name"],
    },
}
