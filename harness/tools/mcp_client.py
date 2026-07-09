"""MCP (Model Context Protocol) client with full transport support.

Provides per-user MCP server configuration management and tool discovery.
Supports three transport types:
  - **stdio**: Spawns MCP server as a subprocess (e.g., Python scripts, npx)
  - **http**: Streamable HTTP transport (default for url-based servers)
  - **sse**: Server-Sent Events transport (legacy MCP servers)

Also provides lifecycle management: circuit breaker, keepalive, dynamic tool
refresh, startup reconnection, and graceful shutdown.

Config storage: data/mcp/<user_id>/servers.json

Management tools:
  - mcp_server_add    — add an MCP server configuration
  - mcp_server_remove — remove a configuration
  - mcp_server_list   — list all configured servers
  - mcp_server_status — check connection + discovered tools for a server

Reference: hermes-agent/tools/mcp_tool.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


logger = logging.getLogger(__name__)

# ── Graceful import — MCP SDK is optional ─────────────────────────────────────

_MCP_AVAILABLE = False
_MCP_STDIO_AVAILABLE = False
_MCP_HTTP_AVAILABLE = False
_MCP_NOTIFICATION_TYPES = False
_MCP_MESSAGE_HANDLER_SUPPORTED = False

# Conservative fallback protocol version (Streamable HTTP introduced 2025-03-26)
LATEST_PROTOCOL_VERSION = "2025-03-26"

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _MCP_AVAILABLE = True
    _MCP_STDIO_AVAILABLE = True
except ImportError:
    pass

if _MCP_AVAILABLE:
    try:
        from mcp.client.streamable_http import streamablehttp_client
        _MCP_HTTP_AVAILABLE = True
    except ImportError:
        pass
    try:
        from mcp.client.streamable_http import streamable_http_client
        _MCP_NEW_HTTP = True
    except ImportError:
        _MCP_NEW_HTTP = False
    try:
        from mcp.client.sse import sse_client
    except ImportError:
        sse_client = None
    try:
        from mcp.types import LATEST_PROTOCOL_VERSION as _SDK_LATEST
        LATEST_PROTOCOL_VERSION = _SDK_LATEST
    except ImportError:
        pass
    try:
        from mcp.types import (
            ServerNotification,
            ToolListChangedNotification,
        )
        _MCP_NOTIFICATION_TYPES = True
    except ImportError:
        pass


def _check_message_handler_support() -> bool:
    """Check if ClientSession accepts ``message_handler`` kwarg."""
    if not _MCP_AVAILABLE:
        return False
    try:
        import inspect
        return "message_handler" in inspect.signature(ClientSession).parameters
    except (TypeError, ValueError):
        return False


_MCP_MESSAGE_HANDLER_SUPPORTED = _check_message_handler_support()

# ── Constants ─────────────────────────────────────────────────────────────────

MCP_CONFIG_BASE = Path("data/mcp")
MCP_CONFIG_FILE = "servers.json"
MCP_STDERR_LOG_DIR = Path("data/mcp/logs")

# Sentinel session_id used when no real session is available.
# Configs stored under this key are user-level (shared across sessions).
DEFAULT_SESSION_ID = "default"

JSONRPC_VERSION = "2.0"

CONNECT_TIMEOUT = 30.0
REQUEST_TIMEOUT = 120.0
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_BASE_DELAY = 2.0
KEEPALIVE_INTERVAL = 180.0  # seconds
KEEPALIVE_TIMEOUT = 30.0

CIRCUIT_BREAKER_THRESHOLD = 3       # consecutive failures
CIRCUIT_BREAKER_COOLDOWN = 60.0     # seconds before retry
CIRCUIT_BREAKER_WINDOW = 120.0      # seconds — reset failure count after this

# Environment variables safe to pass to stdio subprocesses
_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
})

# Regex for credential patterns to strip from error messages
_CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9_]{1,255}"           # GitHub PAT
    r"|sk-[A-Za-z0-9_]{1,255}"           # OpenAI-style key
    r"|Bearer\s+\S+"                      # Bearer token
    r"|token=[^\s&,;\"']{1,255}"         # token=...
    r"|key=[^\s&,;\"']{1,255}"           # key=...
    r"|API_KEY=[^\s&,;\"']{1,255}"       # API_KEY=...
    r"|password=[^\s&,;\"']{1,255}"      # password=...
    r"|secret=[^\s&,;\"']{1,255}"        # secret=...
    r")",
    re.IGNORECASE,
)

# ── Per-(user, session) MCP state ─────────────────────────────────────────────
# Key: (user_id, session_id) — session-scoped isolation.
# session_id="default" is the user-level fallback.

_mcp_states: dict[tuple[str, str], dict[str, "MCPServerState"]] = {}
_mcp_connect_locks: dict[tuple[str, str, str], asyncio.Lock] = {}


class MCPServerState:
    """Runtime state for a single connected MCP server."""

    __slots__ = (
        "name", "config", "tools", "connected", "last_error",
        "last_connect_time", "transport",
        "_session_id", "_reconnect_attempts",
        "_failure_count", "_last_failure_time", "_circuit_open_until",
        "_shutdown_event", "_ready_event", "_keepalive_task",
        "_bg_connect_task",
        "_rpc_lock", "_registered_tool_names",
        "session", "initialize_result",
    )

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.tools: list[dict] = []
        self.connected = False
        self.last_error: str = ""
        self.last_connect_time: float = 0
        self.transport: str = _detect_transport(config)
        self.session: Any = None
        self.initialize_result: Any = None

        # Internal
        self._session_id: str | None = None
        self._reconnect_attempts: int = 0

        # Circuit breaker
        self._failure_count: int = 0
        self._last_failure_time: float = 0
        self._circuit_open_until: float = 0

        # Lifecycle events
        self._shutdown_event = asyncio.Event()
        self._ready_event = asyncio.Event()

        # Keepalive
        self._keepalive_task: asyncio.Task | None = None

        # Background connection task (stdio transport runs until shutdown)
        self._bg_connect_task: asyncio.Task | None = None

        # RPC serialization (prevents JSON-RPC stream corruption on stdio)
        self._rpc_lock = asyncio.Lock()

        # Registered tool names (for dynamic refresh diff)
        self._registered_tool_names: list[str] = []

    def to_status(self) -> dict:
        tool_names = []
        for t in self.tools:
            if hasattr(t, "name"):
                tool_names.append(t.name)
            elif isinstance(t, dict):
                tool_names.append(t.get("name", "unknown"))
            else:
                tool_names.append(str(t))
        return {
            "name": self.name,
            "connected": self.connected,
            "transport": self.transport,
            "tool_count": len(self.tools),
            "tools": tool_names,
            "last_error": self.last_error,
            "last_connect_time": self.last_connect_time,
            "runtime": self.config.get("_runtime", "default"),
            "dependency_status": self.config.get("_runtime_status"),
            "network_egress": self.transport in {"http", "sse"} or bool(self.config.get("_requires_network")),
        }


# ── Transport detection ──────────────────────────────────────────────────────


def _detect_transport(config: dict) -> str:
    """Classify MCP transport from config keys."""
    if config.get("command"):
        return "stdio"
    if config.get("transport") == "sse":
        return "sse"
    return "http"


# ── Security helpers ─────────────────────────────────────────────────────────


def _build_safe_env(user_env: dict | None) -> dict:
    """Build a filtered environment dict for stdio subprocesses.

    Only passes through safe baseline variables (PATH, HOME, etc.) and XDG_*
    variables from the current process environment, plus any variables
    explicitly specified by the user in the server config.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _SAFE_ENV_KEYS or key.startswith("XDG_"):
            env[key] = value
    # Curated compatibility modules for common bundled MCP scripts. Skill-owned
    # Python MCP servers can add their session venv via runtime_env_for_subprocess().
    env["PYTHONPATH"] = "/app/mcp_vendor"
    if user_env:
        env.update(user_env)
    return env


