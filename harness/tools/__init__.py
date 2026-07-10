from tools.registry import register
from tools.web_search import web_search
from tools.web_extract import web_extract
from tools.code_execution import execute_code, EXECUTE_CODE_SCHEMA
from tools.skill_python import run_skill_python, RUN_SKILL_PYTHON_SCHEMA
from tools.file_tools import read_file, write_file, patch_file, search_files
from tools.file_tools import (
    READ_FILE_SCHEMA, WRITE_FILE_SCHEMA, PATCH_FILE_SCHEMA, SEARCH_FILES_SCHEMA,
)
from tools.todo import todo, TODO_SCHEMA
from tools.clarify import clarify, CLARIFY_SCHEMA
from tools.memory import memory, MEMORY_SCHEMA
from tools.skills import skills_list, skill_view, SKILLS_LIST_SCHEMA, SKILL_VIEW_SCHEMA
from tools.skill_manage import skill_manage, SKILL_MANAGE_SCHEMA
from tools.browser import (
    browser_navigate, browser_snapshot, browser_click,
    browser_type, browser_scroll, browser_back,
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
        "description": "Search the web using DuckDuckGo. Returns top results with title, snippet, and URL.",
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
    is_read_only=False,
    parallel_safe=False,
    path_scoped=True,
    emoji="🐍",
    allow_in_parallel_child=False,
)

register(
    "read_file",
    READ_FILE_SCHEMA,
    read_file,
    is_read_only=True,
    parallel_safe=True,
    path_scoped=True,
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
    "search_files",
    SEARCH_FILES_SCHEMA,
    search_files,
    is_read_only=True,
    parallel_safe=True,
    path_scoped=True,
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
    emoji="📚",
)

register(
    "skill_view",
    SKILL_VIEW_SCHEMA,
    skill_view,
    is_read_only=True,
    parallel_safe=True,
    path_scoped=True,
    emoji="📖",
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
)

# ── Phase 7: Browser tools ────────────────────────────────────────────────────

register(
    "browser_navigate",
    BROWSER_NAVIGATE_SCHEMA,
    browser_navigate,
    emoji="🌐",
)

register(
    "browser_snapshot",
    BROWSER_SNAPSHOT_SCHEMA,
    browser_snapshot,
    emoji="📸",
)

register(
    "browser_click",
    BROWSER_CLICK_SCHEMA,
    browser_click,
    emoji="🖱️",
)

register(
    "browser_type",
    BROWSER_TYPE_SCHEMA,
    browser_type,
    emoji="⌨️",
)

register(
    "browser_scroll",
    BROWSER_SCROLL_SCHEMA,
    browser_scroll,
    emoji="📜",
)

register(
    "browser_back",
    BROWSER_BACK_SCHEMA,
    browser_back,
    emoji="⬅️",
)

# ── Phase A4: Image generation & vision analysis ──────────────────────────────

register(
    "image_generate",
    IMAGE_GENERATE_SCHEMA,
    image_generate,
    emoji="🎨",
)

register(
    "vision_analyze",
    VISION_ANALYZE_SCHEMA,
    vision_analyze,
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
