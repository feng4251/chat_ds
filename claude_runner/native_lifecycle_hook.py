"""Native Claude hook boundary for compiled workflow and artifact contracts."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

from claude_runner.artifact_stop_hook import evaluate_stop_hook
from claude_runner.native_workflow import (
    evaluate_workflow_hook,
    validate_workflow_contract,
)


MAX_HOOK_INPUT_BYTES = 2 * 1024 * 1024
MAX_CONTRACT_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
RECEIPT_PATH = Path("/runtime/workflow-receipt.json")


def _load_root_owned_json(path: Path, *, max_bytes: int) -> object:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("native_lifecycle_state_invalid")
    parent_info = os.lstat(path.parent)
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or path.parent.is_symlink()
        or parent_info.st_uid != 0
        or parent_info.st_mode & 0o022
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != os.getegid()
        or info.st_size <= 0
        or info.st_size > max_bytes
        or info.st_mode & 0o022
    ):
        raise ValueError("native_lifecycle_state_invalid")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_contract(path: Path) -> dict[str, Any]:
    value = _load_root_owned_json(path, max_bytes=MAX_CONTRACT_BYTES)
    expected_keys = {
        "schema",
        "skill_name",
        "artifact_contracts",
        "workspace_before",
        "workflow_contract",
        "workflow_receipt_path",
    }
    if (
        not isinstance(value, dict)
        or value.get("schema") != "chatds.native-lifecycle-contract.v1"
        or set(value) != expected_keys
        or not isinstance(value.get("skill_name"), str)
        or re.fullmatch(
            r"[A-Za-z0-9._-]{1,128}", value["skill_name"]
        ) is None
        or not isinstance(value.get("artifact_contracts"), list)
        or not isinstance(value.get("workspace_before"), dict)
        or value.get("workflow_receipt_path")
        not in {None, RECEIPT_PATH.as_posix()}
        or (
            not value["artifact_contracts"]
            and value.get("workflow_contract") is None
        )
    ):
        raise ValueError("native_lifecycle_contract_invalid")
    workflow = value.get("workflow_contract")
    if workflow is not None:
        normalized = validate_workflow_contract(workflow)
        if (
            normalized["skill_name"] != value["skill_name"]
            or value["workflow_receipt_path"] != RECEIPT_PATH.as_posix()
        ):
            raise ValueError("native_lifecycle_contract_invalid")
        value["workflow_contract"] = normalized
    elif value["workflow_receipt_path"] is not None:
        raise ValueError("native_lifecycle_contract_invalid")
    return value


def evaluate_lifecycle_hook(
    *,
    hook_input: object,
    contract: dict[str, Any],
    workflow_receipt: object | None,
    workspace_root: Path,
) -> dict[str, Any]:
    """Apply the mandatory frontier before the artifact completion gate."""

    workflow = contract.get("workflow_contract")
    if workflow is not None:
        decision = evaluate_workflow_hook(
            hook_input=hook_input,
            contract=workflow,
            receipt=workflow_receipt,
            artifact_contracts=contract.get("artifact_contracts"),
        )
        if decision:
            return decision
    if not isinstance(hook_input, dict):
        return {
            "decision": "block",
            "reason": "Machine lifecycle hook input is invalid.",
        }
    if hook_input.get("hook_event_name") == "PreToolUse":
        return {}
    artifact_contracts = contract.get("artifact_contracts")
    if not artifact_contracts:
        return {}
    return evaluate_stop_hook(
        hook_input=hook_input,
        contract={
            "schema": "chatds.artifact-stop-contract.v1",
            "skill_name": contract["skill_name"],
            "contracts": artifact_contracts,
            "workspace_before": contract["workspace_before"],
        },
        workspace_root=workspace_root,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(arguments) != 1:
            raise ValueError("native_lifecycle_contract_invalid")
        raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
        if len(raw) > MAX_HOOK_INPUT_BYTES:
            raise ValueError("native_lifecycle_input_invalid")
        hook_input = json.loads(raw)
        contract = _load_contract(Path(arguments[0]))
        workflow_receipt = None
        if contract["workflow_contract"] is not None:
            workflow_receipt = _load_root_owned_json(
                RECEIPT_PATH,
                max_bytes=MAX_RECEIPT_BYTES,
            )
        output = evaluate_lifecycle_hook(
            hook_input=hook_input,
            contract=contract,
            workflow_receipt=workflow_receipt,
            workspace_root=Path("/workspace"),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        event_name = (
            hook_input.get("hook_event_name")
            if isinstance(locals().get("hook_input"), dict)
            else None
        )
        if event_name == "PreToolUse":
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "Machine lifecycle state failed validation."
                    ),
                },
            }
        else:
            output = {
                "decision": "block",
                "reason": "Machine lifecycle audit failed before completion.",
            }
    sys.stdout.write(json.dumps(
        output,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
