"""SkillsManager — prompt caching and lifecycle for the skills system.

Caches the rendered skills system-prompt block per (user_id, include_optional)
key.  Cache is invalidated when skills are created, edited, or deleted via
the skill_manage tool.

Simplified from hermes-agent:
- No plugin skill namespace tracking
- No platform-specific filtering
- No disabled-skills tracking
- No secret/env-var requirement checking
- No telemetry (bump_use / bump_view)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from skills.scanner import (
    find_all_skills, resolve_skill_path, get_skills_dirs,
    USER_SKILLS_BASE,
)
from skills.loader import load_skill_content

logger = logging.getLogger(__name__)

# Maximum total chars for the skills system-prompt block.
MAX_SKILLS_PROMPT_CHARS = 8000

# Per-skill char cap when building the prompt block.
MAX_SINGLE_SKILL_CHARS = 4000


class SkillsManager:
    """Caches and serves skills prompt blocks for agent sessions.

    Usage in agent.py::

        mgr = get_manager()
        skills_block = mgr.get_system_prompt_block(user_id, session_id)
    """

    def __init__(self):
        # Cache: (user_id, include_optional) -> (mtime_ns, prompt_block)
        self._cache: dict[tuple[str, bool], tuple[int, str]] = {}
        # Per-session activation state
        self._session_optional: dict[str, bool] = {}

    # ── System prompt block ──────────────────────────────────────────────

    def get_system_prompt_block(
        self,
        user_id: str = "default",
        session_id: str = "default",
        include_optional: bool = False,
        enabled_user_skills: list[str] | None = None,
    ) -> str:
        """Return the skills section for the system prompt.

        Builds a compact listing of available skills (progressive disclosure
        tier 1 — name + description only).  The model uses skills_list() and
        skill_view() to explore further.

        Result is cached until skill directories change.
        """
        cache_key = (user_id, session_id, include_optional, tuple(enabled_user_skills or []))

        # Check cache validity by comparing directory mtimes
        cached = self._cache.get(cache_key)
        if cached is not None:
            cached_mtime, cached_block = cached
            current_mtime = self._max_dir_mtime(user_id, session_id, include_optional)
            if current_mtime <= cached_mtime:
                return cached_block

        block = self._build_prompt_block(
            user_id, session_id, include_optional, enabled_user_skills
        )
        current_mtime = self._max_dir_mtime(user_id, session_id, include_optional)
        self._cache[cache_key] = (current_mtime, block)
        return block

    def invalidate(self, user_id: str = "default") -> None:
        """Invalidate cache for a specific user (called after skill mutations)."""
        keys_to_drop = [
            k for k in self._cache
            if k[0] == user_id
        ]
        for k in keys_to_drop:
            del self._cache[k]
        logger.debug("Skills cache invalidated for user=%s", user_id)

    def set_session_optional(
        self,
        session_id: str,
        include_optional: bool,
    ) -> None:
        """Enable/disable optional skills for a session."""
        self._session_optional[session_id] = include_optional

    def get_session_optional(self, session_id: str) -> bool:
        """Check if optional skills are enabled for a session."""
        return self._session_optional.get(session_id, False)

    # ── Internal helpers ─────────────────────────────────────────────────

    def _build_prompt_block(
        self,
        user_id: str,
        session_id: str,
        include_optional: bool,
        enabled_user_skills: list[str] | None = None,
    ) -> str:
        """Build the skills system-prompt block for injection into stable layer."""
        skills = find_all_skills(
            user_id, session_id,
            include_optional=include_optional,
            enabled_user_skills=enabled_user_skills,
        )
        if not skills:
            return ""

        lines: list[str] = []
        lines.append("## Available Skills")
        lines.append("")
        lines.append(
            "Skills provide specialized knowledge and workflows. "
            "Session-scope skills were uploaded or installed for this conversation and should be "
            "treated as task-specific workflow resources when relevant. Load relevant skills with "
            "`skill_view(name)` before executing the task, and inspect linked files with "
            "`skill_view(name, file_path=...)` when the skill references workflows, templates, "
            "or domain-specific guidance. Use `skills_list()` to browse all skills."
        )
        lines.append("")

        total_chars = sum(len(l) for l in lines)

        for skill in skills:
            name = skill["name"]
            desc = skill["description"] or ""
            scope = skill.get("scope") or "unknown"

            line = f"- **{name}** [{scope}]: {desc}" if desc else f"- **{name}** [{scope}]"

            if total_chars + len(line) > MAX_SKILLS_PROMPT_CHARS:
                lines.append(f"\n[... {len(skills) - skills.index(skill)} more skills truncated — use skills_list() to see all]")
                break

            lines.append(line)
            total_chars += len(line)

        return "\n".join(lines)

    def _max_dir_mtime(
        self,
        user_id: str,
        session_id: str,
        include_optional: bool,
    ) -> int:
        """Return the maximum mtime_ns across all skills directories.

        Used for cache invalidation: any file added/removed/modified in a
        skills dir changes the mtime of the directory itself.
        """
        max_mtime = 0
        for d in get_skills_dirs(user_id, session_id, include_optional=include_optional):
            try:
                st = d.stat()
                if st.st_mtime_ns > max_mtime:
                    max_mtime = st.st_mtime_ns
            except OSError:
                pass
        return max_mtime

    # ── Skill content loading (used by tools) ────────────────────────────

    def get_skill_metadata(
        self,
        name: str,
        user_id: str = "default",
        session_id: str = "default",
        include_optional: bool = False,
    ) -> dict[str, Any] | None:
        """Get metadata for a single skill by name (without loading full content)."""
        for skill in find_all_skills(user_id, session_id, include_optional=include_optional):
            if skill["name"] == name:
                return skill
        return None

    def load_skill(
        self,
        name: str,
        file_path: str | None = None,
        user_id: str = "default",
        session_id: str = "default",
        include_optional: bool = False,
        enabled_user_skills: list[str] | None = None,
    ) -> dict[str, Any]:
        """Load full skill content by name, optionally reading a linked file.

        Args:
            name: Skill name.
            file_path: Optional path to a linked file within the skill dir.
            user_id: User identifier.
            session_id: Session identifier (for template substitution).
            include_optional: Whether to search optional skills.
            enabled_user_skills: Whitelist of user-level skill names to expose.
                When provided, user-level skills not in this list are hidden.

        Returns:
            Dict with skill content or error information.
        """
        skill_md = resolve_skill_path(name, user_id, session_id, include_optional=include_optional)
        if skill_md is None:
            available = [s["name"] for s in find_all_skills(
                user_id, session_id, include_optional,
                enabled_user_skills=enabled_user_skills,
            )[:20]]
            return {
                "success": False,
                "error": f"Skill '{name}' not found.",
                "available_skills": available,
                "hint": "Use skills_list to see all available skills",
            }

        # Enforce user-level whitelist: if the resolved skill lives under the
        # user directory but is not in the whitelist, hide it.
        if enabled_user_skills is not None:
            user_dir = (USER_SKILLS_BASE / user_id).resolve()
            session_dir = (USER_SKILLS_BASE / user_id / session_id).resolve() if session_id and session_id != "default" else None
            try:
                skill_md.resolve().relative_to(user_dir)
                in_user_dir = True
            except ValueError:
                in_user_dir = False
            in_session_dir = False
            if session_dir is not None:
                try:
                    skill_md.resolve().relative_to(session_dir)
                    in_session_dir = True
                except ValueError:
                    in_session_dir = False
            if in_user_dir and not in_session_dir and name not in enabled_user_skills:
                return {
                    "success": False,
                    "error": f"Skill '{name}' is not enabled in this session.",
                    "hint": "User-level skills must be explicitly enabled per session.",
                }

        skill_dir = skill_md.parent.resolve()

        # If a linked file is requested, serve that instead
        if file_path:
            if file_path == "__manifest__":
                return self._load_resource_manifest(skill_md, skill_dir, name, session_id)
            return self._load_linked_file(skill_dir, file_path, name)

        result = load_skill_content(
            skill_md,
            skill_dir=str(skill_dir),
            session_id=session_id,
        )

        if "error" in result:
            return {"success": False, **result}

        # Add path info
        try:
            for d in get_skills_dirs(user_id, session_id, include_optional):
                try:
                    result["path"] = str(skill_md.relative_to(d))
                    break
                except ValueError:
                    continue
        except Exception:
            result["path"] = skill_md.name

        result["success"] = True
        result["skill_dir"] = str(skill_dir)
        if result.get("linked_files"):
            result["usage_hint"] = (
                "To inspect workflow resources, call skill_view(name, file_path='__manifest__') "
                "for a compact resource graph, then call skill_view(name, file_path=...) "
                "for relevant files such as orchestrators, workers, references, templates, or scripts."
            )

        return result

    def _load_resource_manifest(
        self,
        skill_md: Path,
        skill_dir: Path,
        skill_name: str,
        session_id: str,
    ) -> dict[str, Any]:
        result = load_skill_content(
            skill_md,
            skill_dir=str(skill_dir),
            session_id=session_id,
        )
        if "error" in result:
            return {"success": False, **result}
        return {
            "success": True,
            "name": skill_name,
            "file": "__manifest__",
            "skill_dir": str(skill_dir),
            "linked_files": result.get("linked_files") or {},
            "resource_graph": result.get("resource_graph") or {},
            "next_steps": [
                "For complex deliverables, first open one or more orchestration/workflow/worker paths from resource_graph.suggested_files.",
                "Then open task-relevant reference, format, script, example, or domain files from resource_graph.suggested_files.",
                "Use skill_view(name, file_path=...) for all skill resources; workspace file tools cannot read skill files.",
            ],
            "hint": (
                "For complex deliverables, inspect resource_graph.suggested_files with "
                "skill_view(name, file_path=...) before drafting. When present, start with "
                "orchestration/workflow files, then inspect task-relevant references, templates, "
                "formats, scripts, examples, or domain resources from this manifest."
            ),
        }

    def _load_linked_file(
        self,
        skill_dir: Path,
        file_path: str,
        skill_name: str,
    ) -> dict[str, Any]:
        """Load a linked file within a skill directory."""
        # Prevent path traversal
        if ".." in file_path:
            return {
                "success": False,
                "error": "Path traversal ('..') is not allowed.",
                "hint": "Use a relative path within the skill directory",
            }

        target = skill_dir / file_path

        # Verify resolved path stays within skill_dir
        try:
            target.resolve().relative_to(skill_dir.resolve())
        except ValueError:
            return {
                "success": False,
                "error": "Path traversal detected — resolved path is outside skill directory.",
                "hint": "Use a relative path within the skill directory",
            }

        if not target.exists():
            return {
                "success": False,
                "error": f"File '{file_path}' not found in skill '{skill_name}'.",
                "available_files": self._available_resource_files(skill_dir),
                "hint": "Use one of the available file paths listed above or call skill_view(name, file_path='__manifest__')",
            }

        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {
                "success": True,
                "name": skill_name,
                "file": file_path,
                "content": f"[Binary file: {target.name}, size: {target.stat().st_size} bytes]",
                "is_binary": True,
            }

        return {
            "success": True,
            "name": skill_name,
            "file": file_path,
            "content": content,
            "file_type": target.suffix,
        }

    def _available_resource_files(self, skill_dir: Path) -> dict[str, list[str]]:
        available: dict[str, list[str]] = {}
        for subdir in sorted(p for p in skill_dir.iterdir() if p.is_dir()):
            if subdir.name.startswith(".") or subdir.name in {"__pycache__", "node_modules"}:
                continue
            files = [
                str(f.relative_to(skill_dir))
                for f in subdir.rglob("*")
                if f.is_file()
            ]
            if files:
                available[subdir.name] = sorted(files)[:100]
        root_files = [
            str(f.relative_to(skill_dir))
            for f in skill_dir.iterdir()
            if f.is_file() and f.name != "SKILL.md"
        ]
        if root_files:
            available["root_files"] = sorted(root_files)
        return available


# Module-level singleton
_manager: SkillsManager | None = None


def get_manager() -> SkillsManager:
    """Get or create the global SkillsManager singleton."""
    global _manager
    if _manager is None:
        _manager = SkillsManager()
    return _manager