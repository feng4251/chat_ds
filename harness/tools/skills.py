"""Skills tools — progressive disclosure skill listing and viewing.

Two tools:
  - skills_list: List available skills (name + description only, token-efficient)
  - skill_view: Load full skill content or access linked files

Simplified from hermes-agent/tools/skills_tool.py:
- No plugin/namespace system
- No platform matching
- No disabled-skills filtering
- No secret capture / env var requirements
- No telemetry (bump_use/bump_view)
- No credential file registration
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from skills.manager import get_manager
from skills.scanner import find_all_skills

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

# Pattern to extract default env var values from SKILL.md or script content.
# Matches blocks like:
#   PATHOLOGY_API_URL      Remote service base URL.
#                          Default: http://127.0.0.1:18018
_ENV_DEFAULT_RE = re.compile(
    r"^(\w+)\s+.*?Default:\s*(\S+)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def _extract_env_hints(content: str) -> dict[str, str]:
    """Extract environment variable defaults from SKILL.md or script content."""
    hints: dict[str, str] = {}
    for match in _ENV_DEFAULT_RE.finditer(content):
        var_name = match.group(1)
        default_val = match.group(2)
        # Only capture vars that look like API URLs / config values
        if "URL" in var_name.upper() or "API" in var_name.upper():
            hints[var_name] = default_val
    return hints


async def skills_list(
    category: str | None = None,
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
) -> str:
    """List all available skills (progressive disclosure tier 1 — minimal metadata).

    Returns only name + description to minimize token usage. Use skill_view()
    to load full content, tags, related files, etc.

    Args:
        category: Optional category filter (e.g., "mlops").
        user_id: User identifier for per-user skill isolation.
        session_id: Session identifier.
        enabled_user_skills: Whitelist of user-level skill names to expose.
            When provided, user-level skills not in this list are hidden.

    Returns:
        JSON string with skills list and categories.
    """
    try:
        mgr = get_manager()
        include_optional = mgr.get_session_optional(session_id)
        all_skills = find_all_skills(
            user_id,
            session_id,
            include_optional=include_optional,
            enabled_user_skills=enabled_user_skills,
        )

        if not all_skills:
            return json.dumps(
                {
                    "success": True,
                    "skills": [],
                    "categories": [],
                    "message": "No skills found.",
                },
                ensure_ascii=False,
            )

        # Filter by category if specified
        if category:
            all_skills = [s for s in all_skills if s.get("category") == category]

        # Extract unique categories
        categories = sorted(
            {s.get("category") for s in all_skills if s.get("category")}
        )

        # Strip internal fields from output
        output_skills = [
            {
                "name": s["name"],
                "description": s["description"],
                "category": s.get("category"),
                "scope": s.get("scope"),
            }
            for s in all_skills
        ]

        return json.dumps(
            {
                "success": True,
                "skills": output_skills,
                "categories": categories,
                "count": len(output_skills),
                "hint": "Use skill_view(name) to see full content, tags, and linked files",
            },
            ensure_ascii=False,
        )

    except Exception as e:
        logger.exception("skills_list error")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


async def skill_view(
    name: str,
    file_path: str | None = None,
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
) -> str:
    """View the content of a skill or a specific file within a skill directory.

    Progressive disclosure tier 2-3: loads full SKILL.md content plus
    linked_files index.  Use file_path to access specific linked files.

    MCP dependencies are registered by the harness control plane when a skill
    is installed. The response reports their declared configuration for
    inspection, but the model must not recreate it manually.

    Args:
        name: Name of the skill (e.g., "axolotl" or "category/axolotl").
        file_path: Optional path to a linked file within the skill
            (e.g., "references/api.md", "templates/config.yaml").
        user_id: User identifier for per-user skill isolation.
        session_id: Session identifier.

    Returns:
        JSON string with skill content or error.
    """
    try:
        mgr = get_manager()
        include_optional = mgr.get_session_optional(session_id)

        result = mgr.load_skill(
            name=name,
            file_path=file_path,
            user_id=user_id,
            session_id=session_id,
            include_optional=include_optional,
            enabled_user_skills=enabled_user_skills,
        )

        # ── Auto-detect bundled MCP server scripts ───────────────────────
        # If the skill has an .mcp.json (mcp_config in linked_files) or
        # Python scripts that look like MCP servers, add a hint.
        # MCP servers are auto-registered on skill upload, so the agent
        # should try using mcp_* tools directly before manual configuration.
        if (
            result.get("success") is not False
            and not result.get("mcp_servers")
            and not file_path
        ):
            linked = result.get("linked_files") or {}

            # Check for .mcp.json (openclaw-compatible)
            has_mcp_config = "mcp_config" in linked

            all_files = []
            for category_files in linked.values():
                all_files.extend(category_files)

            mcp_scripts = []
            for f in all_files:
                if f.endswith(".py") and "mcp" in f.lower():
                    mcp_scripts.append(f)

            if has_mcp_config or mcp_scripts:
                skill_dir = result.get("skill_dir", "")

                if has_mcp_config:
                    result["mcp_config_hint"] = (
                        "This skill includes an .mcp.json file (openclaw-compatible "
                        "MCP configuration). MCP servers defined in .mcp.json are "
                        "automatically registered when the skill is uploaded. "
                        "If the mcp_* tools are not yet available, use "
                        "mcp_server_status to inspect the runtime error and "
                        "report it to the user."
                    )

                if mcp_scripts:
                    env_hints = _extract_env_hints(result.get("content", ""))
                    env_example = ""
                    if env_hints:
                        env_example = ", env=" + str(env_hints)
                    result["mcp_script_hint"] = (
                        "This skill bundles Python MCP server scripts. The runtime "
                        "registers them during skill installation. If the expected "
                        "mcp_* tools are unavailable, call mcp_server_status and "
                        "report its concrete error; do not add/remove servers from "
                        "inside the agent turn.\n\nServer scripts:\n"
                        + "\n".join(
                            f"  {skill_dir}/{script}{env_example}"
                            for script in mcp_scripts
                        )
                        + "\n\nCRITICAL RULES:\n"
                        "- The args MUST be the full file path to the Python "
                        "script, NOT '-c', NOT '-m', NOT a module name.\n"
                        "- Do NOT invent different args. Copy the exact args "
                        "from the example above.\n"
                        + (
                            "Set the env variables shown above to the actual "
                            "service URLs before calling any MCP tools. "
                            "If you don't know the correct URL, ask the user."
                            if env_hints else
                            "Check the SKILL.md content above for required "
                            "environment variables (like API_URL). "
                            "If unsure about values, ask the user."
                        )
                    )

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.exception("skill_view error")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ── JSON Schemas for registry ──────────────────────────────────────────────

SKILLS_LIST_SCHEMA = {
    "name": "skills_list",
    "description": (
        "List all available skills with name and description. "
        "Use skill_view(name) to load a skill's full content before using it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter to narrow results.",
            },
            "user_id": {
                "type": "string",
                "description": "User identifier for per-user skill isolation.",
            },
            "session_id": {
                "type": "string",
                "description": "Session identifier.",
            },
        },
        "required": [],
    },
}

SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": (
        "Load a skill's full content including instructions, tags, and linked files. "
        "First call returns SKILL.md content plus a 'linked_files' dict showing "
        "available references/templates/scripts. To access those, call again with "
        "the file_path parameter (e.g., 'references/api.md')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (use skills_list to see available skills).",
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Optional path to a linked file within the skill "
                    "(e.g., 'references/api.md', 'templates/config.yaml'). "
                    "Omit to get the main SKILL.md content."
                ),
            },
            "user_id": {
                "type": "string",
                "description": "User identifier for per-user skill isolation.",
            },
            "session_id": {
                "type": "string",
                "description": "Session identifier.",
            },
        },
        "required": ["name"],
    },
}