def _sanitize_error(text: str) -> str:
    """Strip credential-like patterns from error text before returning to LLM."""
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", text)


def _exc_str(exc: BaseException) -> str:
    """Return a non-empty human-readable string for *exc*."""
    text = str(exc).strip()
    return text if text else repr(exc)


# ── Stderr redirection for stdio subprocesses ────────────────────────────────


def _get_stderr_log(server_name: str) -> Any:
    """Open a per-server stderr log file for MCP subprocess output.

    Returns a file handle with a real OS-level fd (required by asyncio's
    subprocess machinery). Falls back to os.devnull on failure.
    """
    log_dir = MCP_STDERR_LOG_DIR
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{server_name}.log"
        # Write startup marker
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"\n===== [{ts}] MCP server '{server_name}' started =====\n")
        fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
        fh.fileno()  # sanity-check: real fd
        return fh
    except Exception as exc:
        logger.debug("Failed to open MCP stderr log for '%s': %s", server_name, exc)
        try:
            return open(os.devnull, "w", encoding="utf-8")
        except Exception:
            return None


# ── Circuit breaker ──────────────────────────────────────────────────────────


def _is_circuit_open(state: MCPServerState) -> bool:
    """Check if the circuit breaker is currently open for this server."""
    if state._circuit_open_until == 0:
        return False
    if time.time() < state._circuit_open_until:
        return True
    # Cooldown expired — allow one probe
    state._circuit_open_until = 0
    return False


def _record_failure(state: MCPServerState) -> None:
    """Record a connection failure, opening the circuit breaker if threshold hit."""
    now = time.time()
    if now - state._last_failure_time > CIRCUIT_BREAKER_WINDOW:
        state._failure_count = 1
    else:
        state._failure_count += 1
    state._last_failure_time = now
    if state._failure_count >= CIRCUIT_BREAKER_THRESHOLD:
        state._circuit_open_until = now + CIRCUIT_BREAKER_COOLDOWN
        logger.warning(
            "Circuit breaker OPEN for MCP server '%s' (%d failures, cooldown %ds)",
            state.name, state._failure_count, CIRCUIT_BREAKER_COOLDOWN,
        )


def _record_success(state: MCPServerState) -> None:
    """Reset circuit breaker after a successful connection."""
    state._failure_count = 0
    state._circuit_open_until = 0


# ── Config persistence ───────────────────────────────────────────────────────
#
# MCP configs are session-scoped (mirroring skills):
#   data/mcp/<user_id>/<session_id>/servers.json  — session-scoped (highest)
#   data/mcp/<user_id>/servers.json                — user-level (fallback)
#
# _load_config merges both layers: session overrides user for same-name servers.


def _session_config_path(user_id: str, session_id: str) -> Path:
    """Path to the session-scoped MCP config file."""
    if session_id and session_id != DEFAULT_SESSION_ID:
        return MCP_CONFIG_BASE / user_id / session_id / MCP_CONFIG_FILE
    return MCP_CONFIG_BASE / user_id / MCP_CONFIG_FILE


def _user_config_path(user_id: str) -> Path:
    """Path to the user-level MCP config file (fallback, shared across sessions)."""
    return MCP_CONFIG_BASE / user_id / MCP_CONFIG_FILE


