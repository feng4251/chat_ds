"""MCP auto-registration from skill directories.

When a skill is uploaded or created, this module automatically discovers and
registers MCP servers so the agent never needs to manually call mcp_server_add.

Discovery priority (openclaw-compatible):
  1. .mcp.json at skill root — openclaw standard format
  2. SKILL.md frontmatter ``mcp_servers`` field
  3. Root-level .py files with "mcp" in the filename (heuristic fallback)

Reference: openclaw/src/plugins/bundle-mcp.ts
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from skills.loader import parse_frontmatter, substitute_template_vars
from runtime.python_env import scan_skill_runtime

logger = logging.getLogger(__name__)


async def auto_register_skill_mcp(
    skill_dir: str,
    user_id: str,
    session_id: str = "default",
) -> dict[str, Any]:
    """Auto-register MCP servers discovered in a skill directory.

    Reads MCP configuration from the skill directory (priority order above),
    writes entries to ``data/mcp/<user_id>/servers.json``, and triggers
    connection for each newly registered server.

    Args:
        skill_dir: Absolute path to the skill directory.
        user_id: User identifier.
        session_id: Session identifier (for template substitution).

    Returns:
        Dict with keys: registered (list), skipped (list), errors (list).
    """
    skill_path = Path(skill_dir).resolve()
    if not skill_path.is_dir():
        return {
            "registered": [],
            "skipped": [],
            "errors": [f"Skill directory not found: {skill_dir}"],
        }

    # ── Step 1: Discover MCP server configurations ──────────────────────
    mcp_servers: dict[str, dict] = {}

    # Priority 1: .mcp.json (openclaw-compatible)
    mcp_json = skill_path / ".mcp.json"
    if mcp_json.is_file():
        try:
            raw = json.loads(mcp_json.read_text(encoding="utf-8"))
            servers = raw.get("mcpServers", raw.get("servers", {}))
            for name, cfg in servers.items():
                if isinstance(cfg, dict):
                    mcp_servers[name] = _resolve_config_paths(
                        cfg, str(skill_path), session_id
                    )
            if mcp_servers:
                logger.info(
                    "MCP auto-discovery: found %d server(s) in .mcp.json for skill %s",
                    len(mcp_servers), skill_path.name,
                )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to parse .mcp.json in %s: %s", skill_dir, e)

    # Priority 2: SKILL.md frontmatter mcp_servers
    if not mcp_servers:
        skill_md = skill_path / "SKILL.md"
        if skill_md.is_file():
            try:
                content = skill_md.read_text(encoding="utf-8")
                frontmatter, _ = parse_frontmatter(content)
                fm_servers = frontmatter.get("mcp_servers")
                if fm_servers and isinstance(fm_servers, list):
                    for entry in fm_servers:
                        if isinstance(entry, dict):
                            name = entry.get("name")
                            if name:
                                resolved = {}
                                for k, v in entry.items():
                                    if isinstance(v, str):
                                        resolved[k] = substitute_template_vars(
                                            v, skill_dir=str(skill_path),
                                            session_id=session_id,
                                        )
                                    elif isinstance(v, list):
                                        resolved[k] = [
                                            substitute_template_vars(
                                                item, skill_dir=str(skill_path),
                                                session_id=session_id,
                                            ) if isinstance(item, str) else item
                                            for item in v
                                        ]
                                    else:
                                        resolved[k] = v
                                mcp_servers[name] = resolved
                    if mcp_servers:
                        logger.info(
                            "MCP auto-discovery: found %d server(s) in SKILL.md "
                            "frontmatter for skill %s",
                            len(mcp_servers), skill_path.name,
                        )
            except (OSError, UnicodeDecodeError) as e:
                logger.warning("Failed to read SKILL.md in %s: %s", skill_dir, e)

    # Priority 3: Root-level .py files with "mcp" in filename (heuristic)
    # NOTE: This logic is duplicated in backend/routers/skill_router.py
    # _auto_register_mcp().  If a third copy appears, extract a shared
    # function (e.g. _discover_mcp_servers_from_skill_dir()) instead of
    # copying again.  Keep both copies in sync on any change.
    if not mcp_servers:
        root_py = sorted(
            f for f in skill_path.glob("*.py")
            if "mcp" in f.name.lower()
        )
        if root_py:
            # Use the first matching script; name the server after the skill
            script_path = str(root_py[0])
            server_name = skill_path.name
            mcp_servers[server_name] = {
                "command": "python",
                "args": [script_path],
                "transport": "stdio",
            }
            logger.info(
                "MCP auto-discovery: inferred stdio server '%s' from script %s",
                server_name, root_py[0].name,
            )

    if not mcp_servers:
        return {"registered": [], "skipped": [], "errors": []}

    runtime_result = scan_skill_runtime(skill_path)

    for server_name, server_config in mcp_servers.items():
        server_config["_source_skill"] = skill_path.name
        server_config["_source_path"] = str(skill_path)
        server_config["_scope"] = "session" if session_id != "default" else "user"
        server_config["_user_id"] = user_id
        server_config["_session_id"] = session_id
        if _detect_runtime_for_server(server_config) == "session_python_env":
            server_config["_runtime"] = "session_python_env"
            server_config["_runtime_status"] = runtime_result.get("status")
            server_config["_requires_network"] = bool(
                (runtime_result.get("dependencies") or {}).get("python_packages")
            )

    # ── Step 2: Load existing config, merge new servers ──────────────────
    from tools.mcp_client import _load_scope_config, _save_config

    config = _load_scope_config(user_id, session_id)
    existing_servers = config.get("servers", {})

    registered = []
    skipped = []
    errors = []

    connect_names: list[str] = []
    for server_name, server_config in mcp_servers.items():
        existing = existing_servers.get(server_name)
        if existing is not None:
            if existing.get("_source_path") not in (None, str(skill_path)):
                errors.append(
                    f"{server_name}: name conflicts with MCP server from "
                    f"{existing.get('_source_path', 'another source')}"
                )
                continue
            skipped.append(server_name)
            connect_names.append(server_name)
            continue

        existing_servers[server_name] = server_config
        connect_names.append(server_name)

    config["servers"] = existing_servers
    _save_config(user_id, config, session_id)

    # ── Step 3: Trigger/reuse connection for effective servers ───────────
    from tools.mcp_client import connect_server

    for server_name in connect_names:
        try:
            state = await connect_server(user_id, server_name, session_id)
            if state and state.connected:
                registered.append(server_name)
                logger.info(
                    "MCP auto-register: connected '%s' (transport=%s)",
                    server_name, state.transport,
                )
            else:
                error = state.last_error if state else "connection failed"
                errors.append(f"{server_name}: {error}")
                logger.warning(
                    "MCP auto-register: '%s' saved but connection failed: %s",
                    server_name, error,
                )
        except Exception as e:
            errors.append(f"{server_name}: {str(e)}")
            logger.exception(
                "MCP auto-register: exception connecting '%s'", server_name,
            )

    return {
        "registered": registered,
        "skipped": skipped,
        "errors": errors,
        "runtime": runtime_result,
    }



def _detect_runtime_for_server(server_config: dict) -> str:
    command = str(server_config.get("command") or "")
    args = [str(arg) for arg in server_config.get("args") or []]
    if command in {"python", "python3"} or command.endswith("/python") or command.endswith("/python3"):
        return "session_python_env"
    if any(arg.endswith(".py") for arg in args):
        return "session_python_env"
    return "default"



async def remove_skill_mcp(
    skill_dir: str,
    user_id: str,
    session_id: str = "default",
) -> dict[str, Any]:
    """Remove MCP configs and live connections owned by one skill."""
    from tools.mcp_client import (
        _load_scope_config,
        _save_config,
        disconnect_server,
    )

    skill_path = str(Path(skill_dir).resolve())
    config = _load_scope_config(user_id, session_id)
    servers = config.get("servers", {})
    removed: list[str] = []
    errors: list[str] = []

    for name, server_config in list(servers.items()):
        source_path = server_config.get("_source_path")
        args = [str(v) for v in server_config.get("args", [])]
        owned = source_path == skill_path or any(
            arg == skill_path or arg.startswith(skill_path + "/")
            for arg in args
        )
        if not owned:
            continue
        try:
            await disconnect_server(user_id, name, session_id)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
        servers.pop(name, None)
        removed.append(name)

    config["servers"] = servers
    _save_config(user_id, config, session_id)
    return {"removed": removed, "errors": errors}

def _resolve_config_paths(
    cfg: dict,
    skill_dir: str,
    session_id: str,
) -> dict:
    """Resolve relative paths and template vars in an MCP server config entry.

    Converts relative paths in ``args`` to absolute paths based on skill_dir,
    and expands ``${SKILL_DIR}`` / ``${SESSION_ID}`` template variables.
    """
    resolved: dict = {}

    for k, v in cfg.items():
        if k == "args" and isinstance(v, list):
            resolved_args = []
            for item in v:
                if isinstance(item, str):
                    # Expand template vars
                    item = substitute_template_vars(
                        item, skill_dir=skill_dir, session_id=session_id,
                    )
                    # Resolve relative paths to absolute
                    p = Path(item)
                    if not p.is_absolute():
                        p = (Path(skill_dir) / p).resolve()
                        item = str(p)
                resolved_args.append(item)
            resolved["args"] = resolved_args
        elif isinstance(v, str):
            resolved[k] = substitute_template_vars(
                v, skill_dir=skill_dir, session_id=session_id,
            )
        else:
            resolved[k] = v

    return resolved
