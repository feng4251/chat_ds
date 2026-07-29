from tools.registry import register
from tools.web_search import web_search
from tools.web_extract import web_extract
from tools.skill_http import (
    skill_http_get,
    skill_http_post_json,
    RUN_SKILL_HTTP_GET_SCHEMA,
    RUN_SKILL_HTTP_POST_JSON_SCHEMA,
)
from tools.code_execution import execute_code, EXECUTE_CODE_SCHEMA
from tools.skill_python import (
    run_skill_python,
    preflight_run_skill_python_args,
    RUN_SKILL_PYTHON_SCHEMA,
)
from tools.skill_script import run_skill_script, RUN_SKILL_SCRIPT_SCHEMA
from tools.skill_process import (
    run_skill_process,
    preflight_run_skill_process_args,
    RUN_SKILL_PROCESS_SCHEMA,
)
from tools.declared_command import run_declared_command, RUN_DECLARED_COMMAND_SCHEMA
from tools.skill_capability_plan import (
    submit_skill_capability_plan,
    SUBMIT_SKILL_CAPABILITY_PLAN_SCHEMA,
)
from tools.knowledge_gate import (
    submit_knowledge_gate_decisions,
    SUBMIT_KNOWLEDGE_GATE_DECISIONS_SCHEMA,
)
from tools.file_tools import read_file, write_file, patch_file, merge_files, search_files
from tools.file_tools import (
    READ_FILE_SCHEMA, WRITE_FILE_SCHEMA, PATCH_FILE_SCHEMA, MERGE_FILES_SCHEMA,
    SEARCH_FILES_SCHEMA,
)
from tools.todo import todo, TODO_SCHEMA
from tools.clarify import clarify, CLARIFY_SCHEMA
from tools.memory import memory, MEMORY_SCHEMA
from tools.skills import (
    skills_list,
    skill_view,
    skill_copy_resource,
    SKILLS_LIST_SCHEMA,
    SKILL_VIEW_SCHEMA,
    SKILL_COPY_RESOURCE_SCHEMA,
)
from tools.skill_manage import skill_manage, SKILL_MANAGE_SCHEMA
from tools.browser import (
    browser_navigate, browser_snapshot, browser_click,
    browser_type, browser_scroll, browser_back,
    browser_navigate_args_preflight,
    BROWSER_NAVIGATE_SCHEMA, BROWSER_SNAPSHOT_SCHEMA, BROWSER_CLICK_SCHEMA,
    BROWSER_TYPE_SCHEMA, BROWSER_SCROLL_SCHEMA, BROWSER_BACK_SCHEMA,
)
from tools.session_search import session_search, SESSION_SEARCH_SCHEMA
from tools.image_generation import image_generate, IMAGE_GENERATE_SCHEMA
from tools.vision import vision_analyze, VISION_ANALYZE_SCHEMA
from tools.mcp_client import (
    mcp_server_list, mcp_server_status,
    MCP_SERVER_LIST_SCHEMA, MCP_SERVER_STATUS_SCHEMA,
    connect_all_for_user, disconnect_all_for_user,
)
from tools.delegation import delegate_task, DELEGATE_TASK_SCHEMA
from tools.backend_control import (
    sessions_list, sessions_history, sessions_fork, sessions_send, session_status,
    get_goal, create_goal, update_goal, cronjob,
    SESSIONS_LIST_SCHEMA, SESSIONS_HISTORY_SCHEMA, SESSIONS_FORK_SCHEMA,
    SESSIONS_SEND_SCHEMA, SESSION_STATUS_SCHEMA,
    GET_GOAL_SCHEMA, CREATE_GOAL_SCHEMA, UPDATE_GOAL_SCHEMA, CRONJOB_SCHEMA,
)

# ── Existing tools ───────────────────────────────────────────────────────────

register(
    "web_search",
    {
        "name": "web_search",
        "description": "Search the web using SearXNG first, with DuckDuckGo fallback. Returns top results with title, snippet, and URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5).",
                },
            },
            "required": ["query"],
        },
    },
    web_search,
    is_read_only=True,
    parallel_safe=False,
    external_interaction=True,
)

register(
    "web_extract",
    {
        "name": "web_extract",
        "description": "Fetch and extract readable text content from a URL. Use this to read full articles or pages found via web_search.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL of the page to fetch and extract text from.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 2500).",
                },
            },
            "required": ["url"],
        },
    },
    web_extract,
    is_read_only=True,
    parallel_safe=True,
    external_interaction=True,
)

register(
    "skill_http_get",
    RUN_SKILL_HTTP_GET_SCHEMA,
    skill_http_get,
    is_read_only=True,
    parallel_safe=False,
    external_interaction=True,
    emoji="🔐",
)