def _load_scope_config(user_id: str, session_id: str = "default") -> dict:
    """Load only the selected persistence scope without inherited fallback."""
    path = _session_config_path(user_id, session_id)
    if not path.exists():
        return {"servers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("servers", {})
        return {"servers": servers if isinstance(servers, dict) else {}}
    except (json.JSONDecodeError, PermissionError, OSError) as exc:
        logger.warning(
            "Failed to load MCP scope config for %s/%s: %s",
            user_id, session_id, exc,
        )
        return {"servers": {}}


def _load_config(user_id: str, session_id: str = "default") -> dict:
    """Load merged MCP server configurations.

    Merges user-level config (low priority) with session-level config
    (high priority). Session-scoped servers with the same name override
    user-scoped ones.
    """
    merged: dict[str, dict] = {}

    # Layer 1: user-level (low priority)
    user_path = _user_config_path(user_id)
    if user_path.exists():
        try:
            user_cfg = json.loads(user_path.read_text(encoding="utf-8"))
            for name, cfg in user_cfg.get("servers", {}).items():
                merged[name] = cfg
        except (json.JSONDecodeError, PermissionError) as e:
            logger.warning("Failed to load user MCP config for %s: %s", user_id, e)

    # Layer 2: session-level (high priority — overrides user)
    session_path = _session_config_path(user_id, session_id)
    if session_id != DEFAULT_SESSION_ID and session_path.exists():
        try:
            session_cfg = json.loads(session_path.read_text(encoding="utf-8"))
            for name, cfg in session_cfg.get("servers", {}).items():
                merged[name] = cfg  # override user-level
        except (json.JSONDecodeError, PermissionError) as e:
            logger.warning(
                "Failed to load session MCP config for %s/%s: %s",
                user_id, session_id, e,
            )

    return {"servers": merged}


def _save_config(user_id: str, config: dict, session_id: str = "default") -> None:
    """Save MCP server configurations to the session-scoped path."""
    path = _session_config_path(user_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def _filter_user_mcp_servers(
    servers: dict,
    user_id: str,
    enabled_user_skills: list[str] | None,
) -> dict:
    """Filter out user-level MCP servers whose source skill is not enabled.

    A user-level MCP server is one that appears in the user-level config
    file (``data/mcp/<uid>/servers.json``). If ``enabled_user_skills`` is
    None, no filtering is applied. If it's an empty list, all user-level
    servers with a ``_source_skill`` are filtered out. Servers without a
    ``_source_skill`` are kept (they were added directly, not via a skill).
    """
    if enabled_user_skills is None:
        return servers

    user_path = _user_config_path(user_id)
    if not user_path.exists():
        return servers

    try:
        user_cfg = json.loads(user_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, PermissionError, OSError):
        return servers

    user_servers = set(user_cfg.get("servers", {}).keys())
    enabled_set = set(enabled_user_skills)

    filtered: dict[str, dict] = {}
    for sname, cfg in servers.items():
        if sname in user_servers:
            source_skill = cfg.get("_source_skill") if isinstance(cfg, dict) else None
            if source_skill and source_skill not in enabled_set:
                continue
        filtered[sname] = cfg
    return filtered


def _offline_status(name: str, cfg: dict) -> dict:
    transport = cfg.get("transport", _detect_transport(cfg))
    return {
        "name": name,
        "connected": False,
        "transport": transport,
        "tool_count": 0,
        "tools": [],
        "last_error": "Not connected yet",
        "last_connect_time": 0,
        "runtime": cfg.get("_runtime", "default"),
        "dependency_status": cfg.get("_runtime_status"),
        "network_egress": transport in {"http", "sse"} or bool(cfg.get("_requires_network")),
    }


# ── Tool registration / deregistration helpers ───────────────────────────────


def _sanitize_mcp_name_component(name: str) -> str:
    """Replace characters that aren't safe in tool names."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def _normalize_tool_def(tool_def) -> tuple[str, dict, str]:
    """Return ``(tool_name, input_schema, description)`` for an MCP tool."""
    if hasattr(tool_def, "name"):
        tool_name = tool_def.name
        input_schema = getattr(
            tool_def, "inputSchema", {"type": "object", "properties": {}}
        )
        description = getattr(tool_def, "description", f"MCP tool: {tool_name}")
    else:
        tool_name = tool_def["name"]
        input_schema = tool_def.get(
            "inputSchema", {"type": "object", "properties": {}}
        )
        description = tool_def.get("description", f"MCP tool: {tool_name}")
    return tool_name, input_schema or {}, description or f"MCP tool: {tool_name}"


def _public_tool_name(server_name: str, tool_name: str) -> str:
    return (
        f"mcp_{_sanitize_mcp_name_component(server_name)}_"
        f"{_sanitize_mcp_name_component(tool_name)}"
    )


def _register_mcp_tool(server_name: str, tool_def) -> str:
    """Return the session-local public name for an MCP tool.

    MCP tools deliberately do not enter the process-global ToolRegistry. Their
    schemas and handlers belong to one ``(user_id, session_id)`` runtime and are
    resolved through :func:`get_session_tool_definitions` and
    :func:`dispatch_mcp_tool`.
    """
    tool_name, _, _ = _normalize_tool_def(tool_def)
    return _public_tool_name(server_name, tool_name)


def _deregister_mcp_tools(server_name: str) -> None:
    """Compatibility no-op: MCP tools are no longer globally registered."""
    return None


def get_session_tool_definitions(
    user_id: str,
    session_id: str = "default",
) -> list[dict]:
    """Build the model-visible MCP catalog for exactly one session."""
    states = _mcp_states.get((user_id, session_id), {})
    definitions: list[dict] = []
    seen: set[str] = set()
    for server_name, state in sorted(states.items()):
        if not state.connected:
            continue
        for tool_def in state.tools:
            tool_name, input_schema, description = _normalize_tool_def(tool_def)
            public_name = _public_tool_name(server_name, tool_name)
            if public_name in seen:
                logger.warning(
                    "Duplicate session MCP tool name %s for user=%s session=%s",
                    public_name, user_id, session_id,
                )
                continue
            seen.add(public_name)
            definitions.append({
                "type": "function",
                "function": {
                    "name": public_name,
                    "description": f"[MCP:{server_name}] {description}",
                    "parameters": {
                        "type": input_schema.get("type", "object"),
                        "properties": input_schema.get("properties", {}),
                        "required": input_schema.get("required", []),
                    },
                },
            })
    return definitions


def get_session_tool_names(
    user_id: str,
    session_id: str = "default",
) -> list[str]:
    return [
        item["function"]["name"]
        for item in get_session_tool_definitions(user_id, session_id)
    ]


def _resolve_session_tool(
    public_name: str,
    user_id: str,
    session_id: str,
) -> tuple[str, str, "MCPServerState"] | None:
    states = _mcp_states.get((user_id, session_id), {})
    for server_name, state in states.items():
        for tool_def in state.tools:
            tool_name, _, _ = _normalize_tool_def(tool_def)
            if _public_tool_name(server_name, tool_name) == public_name:
                return server_name, tool_name, state
    return None


async def _call_mcp_state_tool(
    state: "MCPServerState",
    tool_name: str,
    params: dict,
) -> str:
    if state.transport == "stdio":
        if state.session is None:
            raise RuntimeError("stdio MCP session is unavailable")
        async with state._rpc_lock:
            result = await state.session.call_tool(tool_name, params)
        return _format_stdio_tool_result(result, state)

    result = await _send_request(
        state, "tools/call", {"name": tool_name, "arguments": params}
    )
    return json.dumps(_parse_http_tool_result(result), ensure_ascii=False)


async def dispatch_mcp_tool(
    public_name: str,
    args: dict,
    user_id: str,
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
) -> str:
    """Dispatch one session-local MCP tool, reconnecting once on failure."""
    await connect_all_for_user(
        user_id, session_id,
        enabled_user_skills=enabled_user_skills,
    )
    route = _resolve_session_tool(public_name, user_id, session_id)
    if route is None:
        return json.dumps({
            "error": f"MCP tool '{public_name}' is unavailable in this session",
            "session_id": session_id,
        }, ensure_ascii=False)

    server_name, tool_name, state = route
    params = dict(args)
    for attempt in (1, 2):
        if not state.connected:
            state = await connect_server(user_id, server_name, session_id)
        if not state or not state.connected:
            error = state.last_error if state else "not configured"
            return json.dumps({
                "error": f"MCP server '{server_name}' is not connected: {error}",
                "session_id": session_id,
            }, ensure_ascii=False)
        try:
            result = await _call_mcp_state_tool(state, tool_name, params)
            _record_success(state)
            return result
        except Exception as exc:
            state.last_error = _sanitize_error(_exc_str(exc))
            state.connected = False
            _record_failure(state)
            logger.warning(
                "MCP tool %s/%s failed (attempt %d): %s",
                server_name, tool_name, attempt, exc,
            )
            if attempt == 1:
                await disconnect_server(user_id, server_name, session_id)
                state = await connect_server(user_id, server_name, session_id)
                continue
            return json.dumps({
                "error": state.last_error,
                "server": server_name,
                "session_id": session_id,
                "retry_exhausted": True,
            }, ensure_ascii=False)

    return json.dumps({"error": "unreachable MCP dispatch state"}, ensure_ascii=False)


def _format_stdio_tool_result(result, state: "MCPServerState" | None = None) -> str:
    """Format a stdio MCP tool call result into a string.

    Appends a provenance line so the model knows the actual target address
    and cannot hallucinate IPs or config-file paths.
    """
    content = getattr(result, "content", [])
    text_parts = []
    for item in content:
        if hasattr(item, "text"):
            text_parts.append(item.text)
        elif hasattr(item, "data") and hasattr(item, "mimeType"):
            mime = str(item.mimeType or "").split(";", 1)[0].strip().lower()
            text_parts.append(f"[Image: {mime}]")
            if hasattr(item, "data") and item.data:
                text_parts.append(f"[base64 data, {len(item.data)} chars]")
        elif hasattr(item, "resource"):
            text_parts.append(f"[Resource: {item.resource}]")
        else:
            text_parts.append(str(item))
    text = "\n".join(text_parts) if text_parts else str(result)

    # ── Inject provenance: actual target address ──────────────────────
    if "实际调用地址:" not in text:
        prov = _build_provenance_line(state)
        if prov:
            text = text.rstrip() + "\n" + prov
    return text


def _build_provenance_line(state: "MCPServerState | None") -> str:
    """Build a human-readable provenance line for the model context.

    Uses the server config's env to determine whether the target address was
    explicitly configured or is managed internally by the MCP tool.
    """
    if state is None:
        return ""
    cfg_env = state.config.get("env") if state.config else None
    api_url = None
    if isinstance(cfg_env, dict):
        api_url = cfg_env.get("PATHOLOGY_API_URL")
    if api_url:
        return f"实际调用地址: {api_url} (来源: 环境变量)"
    return "实际调用地址由 MCP 工具内部管理，未通过 harness 显式配置"


def _parse_http_tool_result(result: dict) -> dict:
    """Parse an HTTP MCP tool call result."""
    content = result.get("content", [])
    text_parts = []
    for item in content:
        if isinstance(item, dict):
            item_type = item.get("type", "text")
            if item_type == "text":
                text_parts.append(item.get("text", ""))
            elif item_type == "image":
                text_parts.append(f"[Image: {item.get('mimeType', 'image/png')}]")
                if "data" in item:
                    text_parts.append(item["data"][:200])
                if "url" in item:
                    text_parts.append(f"URL: {item['url']}")
            elif item_type == "resource":
                text_parts.append(f"[Resource: {item.get('resource', {})}]")
            else:
                text_parts.append(json.dumps(item))
        else:
            text_parts.append(str(item))

    return {
        "server": "mcp",
        "result": "\n".join(text_parts) if text_parts else str(result),
    }


# ── State management ─────────────────────────────────────────────────────────


def _get_state(user_id: str, server_name: str, session_id: str = "default") -> MCPServerState | None:
    """Get the runtime state for a server, or None."""
    key = (user_id, session_id)
    user_states = _mcp_states.get(key, {})
    return user_states.get(server_name)


async def _get_or_connect(
    user_id: str, server_name: str, session_id: str = "default",
) -> MCPServerState | None:
    """Get state, attempting connect if not already connected.

    Persistent transports run as a background asyncio Task because their
    connection context blocks on _shutdown_event.wait(). We spawn the task and
    wait for _ready_event (with timeout) instead.
    """
    state = _get_state(user_id, server_name, session_id)
    if state is not None and not state.connected:
        task_done = state._bg_connect_task is None or state._bg_connect_task.done()
        if state._shutdown_event.is_set() or task_done:
            _mcp_states.get((user_id, session_id), {}).pop(server_name, None)
            state = None

    if state is None:
        config = _load_config(user_id, session_id).get("servers", {}).get(server_name)
        if not config:
            return None
        state = MCPServerState(server_name, config)
        state._session_id = session_id
        key = (user_id, session_id)
        _mcp_states.setdefault(key, {})[server_name] = state

    if not state.connected:
        if state.transport in {"stdio", "sse"}:
            logger.info(
                "MCP _get_or_connect: spawning background %s task for '%s', config=%s",
                state.transport, server_name,
                {k: v for k, v in state.config.items() if k != 'env'},
            )
            # Persistent SDK transports remain alive until shutdown.
            bg_task = asyncio.create_task(_connect(state))
            state._bg_connect_task = bg_task
            try:
                await asyncio.wait_for(state._ready_event.wait(), timeout=CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                state.last_error = f"Connection timed out after {CONNECT_TIMEOUT}s"
                logger.warning("MCP _get_or_connect: timeout for '%s'", server_name)
                state._shutdown_event.set()
                _record_failure(state)
                return state
            if not state.connected:
                logger.warning(
                    "MCP _get_or_connect: %s connect failed for '%s', error=%s",
                    state.transport, server_name, state.last_error[:200],
                )
                return state
            logger.info(
                "MCP _get_or_connect: %s connected for '%s'",
                state.transport, server_name,
            )
        else:
            logger.info(
                "MCP _get_or_connect: direct connect for '%s', transport=%s",
                server_name, state.transport,
            )
            await _connect(state)

    return state


# ── MCP JSON-RPC communication (HTTP/SSE transport) ──────────────────────────


async def _send_request(state: MCPServerState, method: str, params: dict | None = None) -> dict:
    """Send a JSON-RPC request to an MCP server via HTTP POST."""
    config = state.config
    url = config["url"]
    headers = config.get("headers", {}).copy()
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json, text/event-stream")

    # Some MCP servers require MCP-Protocol-Version header
    if not any(key.lower() == "mcp-protocol-version" for key in headers):
        headers["mcp-protocol-version"] = LATEST_PROTOCOL_VERSION

    payload = {
        "jsonrpc": JSONRPC_VERSION,
        "id": int(time.time() * 1000),
        "method": method,
        "params": params or {},
    }

    timeout = config.get("timeout", REQUEST_TIMEOUT)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return _parse_sse_response(resp.text)

        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message', str(err))}")
        return data.get("result", data)


def _parse_sse_response(text: str) -> dict:
    """Parse SSE text and extract the JSON-RPC result."""
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                data = json.loads(line[6:])
                if "result" in data:
                    return data["result"]
                if "error" in data:
                    err = data["error"]
                    raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message', str(err))}")
            except json.JSONDecodeError:
                continue
    return {}


# ── Connection (unified dispatcher) ──────────────────────────────────────────


async def _connect(state: MCPServerState) -> bool:
    """Connect to an MCP server using the appropriate transport.

    Dispatches to _connect_stdio, _connect_sse, or _connect_http based on
    the detected transport type.
    """
    if _is_circuit_open(state):
        state.last_error = f"Circuit breaker open — retry after {CIRCUIT_BREAKER_COOLDOWN}s cooldown"
        logger.info("MCP server '%s': circuit breaker open, skipping connect", state.name)
        return False

    transport = state.transport
    logger.info(
        "MCP _connect: server='%s' transport=%s config_keys=%s",
        state.name, transport, list(state.config.keys()),
    )
    try:
        if transport == "stdio":
            logger.info("MCP _connect: routing to _connect_stdio for '%s'", state.name)
            ok = await _connect_stdio(state)
        elif transport == "sse":
            logger.info("MCP _connect: routing to _connect_sse for '%s'", state.name)
            ok = await _connect_sse(state)
        else:
            logger.info("MCP _connect: routing to _connect_http for '%s'", state.name)
            ok = await _connect_http(state)

        if ok:
            _record_success(state)
        else:
            _record_failure(state)
        logger.info("MCP _connect result for '%s': ok=%s error=%s", state.name, ok, state.last_error[:200] if state.last_error else 'N/A')
        return ok
    except Exception as e:
        _record_failure(state)
        state.last_error = _sanitize_error(_exc_str(e))
        logger.warning("MCP connect failed for '%s' (%s): %s", state.name, transport, e)
        return False
    finally:
        if transport in {"stdio", "sse"} and not state._ready_event.is_set():
            state._ready_event.set()


async def _connect_stdio(state: MCPServerState) -> bool:
    """Connect to an MCP server via stdio transport (subprocess)."""
    if not _MCP_STDIO_AVAILABLE:
        state.last_error = (
            "mcp package not installed. Stdio transport requires the 'mcp' "
            "Python SDK. Install with: pip install 'mcp>=1.0'"
        )
        return False

    config = state.config
    command = config.get("command")
    args = config.get("args", [])
    user_env = config.get("env")

    if not command:
        state.last_error = "stdio transport requires 'command' in config"
        return False

    if config.get("_runtime") == "session_python_env":
        try:
            from runtime.python_env import (
                ensure_session_runtime,
                resolve_session_python,
                runtime_env_for_subprocess,
            )
            user_id = str(config.get("_user_id") or "default")
            session_id = str(state._session_id or config.get("_session_id") or "default")
            runtime_status = await ensure_session_runtime(
                user_id,
                session_id,
                extra_skill_dirs=[config.get("_source_path")] if config.get("_source_path") else None,
            )
            config["_runtime_status"] = runtime_status.get("status")
            config["_runtime_env_hash"] = runtime_status.get("env_hash")
            if runtime_status.get("status") not in {"ready"}:
                state.last_error = (
                    "Python runtime is not ready: "
                    f"{runtime_status.get('status')} {runtime_status.get('error') or ''}"
                ).strip()
                return False
            runtime_python = resolve_session_python(runtime_status)
            if runtime_python and str(command) in {"python", "python3"}:
                command = runtime_python
            user_env = runtime_env_for_subprocess(runtime_status, _build_safe_env(user_env))
        except Exception as exc:
            state.last_error = f"Python runtime setup failed: {_sanitize_error(_exc_str(exc))}"
            return False

    # Reset ready event for (re)connection
    state._ready_event.clear()

    safe_env = user_env if isinstance(user_env, dict) and config.get("_runtime") == "session_python_env" else _build_safe_env(user_env)

    server_params = StdioServerParameters(
        command=command,
        args=args,
        env=safe_env if safe_env else None,
    )

    errlog = _get_stderr_log(state.name)

    try:
        async with stdio_client(server_params, errlog=errlog) as (read_stream, write_stream):
            # Build ClientSession kwargs (message_handler for dynamic tool refresh)
            session_kwargs: dict = {}
            if _MCP_NOTIFICATION_TYPES and _MCP_MESSAGE_HANDLER_SUPPORTED:
                session_kwargs["message_handler"] = _make_message_handler(state)

            async with ClientSession(read_stream, write_stream, **session_kwargs) as session:
                state.initialize_result = await session.initialize()
                state.session = session
                state.connected = True
                state._reconnect_attempts = 0
                state.last_connect_time = time.time()
                logger.info(
                    "MCP stdio server '%s' initialized (protocol %s)",
                    state.name,
                    getattr(state.initialize_result, "protocolVersion", "unknown"),
                )

                # Discover tools
                await _discover_and_register_tools(state)
                state._ready_event.set()

                # Start keepalive
                state._keepalive_task = asyncio.create_task(_keepalive_loop(state))

                # Block until shutdown
                await state._shutdown_event.wait()
    except Exception as e:
        state.connected = False
        state.last_error = _sanitize_error(_exc_str(e))
        _deregister_mcp_tools(state.name)
        state.tools = []
        state._ready_event.set()
        return False
    finally:
        state._ready_event.set()
        state.session = None
        if state._keepalive_task:
            state._keepalive_task.cancel()
            state._keepalive_task = None

    return True


async def _connect_http(state: MCPServerState) -> bool:
    """Connect to an MCP server via HTTP/StreamableHTTP transport."""
    config = state.config
    url = config.get("url")
    if not url:
        state.last_error = "HTTP transport requires 'url' in config"
        return False

    state.connected = False
    state.last_error = ""

    try:
        # 1. Initialize
        init_result = await _send_request(
            state, "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "chat_ds", "version": "1.0.0"},
            },
        )

        state.connected = True
        state._reconnect_attempts = 0
        state.last_connect_time = time.time()
        logger.info(
            "MCP server '%s' initialized (protocol %s)",
            state.name,
            init_result.get("protocolVersion", "unknown"),
        )

        # 2. Discover tools
        tools_result = await _send_request(state, "tools/list", {})
        tools = tools_result.get("tools", [])
        state.tools = tools

        # 3. Register discovered tools
        _deregister_mcp_tools(state.name)
        state._registered_tool_names = []
        for tool in tools:
            reg_name = _register_mcp_tool(state.name, tool)
            state._registered_tool_names.append(reg_name)

        logger.info("MCP server '%s' registered %d tools", state.name, len(tools))

        # Start keepalive
        state._keepalive_task = asyncio.create_task(_keepalive_loop(state))

        return True

    except Exception as e:
        state.connected = False
        state.last_error = _sanitize_error(str(e))
        logger.warning("MCP HTTP connect failed for '%s': %s", state.name, e)
        _deregister_mcp_tools(state.name)
        state.tools = []
        return False


async def _connect_sse(state: MCPServerState) -> bool:
    """Connect to an MCP server via SSE transport."""
    if sse_client is None:
        state.last_error = (
            "SSE transport not available. Upgrade the 'mcp' package to get SSE support."
        )
        return False

    config = state.config
    url = config.get("url")
    if not url:
        state.last_error = "SSE transport requires 'url' in config"
        return False

    state.connected = False
    state.last_error = ""

    try:
        async with sse_client(url) as (read_stream, write_stream):
            session_kwargs: dict = {}
            if _MCP_NOTIFICATION_TYPES and _MCP_MESSAGE_HANDLER_SUPPORTED:
                session_kwargs["message_handler"] = _make_message_handler(state)

            async with ClientSession(read_stream, write_stream, **session_kwargs) as session:
                state.initialize_result = await session.initialize()
                state.session = session
                state.connected = True
                state._reconnect_attempts = 0
                state.last_connect_time = time.time()
                logger.info(
                    "MCP SSE server '%s' initialized (protocol %s)",
                    state.name,
                    getattr(state.initialize_result, "protocolVersion", "unknown"),
                )

                await _discover_and_register_tools(state)
                state._ready_event.set()

                state._keepalive_task = asyncio.create_task(_keepalive_loop(state))

                await state._shutdown_event.wait()
    except Exception as e:
        state.connected = False
        state.last_error = _sanitize_error(_exc_str(e))
        _deregister_mcp_tools(state.name)
        state.tools = []
        state._ready_event.set()
        return False
    finally:
        state._ready_event.set()
        state.session = None
        if state._keepalive_task:
            state._keepalive_task.cancel()
            state._keepalive_task = None

    return True


async def _discover_and_register_tools(state: MCPServerState) -> None:
    """Discover tools from a connected session and register them."""
    async with state._rpc_lock:
        tools_result = await state.session.list_tools()
    tools = tools_result.tools if hasattr(tools_result, "tools") else []
    state.tools = tools

    _deregister_mcp_tools(state.name)
    state._registered_tool_names = []
    for tool in tools:
        reg_name = _register_mcp_tool(state.name, tool)
        state._registered_tool_names.append(reg_name)

    logger.info("MCP server '%s' registered %d tools", state.name, len(tools))


async def _reconnect(state: MCPServerState) -> bool:
    """Reconnect with exponential backoff."""
    max_attempts = state.config.get("max_reconnect_attempts", MAX_RECONNECT_ATTEMPTS)

    for attempt in range(1, max_attempts + 1):
        delay = RECONNECT_BASE_DELAY * (2 ** (attempt - 1))
        logger.info(
            "MCP reconnect '%s' attempt %d/%d (delay %.1fs)",
            state.name, attempt, max_attempts, delay,
        )
        await asyncio.sleep(delay)

        if await _connect(state):
            return True

    logger.error("MCP reconnect failed for '%s' after %d attempts", state.name, max_attempts)
    return False


# ── Keepalive ────────────────────────────────────────────────────────────────


async def _keepalive_loop(state: MCPServerState) -> None:
    """Background task that periodically pings the MCP server to detect staleness."""
    try:
        while not state._shutdown_event.is_set():
            # Wait for KEEPALIVE_INTERVAL or shutdown
            try:
                await asyncio.wait_for(
                    state._shutdown_event.wait(),
                    timeout=KEEPALIVE_INTERVAL,
                )
                break  # shutdown was set
            except asyncio.TimeoutError:
                pass  # time to send keepalive

            if state._shutdown_event.is_set():
                break
            if not state.connected or state.session is None:
                continue

            try:
                async with state._rpc_lock:
                    await asyncio.wait_for(
                        state.session.list_tools(),
                        timeout=KEEPALIVE_TIMEOUT,
                    )
            except Exception as exc:
                logger.warning(
                    "MCP server '%s' keepalive failed, marking disconnected: %s",
                    state.name, exc,
                )
                state.connected = False
                state.last_error = _sanitize_error(_exc_str(exc))
                state._shutdown_event.set()
                # The next dispatch/get_or_connect creates a fresh runtime.
                break
    except asyncio.CancelledError:
        pass


# ── Dynamic tool refresh (tools/list_changed) ────────────────────────────────


def _make_message_handler(state: MCPServerState):
    """Build a ``message_handler`` callback for ``ClientSession``.

    Dispatches on notification type. Only ``ToolListChangedNotification``
    triggers a refresh.
    """

    async def _handler(message):
        try:
            if isinstance(message, Exception):
                logger.debug("MCP message handler (%s): exception: %s", state.name, message)
                return
            if _MCP_NOTIFICATION_TYPES and isinstance(message, ServerNotification):
                if isinstance(message.root, ToolListChangedNotification):
                    logger.info(
                        "MCP server '%s': received tools/list_changed notification",
                        state.name,
                    )
                    # Schedule refresh in a background task to avoid blocking
                    # the SDK notification handler (prevents stdio stream wedging)
                    asyncio.create_task(_refresh_tools(state))
        except Exception:
            logger.exception("Error in MCP message handler for '%s'", state.name)

    return _handler


async def _refresh_tools(state: MCPServerState) -> None:
    """Re-fetch tools and update this session-local server catalog."""
    try:
        old_tool_names = set(state._registered_tool_names)

        async with state._rpc_lock:
            tools_result = await state.session.list_tools()
        new_mcp_tools = tools_result.tools if hasattr(tools_result, "tools") else []

        # Compute the session-local catalog diff.
        new_names = {
            _register_mcp_tool(state.name, tool)
            for tool in new_mcp_tools
        }

        state.tools = new_mcp_tools
        state._registered_tool_names = sorted(new_names)

        # Log changes
        added = new_names - old_tool_names
        removed = old_tool_names - new_names
        changes = []
        if added:
            changes.append(f"added: {', '.join(sorted(added))}")
        if removed:
            changes.append(f"removed: {', '.join(sorted(removed))}")
        if changes:
            logger.warning(
                "MCP server '%s': tools changed dynamically — %s",
                state.name, "; ".join(changes),
            )
        else:
            logger.info(
                "MCP server '%s': dynamically refreshed %d tool(s) (no changes)",
                state.name, len(state._registered_tool_names),
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("MCP server '%s': dynamic tool refresh failed", state.name)


# ── Background connection management ──────────────────────────────────────────


async def connect_server(
    user_id: str, server_name: str, session_id: str = "default",
) -> MCPServerState | None:
    """Connect once per ``(user, session, server)`` under a keyed lock."""
    key = (user_id, session_id, server_name)
    lock = _mcp_connect_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await _get_or_connect(user_id, server_name, session_id)


async def disconnect_server(
    user_id: str, server_name: str, session_id: str = "default",
) -> None:
    """Disconnect one session-local server and tear down its transport."""
    state = _get_state(user_id, server_name, session_id)
    if state:
        state.connected = False
        state._shutdown_event.set()

        if state._keepalive_task:
            state._keepalive_task.cancel()
            await asyncio.gather(state._keepalive_task, return_exceptions=True)
            state._keepalive_task = None

        # Let the SDK context manager close the subprocess/stream cleanly.
        task = state._bg_connect_task
        if task and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            state._bg_connect_task = None

        _deregister_mcp_tools(server_name)
        state.tools = []
        state.session = None

    key = (user_id, session_id)
    user_states = _mcp_states.get(key, {})
    user_states.pop(server_name, None)


async def connect_all_for_user(
    user_id: str,
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
) -> dict[str, bool]:
    """Connect to all configured MCP servers for a user+session.

    When enabled_user_skills is provided, user-level MCP servers whose
    _source_skill is not in the whitelist are skipped entirely — they
    are neither connected nor exposed to the agent.
    """
    config = _load_config(user_id, session_id)
    results = {}

    # Determine which servers are user-level (came from user config file)
    user_servers: set[str] = set()
    user_path = _user_config_path(user_id)
    if user_path.exists():
        try:
            user_cfg = json.loads(user_path.read_text(encoding="utf-8"))
            user_servers = set(user_cfg.get("servers", {}).keys())
        except (json.JSONDecodeError, PermissionError, OSError):
            pass

    for server_name in config.get("servers", {}):
        # Filter user-level MCP servers by enabled_user_skills whitelist
        if (
            enabled_user_skills is not None
            and server_name in user_servers
        ):
            server_cfg = config["servers"][server_name]
            source_skill = server_cfg.get("_source_skill")
            if source_skill and source_skill not in enabled_user_skills:
                continue  # skip — skill not enabled in this session

        state = await connect_server(user_id, server_name, session_id)
        results[server_name] = state.connected if state else False
    return results


async def disconnect_all_for_user(
    user_id: str, session_id: str = "default",
) -> None:
    """Disconnect all servers for a user+session."""
    config = _load_config(user_id, session_id)
    for server_name in config.get("servers", {}):
        await disconnect_server(user_id, server_name, session_id)
    _mcp_states.pop((user_id, session_id), None)



async def cleanup_session_runtime(user_id: str, session_id: str) -> dict:
    """Disconnect and forget all runtime/config state for one session."""
    await disconnect_all_for_user(user_id, session_id)
    _mcp_states.pop((user_id, session_id), None)
    for key in list(_mcp_connect_locks):
        if key[0] == user_id and key[1] == session_id:
            _mcp_connect_locks.pop(key, None)

    path = _session_config_path(user_id, session_id)
    removed_config = False
    if session_id != DEFAULT_SESSION_ID and path.exists():
        try:
            path.unlink()
            removed_config = True
            try:
                path.parent.rmdir()
            except OSError:
                pass
        except OSError as exc:
            return {"success": False, "error": str(exc)}
    return {"success": True, "removed_config": removed_config}

def get_all_mcp_sessions() -> list[tuple[str, str]]:
    """List all (user_id, session_id) pairs that have MCP server config files.

    Scans both user-level (data/mcp/<uid>/servers.json) and session-level
    (data/mcp/<uid>/<sid>/servers.json) paths.
    """
    if not MCP_CONFIG_BASE.exists():
        return []
    try:
        sessions: list[tuple[str, str]] = []
        for user_dir in MCP_CONFIG_BASE.iterdir():
            if not user_dir.is_dir():
                continue
            user_id = user_dir.name
            # User-level config
            if (user_dir / MCP_CONFIG_FILE).exists():
                sessions.append((user_id, DEFAULT_SESSION_ID))
            # Session-level configs
            for sub_dir in user_dir.iterdir():
                if sub_dir.is_dir() and (sub_dir / MCP_CONFIG_FILE).exists():
                    sessions.append((user_id, sub_dir.name))
        return sorted(sessions)
    except OSError:
        return []


def get_active_mcp_sessions() -> list[tuple[str, str]]:
    """Return runtime scopes that currently own live or connecting servers."""
    return sorted(_mcp_states)


# ── MCP Management Tools (registered as agent tools) ─────────────────────────


MCP_SERVER_ADD_SCHEMA = {
    "name": "mcp_server_add",
    "description": (
        "Add or update an MCP server configuration. The server will be connected "
        "and its tools will become available as 'mcp_<server>_<tool>'. "
        "For HTTP servers, provide 'url'. For stdio servers (subprocess), "
        "provide 'command' and 'args'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "A unique name for this MCP server.",
            },
            "url": {
                "type": "string",
                "description": "HTTP/SSE endpoint URL for the MCP server. Required for http/sse transport.",
            },
            "command": {
                "type": "string",
                "description": "Command to spawn for stdio transport (e.g., 'python', 'npx').",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Command arguments for stdio transport.",
            },
            "env": {
                "type": "object",
                "description": "Environment variables for stdio transport subprocess.",
            },
            "transport": {
                "type": "string",
                "enum": ["http", "sse", "stdio"],
                "description": "Transport type. Auto-detected if omitted (command→stdio, transport:sse→sse, default→http).",
            },
            "headers": {
                "type": "object",
                "description": "Optional HTTP headers (e.g., Authorization) for http/sse transport.",
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds (default 120).",
            },
        },
        "required": ["name"],
    },
}


async def mcp_server_add(
    name: str,
    url: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict | None = None,
    transport: str | None = None,
    headers: dict | None = None,
    timeout: int = 120,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Add or update an MCP server configuration."""
    try:
        # Validate: must have either url or command
        if not url and not command:
            return json.dumps({
                "success": False,
                "error": "Either 'url' (for http/sse transport) or 'command' (for stdio transport) must be provided.",
            }, ensure_ascii=False)

        config = _load_scope_config(user_id, session_id)
        servers = config.get("servers", {})

        server_config: dict = {
            "timeout": timeout,
            "_user_id": user_id,
            "_session_id": session_id,
        }

        if command:
            # Reject obviously wrong stdio args that won't work
            if args:
                if args == ["-c", "import pathology_mcp"] or args == ["-c", "import pathology_mcp; pathology_mcp.main()"]:
                    return json.dumps({
                        "success": False,
                        "error": (
                            "Invalid args for stdio MCP server. "
                            "The args must be the FULL FILE PATH to the Python script, "
                            "e.g. args=['/app/data/skills/.../pathology_mcp.py']. "
                            "Do NOT use '-c' with import statements — that will not start the MCP server. "
                            "Use skill_view to see the correct args."
                        ),
                    }, ensure_ascii=False)
                if "-c" in args and any("import" in a for a in args):
                    return json.dumps({
                        "success": False,
                        "error": (
                            "Invalid args: using '-c' with 'import' is not a valid way to start a stdio MCP server. "
                            "The args must be the FULL FILE PATH to the Python script. "
                            "Use skill_view to get the correct args and copy them EXACTLY."
                        ),
                    }, ensure_ascii=False)
            server_config["command"] = command
            if command in {"python", "python3"} or str(command).endswith(("/python", "/python3")):
                server_config.setdefault("_runtime", "session_python_env")
                server_config.setdefault("_requires_network", True)
            if args:
                server_config["args"] = args
            if env:
                server_config["env"] = env
            server_config["transport"] = transport or "stdio"
        else:
            server_config["url"] = url
            server_config["transport"] = transport or "http"
            if headers:
                server_config["headers"] = headers

        is_update = name in servers
        servers[name] = server_config
        config["servers"] = servers
        _save_config(user_id, config, session_id)

        # Disconnect old if updating
        if is_update:
            await disconnect_server(user_id, name, session_id)

        # Connect immediately
        state = await connect_server(user_id, name, session_id)
        if state and state.connected:
            tool_names = []
            for t in state.tools:
                if hasattr(t, "name"):
                    tool_names.append(t.name)
                elif isinstance(t, dict):
                    tool_names.append(t.get("name", "unknown"))
                else:
                    tool_names.append(str(t))
            return json.dumps({
                "success": True,
                "action": "updated" if is_update else "added",
                "server": name,
                "transport": state.transport,
                "tools": tool_names,
                "tool_count": len(state.tools),
            }, ensure_ascii=False)
        else:
            error = state.last_error if state else "unknown error"
            return json.dumps({
                "success": False,
                "action": "added",
                "server": name,
                "error": f"Configuration saved but connection failed: {error}",
                "hint": "The server config was saved. Check the command/url and try mcp_server_status later.",
            }, ensure_ascii=False)

    except Exception as e:
        logger.exception("mcp_server_add error")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


MCP_SERVER_REMOVE_SCHEMA = {
    "name": "mcp_server_remove",
    "description": "Remove an MCP server configuration and deregister its tools.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the MCP server to remove.",
            },
        },
        "required": ["name"],
    },
}


