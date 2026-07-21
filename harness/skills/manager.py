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

import hashlib
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from skills.scanner import (
    find_all_skills, resolve_skill_path, get_skills_dirs,
    USER_SKILLS_BASE,
)
from skills.loader import load_skill_content
from skills.path_safety import (
    iter_safe_regular_files,
    validate_skill_resource,
    validate_skill_root,
)

logger = logging.getLogger(__name__)

# Maximum total chars for the skills system-prompt block.
MAX_SKILLS_PROMPT_CHARS = 8000

# Per-skill char cap when building the prompt block.
MAX_SINGLE_SKILL_CHARS = 4000

# Resource manifests are discovery indexes, not execution closures.  Page the
# canonical inventory so a standards-compliant asset-heavy Skill remains
# inspectable without serializing every path into one model/tool result.
DEFAULT_SKILL_MANIFEST_PAGE_ENTRIES = 128
MAX_SKILL_MANIFEST_PAGE_ENTRIES = 512
MAX_SKILL_MANIFEST_OFFSET_ENTRIES = 1_000_000


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
            "Skills provide specialized instructions, knowledge, and resources. "
            "Session-scope skills were uploaded or installed for this conversation and should be "
            "treated as task-specific instructions and resources when relevant. Load a selected "
            "Skill with `skill_view(name)`, and inspect only relevant linked files with "
            "`skill_view(name, file_path=...)`. Use `skills_list()` only when catalog browsing "
            "is explicitly needed."
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
        offset: int | None = None,
        limit: int | None = None,
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
        skill_md = resolve_skill_path(
            name,
            user_id,
            session_id,
            include_optional=include_optional,
            enabled_user_skills=enabled_user_skills,
        )
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
                return self._load_resource_manifest(
                    skill_md,
                    skill_dir,
                    name,
                    session_id,
                    offset=0 if offset is None else offset,
                    limit=(
                        DEFAULT_SKILL_MANIFEST_PAGE_ENTRIES
                        if limit is None else limit
                    ),
                )
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
                    result["path"] = str(skill_md.relative_to(d.resolve(strict=True)))
                    break
                except (FileNotFoundError, OSError, RuntimeError, ValueError):
                    continue
        except Exception:
            result["path"] = skill_md.name

        result["success"] = True
        result["skill_dir"] = str(skill_dir)
        if result.get("linked_files"):
            result["usage_hint"] = (
                "Use skill_view(name, file_path='__manifest__') for a compact index of "
                "the Skill's bundled resources, then read only the references, assets, "
                "templates, scripts, or compiled workflow files relevant to this request. "
                "Bundled files are supporting resources; their presence alone does not "
                "require delegation, multiple output files, or a merge step."
            )

        return result

    def _load_resource_manifest(
        self,
        skill_md: Path,
        skill_dir: Path,
        skill_name: str,
        session_id: str,
        *,
        offset: int = 0,
        limit: int = DEFAULT_SKILL_MANIFEST_PAGE_ENTRIES,
    ) -> dict[str, Any]:
        result = load_skill_content(
            skill_md,
            skill_dir=str(skill_dir),
            session_id=session_id,
        )
        if "error" in result:
            return {"success": False, **result}
        workflow_contract = result.get("workflow_contract") or {}
        execution_contract = result.get("execution_contract") or {}
        compiled_output_contract = (
            execution_contract.get("output_contract")
            if isinstance(execution_contract.get("output_contract"), dict)
            else {}
        )
        output_contract = (
            compiled_output_contract or workflow_contract.get("output_contract") or {}
        )
        linked_files = result.get("linked_files") or {}
        entries = sorted(
            (
                str(category),
                str(path),
            )
            for category, paths in linked_files.items()
            if isinstance(category, str) and isinstance(paths, list)
            for path in paths
            if isinstance(path, str)
        )
        total_entries = len(entries)
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or offset > MAX_SKILL_MANIFEST_OFFSET_ENTRIES
        ):
            return {
                "success": False,
                "reason": "invalid_manifest_offset",
                "error": (
                    "Skill manifest offset must be an integer from 0 through "
                    f"{MAX_SKILL_MANIFEST_OFFSET_ENTRIES}."
                ),
            }
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > MAX_SKILL_MANIFEST_PAGE_ENTRIES
        ):
            return {
                "success": False,
                "reason": "invalid_manifest_limit",
                "error": (
                    "Skill manifest limit must be an integer from 1 through "
                    f"{MAX_SKILL_MANIFEST_PAGE_ENTRIES}."
                ),
            }
        if offset > total_entries:
            return {
                "success": False,
                "reason": "manifest_offset_out_of_range",
                "error": (
                    f"Skill manifest offset {offset} exceeds its "
                    f"{total_entries}-resource inventory."
                ),
            }

        page_entries = entries[offset:offset + limit]
        page_linked_files: dict[str, list[str]] = {}
        for category, path in page_entries:
            page_linked_files.setdefault(category, []).append(path)
        next_offset = offset + len(page_entries)
        has_more = next_offset < total_entries
        manifest_sha256 = hashlib.sha256(json.dumps(
            entries,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

        next_steps, manifest_hint = _build_manifest_guidance(
            linked_files=linked_files,
            resource_graph=result.get("resource_graph") or {},
            workflow_contract=workflow_contract,
            execution_contract=execution_contract,
            output_contract=output_contract,
        )
        execution_plan_hint = _build_execution_plan_hint(output_contract)
        payload = {
            "success": True,
            "name": skill_name,
            "file": "__manifest__",
            "skill_dir": str(skill_dir),
            # Bind every manifest page to the exact canonical instruction
            # document that produced its compiled inventory/contract view.
            "skill_md_sha256": result.get("skill_md_sha256"),
            "skill_md_chars": result.get("skill_md_chars"),
            "linked_files": page_linked_files,
            "linked_file_count": total_entries,
            "manifest_sha256": manifest_sha256,
            "manifest_pagination": {
                "offset": offset,
                "limit": limit,
                "returned_entries": len(page_entries),
                "total_entries": total_entries,
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
            },
            "resource_graph": result.get("resource_graph") or {},
            "workflow_contract": workflow_contract,
            "execution_contract": execution_contract,
            "next_steps": next_steps,
            "hint": manifest_hint,
        }
        if has_more:
            payload["hint"] = (
                str(payload.get("hint") or "")
                + " Continue the canonical resource manifest with "
                f"skill_view(name={skill_name!r}, file_path='__manifest__', "
                f"offset={next_offset}, limit={limit}); use only that exact "
                "next_offset and verify manifest_sha256 remains unchanged."
            ).strip()
        if output_contract:
            payload["output_contract"] = output_contract
        if execution_plan_hint:
            payload["execution_plan_hint"] = execution_plan_hint
        return payload

    def _load_linked_file(
        self,
        skill_dir: Path,
        file_path: str,
        skill_name: str,
    ) -> dict[str, Any]:
        """Load a linked file within a skill directory."""
        root_check = validate_skill_root(skill_dir)
        if not root_check.valid or root_check.path is None:
            return {
                "success": False,
                "error": root_check.message or "Skill package root failed validation.",
            }
        skill_dir = root_check.path

        file_check = validate_skill_resource(
            skill_dir,
            file_path,
            expected_kind="file",
            require_relative=True,
        )
        directory_check = validate_skill_resource(
            skill_dir,
            file_path,
            expected_kind="directory",
            require_relative=True,
        )
        if not file_check.valid and not directory_check.valid:
            code = file_check.code or directory_check.code
            if code == "missing_resource":
                return {
                    "success": False,
                    "error": f"File '{file_path}' not found in skill '{skill_name}'.",
                    "available_files": self._available_resource_files(skill_dir),
                    "hint": "Use one of the available file paths listed above or call skill_view(name, file_path='__manifest__')",
                }
            return {
                "success": False,
                "error": file_check.message or directory_check.message or "Unsafe Skill resource path.",
                "reason": code,
                "hint": "Use a relative path within the skill directory",
            }

        if directory_check.valid and directory_check.path is not None:
            files = [
                str(path.relative_to(skill_dir))
                for path in iter_safe_regular_files(
                    skill_dir,
                    directory_check.path,
                    excluded_dirs={"__pycache__", "node_modules", ".git"},
                )
                if not any(
                    part.startswith(".") or part in {"__pycache__", "node_modules"}
                    for part in path.relative_to(skill_dir).parts
                )
            ]
            return {
                "success": True,
                "name": skill_name,
                "file": file_path.rstrip("/"),
                "is_directory": True,
                "files": files[:200],
                "file_count": len(files),
                "truncated": len(files) > 200,
                "hint": (
                    "This resource is a directory. Inspect the relevant listed files "
                    "with skill_view(name, file_path=...) before relying on their contents."
                ),
            }

        target = file_check.path
        assert target is not None
        size_bytes = target.stat().st_size
        sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {
                "success": True,
                "name": skill_name,
                "file": file_path,
                "content": f"[Binary file: {target.name}, size: {size_bytes} bytes]",
                "is_binary": True,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "media_type": media_type,
                "hint": (
                    "Use skill_copy_resource with this Skill name and source path "
                    "to preserve the exact bytes in the session workspace."
                ),
            }

        return {
            "success": True,
            "name": skill_name,
            "file": file_path,
            "content": content,
            "file_type": target.suffix,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "media_type": media_type,
        }

    def _available_resource_files(self, skill_dir: Path) -> dict[str, list[str]]:
        available: dict[str, list[str]] = {}
        try:
            candidates = sorted(skill_dir.iterdir())
        except OSError:
            candidates = []
        for subdir in candidates:
            directory_check = validate_skill_resource(
                skill_dir, subdir, expected_kind="directory"
            )
            if not directory_check.valid or directory_check.path is None:
                continue
            subdir = directory_check.path
            if subdir.name.startswith(".") or subdir.name in {"__pycache__", "node_modules"}:
                continue
            files = [
                str(f.relative_to(skill_dir))
                for f in iter_safe_regular_files(
                    skill_dir,
                    subdir,
                    excluded_dirs={"__pycache__", "node_modules", ".git"},
                )
            ]
            if files:
                available[subdir.name] = sorted(files)[:100]
        root_files = [
            str(check.path.relative_to(skill_dir))
            for f in candidates
            if f.name != "SKILL.md"
            for check in [validate_skill_resource(skill_dir, f, expected_kind="file")]
            if check.valid and check.path is not None
        ]
        if root_files:
            available["root_files"] = sorted(root_files)
        return available


def _as_manifest_list(value: Any) -> list[Any]:
    """Return a bounded list view without treating strings as iterables."""
    if isinstance(value, list):
        return value[:200]
    if isinstance(value, tuple):
        return list(value[:200])
    return []


def _build_manifest_guidance(
    *,
    linked_files: dict[str, Any],
    resource_graph: dict[str, Any],
    workflow_contract: dict[str, Any],
    execution_contract: dict[str, Any],
    output_contract: dict[str, Any],
) -> tuple[list[str], str]:
    """Build declaration-driven manifest guidance for any Skill shape.

    A resource directory is not execution authority.  In particular, worker-like
    filenames, a broad user task, or the mere presence of several files must not
    manufacture delegation, a modular artifact set, or a merge requirement.
    """
    steps: list[str] = []
    has_resources = bool(linked_files or resource_graph.get("categories"))
    workflow_files = _as_manifest_list(workflow_contract.get("workflow_files"))
    orchestrator_files = _as_manifest_list(
        workflow_contract.get("orchestrator_files")
    )
    script_candidates = _as_manifest_list(
        workflow_contract.get("script_candidates")
    )
    routes = _as_manifest_list(execution_contract.get("routes"))
    workers = _as_manifest_list(execution_contract.get("workers"))
    aggregation = execution_contract.get("aggregation")
    bootstrap = execution_contract.get("knowledge_bootstrap")
    declared_sources = _as_manifest_list(
        workflow_contract.get("declared_external_sources")
    )

    if has_resources:
        steps.append(
            "Inspect only request-relevant bundled resources with "
            "skill_view(name, file_path=...); workspace file tools cannot read "
            "files inside a Skill package."
        )
    if orchestrator_files or workflow_files:
        steps.append(
            "Read the compiled workflow/orchestrator files required by the selected "
            "route or instructions; do not load unrelated package files."
        )
    if routes:
        steps.append(
            "Match the request to exactly one declared execution_contract route "
            "using its compiled selection policy and load only that route's resources."
        )
    if workers:
        steps.append(
            "Execute only workers required by the selected compiled route/worker "
            "graph, respecting its declared waves and dependencies. Use delegate_task "
            "only when that tool is exposed, and retain persisted result_path values "
            "for declared downstream consumers."
        )
    if isinstance(aggregation, dict) and aggregation:
        steps.append(
            "Run only the compiled aggregation steps whose declared inputs are ready, "
            "respecting their dependency order and checks."
        )
    if bootstrap or declared_sources:
        steps.append(
            "Gather the evidence or prerequisite inputs explicitly declared by the "
            "active contract using only its exposed capabilities."
        )
    if script_candidates:
        steps.append(
            "Run a bundled script only when the active instructions or compiled "
            "contract select that exact entrypoint and the corresponding runner is exposed."
        )

    modular = _as_manifest_list(output_contract.get("declared_modular_files"))
    deliverables = _as_manifest_list(output_contract.get("declared_artifacts"))
    ancillary = _as_manifest_list(output_contract.get("declared_ancillary_files"))
    artifact_patterns = _as_manifest_list(
        workflow_contract.get("artifact_patterns")
    )
    artifact_set_policies = _as_manifest_list(
        output_contract.get("artifact_set_policies")
    )
    artifact_set_policy = output_contract.get("artifact_set_policy")
    declared_file_count = output_contract.get("declared_file_count")
    declares_artifact_set = bool(
        modular
        or deliverables
        or ancillary
        or artifact_patterns
        or artifact_set_policies
        or isinstance(artifact_set_policy, dict)
        or declared_file_count
        or workflow_contract.get("requires_modular_artifacts") is True
    )
    if declares_artifact_set:
        steps.append(
            "Produce exactly the declared artifact set in the session workspace; "
            "do not add, omit, or rename members unless the contract permits it."
        )

    merge_declared = bool(
        workflow_contract.get("requires_merge") is True
        or workflow_contract.get("merge_requirements")
        or output_contract.get("merge_mandatory") is True
        or output_contract.get("merge_command")
        or output_contract.get("merge_input_order")
        or output_contract.get("merge_declarations")
    )
    if merge_declared:
        steps.append(
            "Perform the declared workspace-scoped merge/concatenation with "
            "merge_files using the exact compiled input order, then verify the declared "
            "output. If no exact order was compiled, do not invent one."
        )

    checks = (
        _as_manifest_list(output_contract.get("post_merge_checks"))
        + _as_manifest_list(workflow_contract.get("sanity_checks"))
    )
    if checks:
        steps.append(
            "Run the declared validation and completion checks against the resulting artifacts."
        )

    categories = resource_graph.get("categories")
    if isinstance(categories, dict) and any(
        str(name).casefold() in {"assets", "templates"}
        for name in categories
    ):
        steps.append(
            "When the instructions require an exact template or binary asset, use "
            "skill_copy_resource to preserve its bytes; do not reconstruct it from a preview."
        )

    has_compiled_execution = bool(routes or workers or aggregation)
    if not has_compiled_execution and not declares_artifact_set and not merge_declared:
        hint = (
            "No compiled orchestration or multi-artifact/merge contract is declared. "
            "Follow SKILL.md and the user's requested output shape, using bundled files "
            "only as relevant supporting resources. Do not infer delegation, multiple "
            "artifacts, or a merge step from package size or filenames."
        )
    else:
        enabled_parts: list[str] = []
        if routes:
            enabled_parts.append("route selection")
        if workers:
            enabled_parts.append("worker execution")
        if aggregation:
            enabled_parts.append("aggregation")
        if declares_artifact_set:
            enabled_parts.append("artifact-set production")
        if merge_declared:
            enabled_parts.append("merge")
        if checks:
            enabled_parts.append("validation")
        hint = (
            "This manifest exposes only execution shapes declared by the compiled Skill "
            f"contract ({', '.join(enabled_parts)}). Execute those parts when selected; "
            "do not invent undeclared routes, workers, artifacts, or merge operations."
        )
    return steps, hint


def _build_execution_plan_hint(output_contract: dict[str, Any]) -> str:
    """Turn the skill's declared output contract into a concrete execution instruction.

    The instruction mirrors the skill's OWN declarations (file count, merge command,
    final artifact, post-merge checks). The harness does not invent a path here — it
    only restates what the skill declared so the model executes it faithfully.
    """
    if not output_contract:
        return ""
    parts: list[str] = []
    file_count = output_contract.get("declared_file_count")
    modular = _as_manifest_list(output_contract.get("declared_modular_files"))
    deliverables = _as_manifest_list(output_contract.get("declared_artifacts"))
    ancillary = _as_manifest_list(output_contract.get("declared_ancillary_files"))
    if modular:
        parts.append(
            f"Produce the {len(modular)} declared modular artifacts "
            f"({', '.join(modular[:4])}{', …' if len(modular) > 4 else ''}) into the session workspace."
        )
    elif deliverables or ancillary:
        declared = [str(item) for item in (deliverables + ancillary)]
        parts.append(
            f"Produce the {len(declared)} declared artifacts "
            f"({', '.join(declared[:4])}{', …' if len(declared) > 4 else ''}) in the session workspace."
        )
    elif file_count:
        parts.append(f"Produce the declared artifact set ({file_count} total artifacts expected).")
    merge_command = output_contract.get("merge_command")
    merge_input_order = _as_manifest_list(output_contract.get("merge_input_order"))
    merge_declared = bool(
        merge_command
        or merge_input_order
        or output_contract.get("merge_mandatory") is True
        or output_contract.get("merge_declarations")
    )
    final_artifact = output_contract.get("declared_final_artifact")
    if merge_declared:
        mandatory = " (mandatory)" if output_contract.get("merge_mandatory") else ""
        order_hint = ""
        if merge_input_order:
            rendered_order = ", ".join(str(item) for item in merge_input_order[:8])
            if len(merge_input_order) > 8:
                rendered_order += ", …"
            order_hint = f" Exact declared input order: {rendered_order}."
        else:
            order_hint = " Use only the exact input order compiled from the declaration; do not infer one."
        parts.append(
            f"Then reproduce the skill's declared merge order{mandatory} with the workspace-scoped "
            f"`merge_files` tool (do NOT rewrite the large final artifact with write_file/execute_code). "
            f"{order_hint.strip()}"
        )
        if merge_command:
            parts.append(
                f"The package's original command is reference-only: `{merge_command}`."
            )
    elif final_artifact:
        parts.append(f"Then produce the declared final artifact `{final_artifact}`.")
    checks = output_contract.get("post_merge_checks") or []
    if checks:
        parts.append("Verify the resulting artifacts against the Skill's declared checks: " + "; ".join(checks[:5]) + ".")
    if final_artifact:
        completion = f"Completion requires `{final_artifact}` to exist in the workspace"
        if checks:
            completion += " and pass those checks"
        parts.append(completion + ".")
    return " ".join(parts)


# Module-level singleton
_manager: SkillsManager | None = None


def get_manager() -> SkillsManager:
    """Get or create the global SkillsManager singleton."""
    global _manager
    if _manager is None:
        _manager = SkillsManager()
    return _manager