register(
    "skill_http_post_json",
    RUN_SKILL_HTTP_POST_JSON_SCHEMA,
    skill_http_post_json,
    is_read_only=False,
    is_destructive=True,
    parallel_safe=False,
    allow_in_parallel_child=True,
    mutates_workspace=False,
    external_interaction=True,
    emoji="🔐",
)

# ── Phase 2: New tools ───────────────────────────────────────────────────────

register(
    "execute_code",
    EXECUTE_CODE_SCHEMA,
    execute_code,
    is_read_only=False,
    parallel_safe=False,
    path_scoped=True,
    emoji="🐍",
    allow_in_parallel_child=False,
)

register(
    "run_skill_python",
    RUN_SKILL_PYTHON_SCHEMA,
    run_skill_python,
    args_preflight_fn=preflight_run_skill_python_args,
    is_read_only=False,
    parallel_safe=False,
    path_scoped=True,
    emoji="🐍",
    allow_in_parallel_child=False,
)

register(
    "run_skill_script",
    RUN_SKILL_SCRIPT_SCHEMA,
    run_skill_script,
    is_read_only=False,
    parallel_safe=False,
    path_scoped=True,
    emoji="⚙️",
    allow_in_parallel_child=False,
)

register(
    "run_skill_process",
    RUN_SKILL_PROCESS_SCHEMA,
    run_skill_process,
    args_preflight_fn=preflight_run_skill_process_args,
    is_read_only=False,
    is_destructive=True,
    parallel_safe=False,
    path_scoped=True,
    allow_in_child=True,
    allow_in_parallel_child=False,
    mutates_workspace=True,
    mutates_global_state=False,
    salvage_safe=False,
    external_interaction=True,
    emoji="🧰",
)

register(
    "run_declared_command",
    RUN_DECLARED_COMMAND_SCHEMA,
    run_declared_command,
    is_read_only=False,
    parallel_safe=False,
    path_scoped=True,
    emoji="⌨️",
    allow_in_parallel_child=False,
)

register(
    "submit_skill_capability_plan",
    SUBMIT_SKILL_CAPABILITY_PLAN_SCHEMA,
    submit_skill_capability_plan,
    is_read_only=True,
    parallel_safe=False,
    allow_in_child=False,
    emoji="🧭",
)

register(
    "submit_knowledge_gate_decisions",
    SUBMIT_KNOWLEDGE_GATE_DECISIONS_SCHEMA,
    submit_knowledge_gate_decisions,
    is_read_only=True,
    parallel_safe=False,
    allow_in_child=True,
    emoji="🚦",
)

register(
    "read_file",
    READ_FILE_SCHEMA,
    read_file,
    is_read_only=True,
    parallel_safe=True,
    path_scoped=True,
    salvage_safe=True,
    emoji="📖",
)

register(
    "write_file",
    WRITE_FILE_SCHEMA,
    write_file,
    is_read_only=False,
    is_destructive=True,
    parallel_safe=False,
    path_scoped=True,
    emoji="📝",
    allow_in_parallel_child=False,
)

register(
    "patch_file",
    PATCH_FILE_SCHEMA,
    patch_file,
    is_read_only=False,
    is_destructive=True,
    parallel_safe=False,
    path_scoped=True,
    emoji="🩹",
    allow_in_parallel_child=False,
)

register(
    "merge_files",
    MERGE_FILES_SCHEMA,
    merge_files,
    is_read_only=False,
    is_destructive=True,
    parallel_safe=False,
    path_scoped=True,
    emoji="🧩",
    allow_in_parallel_child=False,
)

register(
    "search_files",
    SEARCH_FILES_SCHEMA,
    search_files,
    is_read_only=True,
    parallel_safe=True,
    path_scoped=True,
    salvage_safe=True,
    emoji="🔍",
)

register(
    "todo",
    TODO_SCHEMA,
    todo,
    emoji="✅",
)

register(
    "clarify",
    CLARIFY_SCHEMA,
    clarify,
    emoji="❓",
    allow_in_child=False,
    requires_user_visibility=True,
)

register(
    "memory",
    MEMORY_SCHEMA,
    memory,
    emoji="🧠",
    allow_in_child=False,
    mutates_global_state=True,
)

# ── Phase 4: Skills tools ─────────────────────────────────────────────────────

register(
    "skills_list",
    SKILLS_LIST_SCHEMA,
    skills_list,
    is_read_only=True,
    parallel_safe=True,
    salvage_safe=True,
    emoji="📚",
)

