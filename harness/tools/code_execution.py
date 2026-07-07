"""Python execution through a dedicated network-isolated container."""

from __future__ import annotations

import asyncio
import json
import logging
import os

from tools.approval import check_code_danger, check_code_warnings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_CODE_BYTES = 900_000
EXECUTOR_SOCKET = os.environ.get(
    "EXECUTOR_SOCKET", "/run/chat-ds-executor/executor.sock"
)


async def execute_code(
    code: str,
    timeout: int = DEFAULT_TIMEOUT,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Run Python in a separate container with no network namespace."""
    del user_id, session_id  # Runtime context is intentionally not shared.
    if not code or not code.strip():
        return json.dumps({"status": "error", "error": "No code provided."})
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return json.dumps({"status": "error", "error": "Code payload is too large."})

    danger = check_code_danger(code)
    if danger:
        return json.dumps({"status": "blocked", "error": danger})

    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    request = json.dumps(
        {"code": code, "timeout": timeout}, ensure_ascii=False
    ).encode("utf-8") + b"\n"

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(EXECUTOR_SOCKET), timeout=3
        )
        writer.write(request)
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout + 10)
        writer.close()
        await writer.wait_closed()
        if not raw:
            raise RuntimeError("executor closed the socket without a response")
        result = json.loads(raw.decode("utf-8"))
        warnings = check_code_warnings(code)
        if warnings:
            result["warnings"] = warnings
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        logger.exception("Isolated executor request failed")
        return json.dumps({
            "status": "error",
            "error": (
                "The isolated code executor is unavailable: "
                f"{type(exc).__name__}: {exc}"
            ),
        }, ensure_ascii=False)


EXECUTE_CODE_SCHEMA = {
    "name": "execute_code",
    "description": (
        "Run Python code in a dedicated, ephemeral container with networking "
        "disabled. Use it for calculations and pure data processing. It cannot "
        "reach web, internal APIs, MCP servers, or the session filesystem. "
        f"Default timeout is {DEFAULT_TIMEOUT}s; maximum is {MAX_TIMEOUT}s."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source code to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum execution time in seconds (default {DEFAULT_TIMEOUT}).",
                "default": DEFAULT_TIMEOUT,
                "minimum": 1,
                "maximum": MAX_TIMEOUT,
            },
        },
        "required": ["code"],
    },
}