async def mcp_server_remove(
    name: str,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Remove an MCP server configuration."""
    try:
        config = _load_scope_config(user_id, session_id)
        servers = config.get("servers", {})

        if name not in servers:
            return json.dumps({"success": False, "error": f"Server '{name}' not found."}, ensure_ascii=False)

        del servers[name]
        config["servers"] = servers
        _save_config(user_id, config, session_id)

        await disconnect_server(user_id, name, session_id)

        return json.dumps({"success": True, "server": name, "action": "removed"}, ensure_ascii=False)

    except Exception as e:
        logger.exception("mcp_server_remove error")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


MCP_SERVER_LIST_SCHEMA = {
    "name": "mcp_server_list",
    "description": "List all configured MCP servers for your account.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def mcp_server_list(
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
) -> str:
    """List all MCP server configurations."""
    try:
        config = _load_config(user_id, session_id)
        servers = _filter_user_mcp_servers(
            config.get("servers", {}),
            user_id,
            enabled_user_skills,
        )
        result = []
        for sname, cfg in servers.items():
            state = _get_state(user_id, sname, session_id)
            result.append({
                "name": sname,
                "transport": cfg.get("transport", _detect_transport(cfg)),
                "url": cfg.get("url", ""),
                "command": cfg.get("command", ""),
                "connected": state.connected if state else False,
                "tool_count": len(state.tools) if state else 0,
                "runtime": cfg.get("_runtime", "default"),
                "dependency_status": cfg.get("_runtime_status"),
                "network_egress": cfg.get("transport", _detect_transport(cfg)) in {"http", "sse"} or bool(cfg.get("_requires_network")),
            })
        return json.dumps({"success": True, "servers": result, "count": len(result)}, ensure_ascii=False)

    except Exception as e:
        logger.exception("mcp_server_list error")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


MCP_SERVER_STATUS_SCHEMA = {
    "name": "mcp_server_status",
    "description": "Check the connection status and discovered tools for an MCP server.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the MCP server to check. If omitted, shows all.",
            },
        },
        "required": [],
    },
}


async def mcp_server_status(
    name: str | None = None,
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
) -> str:
    """Check MCP server connection status."""
    try:
        config = _load_config(user_id, session_id)
        servers = _filter_user_mcp_servers(
            config.get("servers", {}),
            user_id,
            enabled_user_skills,
        )

        if name:
            if name not in servers:
                return json.dumps({"success": False, "error": f"Server '{name}' not found."}, ensure_ascii=False)
            state = _get_state(user_id, name, session_id)
            if state:
                return json.dumps({"success": True, "server": state.to_status()}, ensure_ascii=False)
            else:
                return json.dumps({
                    "success": True,
                    "server": _offline_status(name, servers[name]),
                }, ensure_ascii=False)

        # All servers
        results = []
        for sname in servers:
            state = _get_state(user_id, sname, session_id)
            if state:
                results.append(state.to_status())
            else:
                results.append(_offline_status(sname, servers[sname]))

        return json.dumps({"success": True, "servers": results, "count": len(results)}, ensure_ascii=False)

    except Exception as e:
        logger.exception("mcp_server_status error")
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
