"""Bounded native Claude Stop hook for immutable artifact contracts."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

from claude_runner.runner_entrypoint import (
    MAX_WORKSPACE_RELATIVE_PATH_BYTES,
    MAX_WORKSPACE_SNAPSHOT_FILES,
    _validate_artifact_contracts,
    _workspace_snapshot,
)


MAX_HOOK_INPUT_BYTES = 2 * 1024 * 1024
MAX_CONTRACT_BYTES = 4 * 1024 * 1024


def _load_contract(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("artifact_stop_contract_invalid")
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
        or info.st_size > MAX_CONTRACT_BYTES
        or info.st_mode & 0o022
    ):
        raise ValueError("artifact_stop_contract_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") != "chatds.artifact-stop-contract.v1"
        or set(value) != {
            "schema", "skill_name", "contracts", "workspace_before",
        }
        or not isinstance(value.get("skill_name"), str)
        or re.fullmatch(
            r"[A-Za-z0-9._-]{1,128}", value["skill_name"]
        ) is None
        or not isinstance(value.get("contracts"), list)
        or not isinstance(value.get("workspace_before"), dict)
    ):
        raise ValueError("artifact_stop_contract_invalid")
    return value


def _workspace_before(contract: dict[str, Any]) -> dict[str, tuple[int, ...]]:
    raw = contract["workspace_before"]
    if not isinstance(raw, dict) or len(raw) > MAX_WORKSPACE_SNAPSHOT_FILES:
        raise ValueError("artifact_stop_contract_invalid")
    baseline: dict[str, tuple[int, ...]] = {}
    for relative, identity in raw.items():
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or "\x00" in relative
            or len(relative.encode("utf-8"))
            > MAX_WORKSPACE_RELATIVE_PATH_BYTES
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(identity, (list, tuple))
            or len(identity) != 6
            or any(isinstance(item, bool) or not isinstance(item, int)
                   for item in identity)
        ):
            raise ValueError("artifact_stop_contract_invalid")
        baseline[relative] = tuple(identity)
    return baseline


def evaluate_stop_hook(
    *,
    hook_input: object,
    contract: dict[str, Any],
    before_content_sha256: dict[str, str] | None = None,
    workspace_root: Path,
) -> dict[str, Any]:
    """Return one native block decision, then defer to the terminal gate."""

    if (
        not isinstance(hook_input, dict)
        or hook_input.get("hook_event_name") != "Stop"
        or not isinstance(hook_input.get("stop_hook_active"), bool)
    ):
        return {
            "decision": "block",
            "reason": "Machine artifact audit could not validate Stop input.",
        }
    # Claude marks the continuation caused by a blocking Stop hook.  One
    # correction pass is deliberately bounded: the authoritative post-process
    # validator still fails closed if the second Stop remains incomplete.
    if hook_input["stop_hook_active"]:
        return {}
    receipt = _validate_artifact_contracts(
        contracts=contract["contracts"],
        invoked_skill_names=frozenset(),
        bound_skill_name=str(contract["skill_name"]),
        before=_workspace_before(contract),
        before_content_sha256=before_content_sha256,
        after=_workspace_snapshot(workspace_root),
        workspace_root=workspace_root,
    )
    if receipt.get("status") != "failed":
        return {}
    findings = receipt.get("findings")
    if not isinstance(findings, list):
        findings = []
    rendered = json.dumps(
        findings[:128],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(rendered) > 32_000:
        rendered = rendered[:32_000]
    return {
        "decision": "block",
        "reason": (
            "Machine artifact contract is incomplete. Continue the same "
            "task, preserve current valid files, repair every finding below, "
            "and rerun any declared merge and verification before finishing. "
            f"Findings: {rendered}"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(arguments) != 1:
            raise ValueError("artifact_stop_contract_invalid")
        raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
        if len(raw) > MAX_HOOK_INPUT_BYTES:
            raise ValueError("artifact_stop_input_invalid")
        hook_input = json.loads(raw)
        contract = _load_contract(Path(arguments[0]))
        output = evaluate_stop_hook(
            hook_input=hook_input,
            contract=contract,
            workspace_root=Path("/workspace"),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        output = {
            "decision": "block",
            "reason": "Machine artifact audit failed before completion.",
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
