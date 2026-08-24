"""Native Claude hook boundary for compiled workflow and artifact contracts."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

from claude_runner.artifact_stop_hook import (
    _workspace_before,
    evaluate_stop_hook,
)
from claude_runner.native_workflow import (
    evaluate_workflow_hook,
    validate_workflow_contract,
)


MAX_HOOK_INPUT_BYTES = 2 * 1024 * 1024
MAX_CONTRACT_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
MAX_SYNTHESIS_BASELINE_BYTES = 96 * 1024 * 1024
MAX_SYNTHESIS_FINALS = 8_192
RECEIPT_PATH = Path("/runtime/workflow-receipt.json")
SYNTHESIS_BASELINE_PATH = Path(
    "/runtime/workflow-synthesis-baseline.json"
)


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
        "workflow_synthesis_baseline_path",
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
        or value.get("workflow_synthesis_baseline_path")
        not in {None, SYNTHESIS_BASELINE_PATH.as_posix()}
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
            or value["workflow_synthesis_baseline_path"]
            != (
                SYNTHESIS_BASELINE_PATH.as_posix()
                if value["artifact_contracts"]
                else None
            )
        ):
            raise ValueError("native_lifecycle_contract_invalid")
        value["workflow_contract"] = normalized
    elif (
        value["workflow_receipt_path"] is not None
        or value["workflow_synthesis_baseline_path"] is not None
    ):
        raise ValueError("native_lifecycle_contract_invalid")
    return value


def _synthesis_workspace_before(
    workflow: dict[str, Any],
    value: object,
) -> tuple[dict[str, tuple[int, ...]], dict[str, str]]:
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema",
            "skill_name",
            "route_id",
            "route_sha256",
            "workspace_before",
            "final_content_sha256",
        }
        or value.get("schema")
        != "chatds.workflow-synthesis-baseline.v1"
        or value.get("skill_name") != workflow["skill_name"]
        or value.get("route_id") != workflow["route_id"]
        or value.get("route_sha256") != workflow["route_sha256"]
    ):
        raise ValueError("workflow_synthesis_baseline_invalid")
    workspace_before = _workspace_before({
        "workspace_before": value["workspace_before"],
    })
    content = value.get("final_content_sha256")
    if (
        not isinstance(content, dict)
        or len(content) > MAX_SYNTHESIS_FINALS
        or any(
            not isinstance(path, str)
            or path not in workspace_before
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for path, digest in content.items()
        )
    ):
        raise ValueError("workflow_synthesis_baseline_invalid")
    return workspace_before, dict(content)


def _block_hook_input(hook_input: object, reason: str) -> dict[str, Any]:
    if (
        isinstance(hook_input, dict)
        and hook_input.get("hook_event_name") == "PreToolUse"
    ):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }
    return {"decision": "block", "reason": reason}


def evaluate_lifecycle_hook(
    *,
    hook_input: object,
    contract: dict[str, Any],
    workflow_receipt: object | None,
    workflow_synthesis_baseline: object | None = None,
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
    # Claude marks the one continuation created by a blocking Stop hook.
    # Preserve the existing bounded-correction contract: the authoritative
    # controller audit still fails closed after that continuation, while the
    # hook itself must not create an unbounded Stop loop.
    if (
        hook_input.get("hook_event_name") == "Stop"
        and hook_input.get("stop_hook_active") is True
    ):
        return {}
    artifact_contracts = contract.get("artifact_contracts")
    if not artifact_contracts:
        return {}
    if (
        hook_input.get("hook_event_name") == "PreToolUse"
        and (
            workflow is None
            or not isinstance(workflow_receipt, dict)
            or workflow_receipt.get("status") != "passed"
        )
    ):
        return {}
    artifact_workspace_before = contract["workspace_before"]
    artifact_content_before: dict[str, str] | None = None
    if workflow is not None:
        try:
            if (
                not isinstance(workflow_receipt, dict)
                or workflow_receipt.get("status") != "passed"
            ):
                return _block_hook_input(
                    hook_input,
                    "Machine workflow synthesis state is invalid.",
                )
            (
                artifact_workspace_before,
                artifact_content_before,
            ) = _synthesis_workspace_before(
                workflow,
                workflow_synthesis_baseline,
            )
        except (KeyError, TypeError, ValueError):
            return _block_hook_input(
                hook_input,
                "Machine workflow synthesis baseline is invalid.",
            )
    if hook_input.get("hook_event_name") == "PreToolUse":
        return {}
    return evaluate_stop_hook(
        hook_input=hook_input,
        contract={
            "schema": "chatds.artifact-stop-contract.v1",
            "skill_name": contract["skill_name"],
            "contracts": artifact_contracts,
            "workspace_before": artifact_workspace_before,
        },
        before_content_sha256=artifact_content_before,
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
        workflow_synthesis_baseline = None
        if contract["workflow_contract"] is not None:
            workflow_receipt = _load_root_owned_json(
                RECEIPT_PATH,
                max_bytes=MAX_RECEIPT_BYTES,
            )
            if (
                contract["workflow_synthesis_baseline_path"] is not None
                and isinstance(workflow_receipt, dict)
                and workflow_receipt.get("status") == "passed"
            ):
                workflow_synthesis_baseline = _load_root_owned_json(
                    SYNTHESIS_BASELINE_PATH,
                    max_bytes=MAX_SYNTHESIS_BASELINE_BYTES,
                )
        output = evaluate_lifecycle_hook(
            hook_input=hook_input,
            contract=contract,
            workflow_receipt=workflow_receipt,
            workflow_synthesis_baseline=workflow_synthesis_baseline,
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
