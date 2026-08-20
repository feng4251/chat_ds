"""Worker-side evaluator for the immutable native DSH artifact frontier."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from deepseek_runner.native_artifacts import (
    MAX_ARTIFACT_PROJECTION_BYTES,
    SAFE_SKILL_NAME,
    evaluate_deepseek_artifact_projection,
    validate_deepseek_artifact_projection,
)


MAX_REQUEST_BYTES = 256 * 1024
PROJECTION_PATH = Path("/runtime/controller/native-artifacts.json")


def _load_projection(path: Path) -> dict:
    if path != PROJECTION_PATH or path.is_symlink():
        raise ValueError("deepseek_artifact_gate_invalid")
    parent = os.lstat(path.parent)
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or path.parent.is_symlink()
        or parent.st_uid != 0
        or parent.st_gid != os.getegid()
        or stat.S_IMODE(parent.st_mode) != 0o750
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != os.getegid()
        or stat.S_IMODE(info.st_mode) != 0o440
        or info.st_size <= 0
        or info.st_size > MAX_ARTIFACT_PROJECTION_BYTES
    ):
        raise ValueError("deepseek_artifact_gate_invalid")
    try:
        value = json.loads(path.read_bytes())
        return validate_deepseek_artifact_projection(value)
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("deepseek_artifact_gate_invalid") from exc


def _request() -> tuple[str, ...]:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("deepseek_artifact_gate_invalid")
    try:
        value = json.loads(raw)
    except (UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("deepseek_artifact_gate_invalid") from exc
    if not isinstance(value, dict) or set(value) != {"active_skill_names"}:
        raise ValueError("deepseek_artifact_gate_invalid")
    names = value["active_skill_names"]
    if (
        not isinstance(names, list)
        or len(names) > 64
        or len(set(names)) != len(names)
        or any(
            not isinstance(name, str)
            or SAFE_SKILL_NAME.fullmatch(name) is None
            for name in names
        )
    ):
        raise ValueError("deepseek_artifact_gate_invalid")
    return tuple(names)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args != [PROJECTION_PATH.as_posix()]:
            raise ValueError("deepseek_artifact_gate_invalid")
        receipt = evaluate_deepseek_artifact_projection(
            projection=_load_projection(Path(args[0])),
            invoked_skill_names=_request(),
            workspace_root=Path("/workspace"),
        )
        encoded = json.dumps(
            receipt,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return 70
    sys.stdout.write(encoded + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
