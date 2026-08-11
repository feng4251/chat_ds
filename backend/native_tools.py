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

# Unattended runs must not recursively schedule work, stop for interactive
# clarification, or create child agents unless those tools were explicitly
# saved on the scheduled job. This preserves the existing scheduler default.
UNATTENDED_DEFAULT_NATIVE_TOOLS: tuple[str, ...] = tuple(
    name
    for name in DEFAULT_NATIVE_TOOLS
    if name not in {"cronjob", "clarify", "delegate_task"}
)