register(
    "skill_view",
    SKILL_VIEW_SCHEMA,
    skill_view,
    is_read_only=True,
    parallel_safe=True,
    path_scoped=True,
    salvage_safe=True,
    emoji="📖",
)

register(
    "skill_copy_resource",
    SKILL_COPY_RESOURCE_SCHEMA,
    skill_copy_resource,
    is_read_only=False,
    parallel_safe=False,
    path_scoped=True,
    emoji="📎",
)

register(
    "skill_manage",
    SKILL_MANAGE_SCHEMA,
    skill_manage,
    is_read_only=False,
    is_destructive=True,
    parallel_safe=False,
    path_scoped=True,
    emoji="🛠️",
    allow_in_parallel_child=False,
    mutates_workspace=False,
    mutates_global_state=True,
)

# ── Phase 7: Browser tools ────────────────────────────────────────────────────

register(
    "browser_navigate",
    BROWSER_NAVIGATE_SCHEMA,
    browser_navigate,
    args_preflight_fn=browser_navigate_args_preflight,
    is_read_only=True,
    parallel_safe=False,
    external_interaction=True,
    emoji="🌐",
)

register(
    "browser_snapshot",
    BROWSER_SNAPSHOT_SCHEMA,
    browser_snapshot,
    is_read_only=True,
    parallel_safe=False,
    external_interaction=True,
    emoji="📸",
)

register(
    "browser_click",
    BROWSER_CLICK_SCHEMA,
    browser_click,
    external_interaction=True,
    emoji="🖱️",
)

register(
    "browser_type",
    BROWSER_TYPE_SCHEMA,
    browser_type,
    external_interaction=True,
    emoji="⌨️",
)

register(
    "browser_scroll",
    BROWSER_SCROLL_SCHEMA,
    browser_scroll,
    external_interaction=True,
    emoji="📜",
)

register(
    "browser_back",
    BROWSER_BACK_SCHEMA,
    browser_back,
    external_interaction=True,
    emoji="⬅️",
)

# ── Phase A4: Image generation & vision analysis ──────────────────────────────

register(
    "image_generate",
    IMAGE_GENERATE_SCHEMA,
    image_generate,
    external_interaction=True,
    emoji="🎨",
)

register(
    "vision_analyze",
    VISION_ANALYZE_SCHEMA,
    vision_analyze,
    external_interaction=True,
    emoji="👁️",
)

# ── MCP diagnostics (mutations belong to the backend control plane) ────────────

register(
    "mcp_server_list",
    MCP_SERVER_LIST_SCHEMA,
    mcp_server_list,
    is_read_only=True,
    parallel_safe=True,
    emoji="📋",
)

register(
    "mcp_server_status",
    MCP_SERVER_STATUS_SCHEMA,
    mcp_server_status,
    is_read_only=True,
    parallel_safe=True,
    emoji="📡",
)

# ── Phase 7: Session search ────────────────────────────────────────────────────

register(
    "session_search",
    SESSION_SEARCH_SCHEMA,
    session_search,
    is_read_only=True,
    parallel_safe=True,
    emoji="🔎",
)

register(
    "sessions_list",
    SESSIONS_LIST_SCHEMA,
    sessions_list,
    emoji="🗂️",
)

register(
    "sessions_history",
    SESSIONS_HISTORY_SCHEMA,
    sessions_history,
    emoji="📜",
)

register(
    "sessions_fork",
    SESSIONS_FORK_SCHEMA,
    sessions_fork,
    emoji="🌿",
    allow_in_child=False,
    mutates_global_state=True,
)

register(
    "sessions_send",
    SESSIONS_SEND_SCHEMA,
    sessions_send,
    emoji="📨",
    allow_in_child=False,
    mutates_global_state=True,
)

register(
    "session_status",
    SESSION_STATUS_SCHEMA,
    session_status,
    emoji="📊",
)

register(
    "delegate_task",
    DELEGATE_TASK_SCHEMA,
    delegate_task,
    emoji="🧩",
    allow_in_child=False,
)

register(
    "cronjob",
    CRONJOB_SCHEMA,
    cronjob,
    emoji="⏰",
    allow_in_child=False,
    mutates_global_state=True,
)

register(
    "get_goal",
    GET_GOAL_SCHEMA,
    get_goal,
    emoji="🎯",
)

register(
    "create_goal",
    CREATE_GOAL_SCHEMA,
    create_goal,
    emoji="🎯",
    allow_in_child=False,
    mutates_global_state=True,
)

register(
    "update_goal",
    UPDATE_GOAL_SCHEMA,
    update_goal,
    emoji="🎯",
    allow_in_child=False,
    mutates_global_state=True,
)
