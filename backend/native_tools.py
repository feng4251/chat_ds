"""Canonical native-tool capability lists shared by backend entry points.

Keep model-facing native tools in this module so chat, workspace settings,
and scheduled runs cannot silently drift onto different capability surfaces.
Session-local MCP tools are discovered by the harness and do not belong here.
"""

from __future__ import annotations


DEFAULT_NATIVE_TOOLS: tuple[str, ...] = (
    "web_search",
    "web_extract",
    "market_quote",
    "execute_code",
    "run_skill_python",
    "run_skill_script",
    "run_declared_command",
    "submit_skill_capability_plan",
    "submit_knowledge_gate_decisions",
    "skill_http_get",
    "skill_http_post_json",
    "read_file",
    "write_file",
    "patch_file",
    "merge_files",
    "search_files",
    "todo",
    "clarify",
    "memory",
    "skills_list",
    "skill_view",
    "skill_copy_resource",
    "skill_manage",
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_back",
    "session_search",
    "sessions_list",
    "sessions_history",
    "sessions_send",
    "sessions_fork",
    "session_status",
    "delegate_task",
    "cronjob",
    "get_goal",
    "create_goal",
    "update_goal",
    "image_generate",
    "vision_analyze",
    "mcp_server_list",
    "mcp_server_status",
)

DEFAULT_NATIVE_TOOL_SET = frozenset(DEFAULT_NATIVE_TOOLS)

# ``enabled_tools`` stored on a scheduled job is a ChatDS capability set, not
# a copy of Claude Code's display names.  Older schedule-controller views did
# not publish that distinction in their JSON schema, so models reasonably
# returned the MCP-qualified names they could see (and occasionally listed a
# built-in Claude tool such as Bash).  Keep this compatibility translation at
# the typed controller boundary.  It is deliberately limited to platform-
# owned tool identities; a foreign MCP prefix must never be allowed to mint a
# ChatDS capability merely because its final component happens to match.
LEGACY_CLAUDE_SCHEDULE_TOOL_ALIASES: dict[str, str | None] = {
    "mcp__chatds-web-search__web_search": "web_search",
    "mcp__chatds-market-data__market_quote": "market_quote",
    "mcp__chatds-schedule__schedule_create": "cronjob",
    # Claude's built-ins are supplied by the native ``default`` tool surface
    # on every scheduled Claude Turn.  They are ambient to this particular
    # capability selector, so retaining them in the DB would be misleading.
    "Bash": None,
    "Read": None,
    "Write": None,
    "Edit": None,
    "Glob": None,
    "Grep": None,
    "Agent": None,
}


def canonicalize_scheduled_tools(
    values: list[str] | None,
    *,
    allowed_tools: set[str] | frozenset[str],
) -> tuple[str, ...]:
    """Bind a scheduled capability subset to the current Session authority.

    ``None`` means "use the safe unattended default intersected with the
    current Session".  An explicit empty list remains empty; it must not be
    widened later by truthiness-based defaulting.
    """

    allowed = frozenset(allowed_tools)
    if allowed - DEFAULT_NATIVE_TOOL_SET:
        raise ValueError("schedule_control_allowed_tools_invalid")
    if values is None:
        return tuple(
            name for name in UNATTENDED_DEFAULT_NATIVE_TOOLS
            if name in allowed
        )
    if not isinstance(values, list):
        raise ValueError("schedule_control_tools_invalid")
    canonical: list[str] = []
    observed: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("schedule_control_tools_invalid")
        mapped = LEGACY_CLAUDE_SCHEDULE_TOOL_ALIASES.get(value, value)
        if mapped is None:
            continue
        if mapped not in DEFAULT_NATIVE_TOOL_SET:
            raise ValueError("schedule_control_tools_unknown")
        if mapped not in allowed:
            raise ValueError("schedule_control_tools_unauthorized")
        if mapped not in observed:
            observed.add(mapped)
            canonical.append(mapped)
    return tuple(canonical)

# Unattended runs must not recursively schedule work, stop for interactive
# clarification, or create child agents unless those tools were explicitly
# saved on the scheduled job. This preserves the existing scheduler default.
UNATTENDED_DEFAULT_NATIVE_TOOLS: tuple[str, ...] = tuple(
    name
    for name in DEFAULT_NATIVE_TOOLS
    if name not in {"cronjob", "clarify", "delegate_task"}
)
