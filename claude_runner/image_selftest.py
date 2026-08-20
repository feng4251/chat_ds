"""Exact-entrypoint conformance test embedded in the Claude Turn image."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from claude_runner.artifact_stop_hook import evaluate_stop_hook
from claude_runner.native_lifecycle_hook import evaluate_lifecycle_hook
from claude_runner.native_workflow import build_workflow_receipt
from claude_runner.runner_entrypoint import _claude_command
from claude_runner.runtime_capabilities import (
    validate_runtime_capability_contract,
)


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
    if validate_runtime_capability_contract(contract) != contract:
        raise RuntimeError("runtime_capability_self_test_failed")
    runner_config = {
        "native_session_id": "11111111-1111-4111-8111-111111111111",
        "resume_from_native_session_id": None,
        "api_model": "renamed-model-holdout",
        "context_window_tokens": 200_000,
        "max_output_tokens": 1024,
        "native_web_tools": False,
        "prompt": "entrypoint holdout",
        "runtime_capability_contract": contract,
    }
    command, user_prompt = _claude_command(runner_config)
    if (
        command[:2] != ["/usr/local/bin/claude", "--print"]
        or command[command.index("--input-format") + 1] != "stream-json"
        or "--append-system-prompt" in command
        or json.loads(user_prompt)["message"]["content"]
        != [{"type": "text", "text": "entrypoint holdout"}]
    ):
        raise RuntimeError("controller_entrypoint_self_test_failed")
    hook_command, _ = _claude_command(
        runner_config,
        artifact_stop_contract_path=Path(
            "/runtime/artifact-stop-contract.json"
        ),
    )
    hook_settings = json.loads(
        hook_command[hook_command.index("--settings") + 1]
    )
    try:
        native_hook = hook_settings["hooks"]["Stop"][0]["hooks"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("artifact_stop_hook_self_test_failed") from exc
    if (
        native_hook.get("type") != "command"
        or "claude_runner.artifact_stop_hook"
        not in str(native_hook.get("command") or "")
    ):
        raise RuntimeError("artifact_stop_hook_self_test_failed")
    lifecycle_command, _ = _claude_command(
        runner_config,
        native_lifecycle_contract_path=Path(
            "/runtime/native-lifecycle-contract.json"
        ),
    )
    lifecycle_settings = json.loads(
        lifecycle_command[lifecycle_command.index("--settings") + 1]
    )
    try:
        lifecycle_hooks = lifecycle_settings["hooks"]
        pre_tool_hook = lifecycle_hooks["PreToolUse"][0]["hooks"][0]
        stop_hook = lifecycle_hooks["Stop"][0]["hooks"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("native_lifecycle_hook_self_test_failed") from exc
    if (
        pre_tool_hook != stop_hook
        or "claude_runner.native_lifecycle_hook"
        not in str(pre_tool_hook.get("command") or "")
    ):
        raise RuntimeError("native_lifecycle_hook_self_test_failed")
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        (workspace / "RENAMED_RECEIPT.md").write_text(
            "# Receipt\n", encoding="utf-8"
        )
        if evaluate_stop_hook(
            hook_input={
                "hook_event_name": "Stop",
                "stop_hook_active": False,
            },
            contract={
                "schema": "chatds.artifact-stop-contract.v1",
                "skill_name": "renamed-holdout",
                "workspace_before": {},
                "contracts": [{
                    "skill_name": "renamed-holdout",
                    "declared_final_artifact": "{NAME}_RECEIPT.md",
                    "declared_section_count": 1,
                }],
            },
            workspace_root=workspace,
        ) != {}:
            raise RuntimeError("artifact_stop_hook_self_test_failed")
        workflow = {
            "schema": "chatds.skill-workflow-contract.v1",
            "skill_name": "renamed-holdout",
            "route_id": "receipt_review",
            "route_sha256": "d" * 64,
            "phases": [{
                "mode": "sequential",
                "workers": [{
                    "worker_id": "reviewer",
                    "native_agent_type": (
                        "chatds-session-skills:renamed-holdout:reviewer"
                    ),
                }],
            }],
        }
        lifecycle_contract = {
            "schema": "chatds.native-lifecycle-contract.v1",
            "skill_name": "renamed-holdout",
            "artifact_contracts": [],
            "workspace_before": {},
            "workflow_contract": workflow,
            "workflow_receipt_path": "/runtime/workflow-receipt.json",
        }
        blocked = evaluate_lifecycle_hook(
            hook_input={
                "hook_event_name": "Stop",
                "stop_hook_active": False,
            },
            contract=lifecycle_contract,
            workflow_receipt=build_workflow_receipt(workflow, []),
            workspace_root=workspace,
        )
        if (
            blocked.get("decision") != "block"
            or "workflow_worker_missing" not in str(blocked.get("reason"))
        ):
            raise RuntimeError("native_lifecycle_hook_self_test_failed")
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
