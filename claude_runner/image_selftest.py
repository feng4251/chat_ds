"""Exact-entrypoint conformance test embedded in the Claude Turn image."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any

from claude_runner.runner_entrypoint import _claude_command
from claude_runner.runtime_capabilities import render_runtime_capability_prompt


SELF_TEST_SCHEMA = "chatds.claude-runner-image-self-test.v1"
_MCP_MODULES = {
    "claude_runner.mcp_process": {
        "process_open",
        "process_write",
        "process_read",
        "process_close",
    },
    "claude_runner.mcp_web_search": {"web_search"},
    "claude_runner.mcp_market_data": {"market_quote"},
    "claude_runner.mcp_schedule_control": {"schedule_create"},
}
_MCP_COMPATIBILITY_PATHS = {
    "claude_runner.mcp_process": "/app/claude-runner/mcp_process.py",
    "claude_runner.mcp_web_search": "/app/claude-runner/mcp_web_search.py",
    "claude_runner.mcp_market_data": "/app/claude-runner/mcp_market_data.py",
    "claude_runner.mcp_schedule_control": "/app/claude-runner/mcp_schedule_control.py",
}


def _mcp_tools(entrypoint: list[str]) -> set[str]:
    requests = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize",'
        '"params":{"protocolVersion":"2025-06-18"}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
    ).encode("utf-8")
    completed = subprocess.run(
        [sys.executable, "-I", *entrypoint],
        input=requests,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        raise RuntimeError("mcp_entrypoint_self_test_failed")
    try:
        replies = [
            json.loads(line)
            for line in completed.stdout.decode("utf-8").splitlines()
            if line.strip()
        ]
        initialized = next(row for row in replies if row.get("id") == 1)
        listed = next(row for row in replies if row.get("id") == 2)
        server_info = initialized["result"]["serverInfo"]
        tools = listed["result"]["tools"]
        if not isinstance(server_info.get("name"), str):
            raise ValueError
        return {str(row["name"]) for row in tools}
    except (KeyError, StopIteration, TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeError("mcp_entrypoint_self_test_failed") from exc


def _result() -> dict[str, Any]:
    contract = {
        "schema": "chatds.runtime-capabilities.v1",
        "structured_capabilities": ["renamed_holdout_lookup"],
        "public_http_read": {
            "enabled": True,
            "methods": ["GET", "HEAD"],
            "ports": [80, 443],
        },
    }
    prompt = render_runtime_capability_prompt(contract)
    if "renamed_holdout_lookup" not in prompt:
        raise RuntimeError("runtime_capability_self_test_failed")
    command, user_prompt = _claude_command({
        "native_session_id": "11111111-1111-4111-8111-111111111111",
        "resume_from_native_session_id": None,
        "api_model": "renamed-model-holdout",
        "context_window_tokens": 200_000,
        "max_output_tokens": 1024,
        "native_web_tools": False,
        "prompt": "entrypoint holdout",
        "runtime_capability_contract": contract,
    })
    if (
        command[:2] != ["/usr/local/bin/claude", "--print"]
        or "--append-system-prompt" not in command
        or user_prompt != b"entrypoint holdout"
    ):
        raise RuntimeError("controller_entrypoint_self_test_failed")
    for module, expected in _MCP_MODULES.items():
        if _mcp_tools(["-m", module]) != expected:
            raise RuntimeError("mcp_entrypoint_self_test_failed")
        if _mcp_tools([_MCP_COMPATIBILITY_PATHS[module]]) != expected:
            raise RuntimeError("mcp_entrypoint_self_test_failed")
    completed = subprocess.run(
        ["/usr/local/bin/claude", "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if (
        completed.returncode != 0
        or re.fullmatch(
            rb"[0-9]+\.[0-9]+\.[0-9]+ \(Claude Code\)\r?\n",
            completed.stdout,
        ) is None
    ):
        raise RuntimeError("native_binary_self_test_failed")
    return {
        "schema": SELF_TEST_SCHEMA,
        "status": "ok",
        "mcp_entrypoints": len(_MCP_MODULES),
        "compatibility_entrypoints": len(_MCP_COMPATIBILITY_PATHS),
    }


def main() -> int:
    try:
        result = _result()
    except Exception:
        print(json.dumps({
            "schema": SELF_TEST_SCHEMA,
            "status": "failed",
        }, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
