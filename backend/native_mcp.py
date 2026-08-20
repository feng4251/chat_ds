"""Durable MCP declarations for isolated native Agent Engine Turns."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import or_, select

from models import MCPServerRegistration


MAX_MCP_SERVERS = 128
MAX_MCP_CONFIG_BYTES = 2 * 1024 * 1024
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class NativeMCPError(ValueError):
    pass


def normalize_mcp_name(value: object) -> str:
    name = str(value or "").strip()
    if _SAFE_NAME.fullmatch(name) is None or name in {".", ".."}:
        raise NativeMCPError("Invalid MCP server name")
    return name


def _bounded_string(value: object, *, field: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise NativeMCPError(f"Invalid MCP {field}")
    return value


def normalize_mcp_declaration(value: object) -> dict[str, Any]:
    """Normalize only fields understood by both native runner projections."""

    if not isinstance(value, Mapping):
        raise NativeMCPError("MCP server configuration must be an object")
    server_type = str(
        value.get("type")
        or value.get("transport")
        or ("stdio" if value.get("command") else "http")
    ).strip().lower()
    if server_type == "stdio":
        if value.get("url"):
            raise NativeMCPError("stdio MCP servers cannot declare a URL")
        command = _bounded_string(
            value.get("command"), field="command", limit=4096
        )
        raw_args = value.get("args") or []
        if not isinstance(raw_args, list) or len(raw_args) > 128:
            raise NativeMCPError("Invalid MCP args")
        args = [
            _bounded_string(item, field="argument", limit=8192)
            for item in raw_args
        ]
        raw_env = value.get("env")
        env = None
        if raw_env is not None:
            if not isinstance(raw_env, Mapping) or len(raw_env) > 128:
                raise NativeMCPError("Invalid MCP environment")
            env = {
                _bounded_string(key, field="environment name", limit=256):
                _bounded_string(item, field="environment value", limit=32_768)
                for key, item in raw_env.items()
            }
        normalized = {
            "type": "stdio",
            "command": command,
            "args": args,
            **({"env": env} if env is not None else {}),
        }
    elif server_type in {"http", "sse"}:
        if value.get("command"):
            raise NativeMCPError("Remote MCP servers cannot declare a command")
        url = _bounded_string(value.get("url"), field="URL", limit=8192)
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise NativeMCPError("Invalid MCP URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port is not None and not 1 <= port <= 65535
        ):
            raise NativeMCPError("Invalid MCP URL")
        raw_headers = value.get("headers")
        headers = None
        if raw_headers is not None:
            if not isinstance(raw_headers, Mapping) or len(raw_headers) > 128:
                raise NativeMCPError("Invalid MCP headers")
            headers = {
                _bounded_string(key, field="header name", limit=256):
                _bounded_string(item, field="header value", limit=32_768)
                for key, item in raw_headers.items()
            }
        normalized = {
            "type": server_type,
            "url": url,
            **({"headers": headers} if headers is not None else {}),
        }
    else:
        raise NativeMCPError("Unsupported MCP transport")

    if len(canonical_mcp_json(normalized).encode("utf-8")) > MAX_MCP_CONFIG_BYTES:
        raise NativeMCPError("MCP configuration exceeds its byte limit")
    return normalized


def canonical_mcp_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def effective_mcp_servers(
    db,
    *,
    user_id: str,
    session_id: str,
) -> dict[str, dict[str, Any]]:
    """Resolve user scope first and exact Session overrides second."""

    rows = list((await db.execute(
        select(MCPServerRegistration).where(
            MCPServerRegistration.user_id == user_id,
            or_(
                MCPServerRegistration.session_id.is_(None),
                MCPServerRegistration.session_id == session_id,
            ),
        ).order_by(
            MCPServerRegistration.session_id.is_not(None),
            MCPServerRegistration.name,
        )
    )).scalars().all())
    if len(rows) > MAX_MCP_SERVERS * 2:
        raise NativeMCPError("MCP registration count exceeds its limit")
    resolved: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = normalize_mcp_name(row.name)
        try:
            raw = json.loads(row.config_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise NativeMCPError(
                f"Stored MCP configuration is invalid: {name}"
            ) from exc
        resolved[name] = normalize_mcp_declaration(raw)
    if len(resolved) > MAX_MCP_SERVERS:
        raise NativeMCPError("Effective MCP server count exceeds its limit")
    return dict(sorted(resolved.items()))
