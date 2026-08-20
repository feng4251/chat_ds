"""Engine-neutral workspace snapshots and immutable Skill artifact receipts."""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any


MAX_WORKSPACE_FILES = 200_000
MAX_RELATIVE_PATH_BYTES = 1024
MAX_ARTIFACT_CONTRACTS = 64
MAX_FINDINGS = 256
MAX_DECLARED_FILES = 256
SAFE_SKILL_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")


def workspace_snapshot(
    workspace_root: Path,
    *,
    max_files: int = MAX_WORKSPACE_FILES,
) -> dict[str, tuple[int, ...]]:
    """Capture kernel-owned identities for every regular workspace file."""

    root_info = os.lstat(workspace_root)
    if not stat.S_ISDIR(root_info.st_mode) or workspace_root.is_symlink():
        raise RuntimeError("workspace_artifact_root_invalid")
    result: dict[str, tuple[int, ...]] = {}
    pending = [workspace_root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                path = Path(entry.path)
                relative = path.relative_to(workspace_root).as_posix()
                if (
                    not relative
                    or len(relative.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES
                ):
                    raise RuntimeError("workspace_artifact_path_invalid")
                if stat.S_ISLNK(info.st_mode):
                    raise RuntimeError("workspace_artifact_symlink_invalid")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise RuntimeError("workspace_artifact_type_invalid")
                result[relative] = (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
                if len(result) > max_files:
                    raise RuntimeError("workspace_artifact_file_limit")
    return result


def workspace_contract_pattern(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeError("artifact_contract_invalid")
    pattern = value.strip().replace("\\", "/")
    if (
        not pattern
        or pattern.startswith("/")
        or "\x00" in pattern
        or len(pattern.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES
        or any(part in {"", ".", ".."} for part in pattern.split("/"))
    ):
        raise RuntimeError("artifact_contract_invalid")
    pattern = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]{0,63}\}", "*", pattern)
    if "{" in pattern or "}" in pattern:
        raise RuntimeError("artifact_contract_invalid")
    return pattern


def _artifact_text_stats(path: Path) -> tuple[int, int]:
    line_count = 0
    heading_count = 0
    prefix = bytearray()
    current_has_bytes = False

    def finish_line() -> None:
        nonlocal line_count, heading_count, prefix, current_has_bytes
        line_count += 1
        if re.match(rb" {0,3}#{1,2}[ \t]+\S", bytes(prefix)):
            heading_count += 1
        prefix = bytearray()
        current_has_bytes = False

    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            parts = chunk.split(b"\n")
            for index, part in enumerate(parts):
                if part:
                    current_has_bytes = True
                    if len(prefix) < 1024:
                        prefix.extend(part[:1024 - len(prefix)])
                if index < len(parts) - 1:
                    finish_line()
    if current_has_bytes:
        finish_line()
    return line_count, heading_count


def _bounded_nonnegative_integer(
    row: dict[str, Any], field: str
) -> int | None:
    value = row.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("artifact_contract_invalid")
    return value


def validate_artifact_contracts(
    *,
    contracts: object,
    active_skill_name: str | None,
    before: dict[str, tuple[int, ...]],
    after: dict[str, tuple[int, ...]],
    workspace_root: Path,
    active_skill_names: object = None,
) -> dict[str, Any]:
    """Validate only the immutable artifact contract activated this Turn."""

    if contracts is None:
        rows: list[object] = []
    elif isinstance(contracts, list) and len(contracts) <= MAX_ARTIFACT_CONTRACTS:
        rows = contracts
    else:
        raise RuntimeError("artifact_contract_invalid")
    activated: set[str] = set()
    if active_skill_name is not None:
        if (
            not isinstance(active_skill_name, str)
            or SAFE_SKILL_NAME.fullmatch(active_skill_name) is None
        ):
            raise RuntimeError("artifact_contract_invalid")
        activated.add(active_skill_name)
    if active_skill_names is not None:
        if (
            not isinstance(active_skill_names, (list, tuple, set, frozenset))
            or len(active_skill_names) > MAX_ARTIFACT_CONTRACTS
        ):
            raise RuntimeError("artifact_contract_invalid")
        for value in active_skill_names:
            if (
                not isinstance(value, str)
                or SAFE_SKILL_NAME.fullmatch(value) is None
            ):
                raise RuntimeError("artifact_contract_invalid")
            activated.add(value)
    active: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("artifact_contract_invalid")
        skill_name = row.get("skill_name")
        if (
            not isinstance(skill_name, str)
            or SAFE_SKILL_NAME.fullmatch(skill_name) is None
        ):
            raise RuntimeError("artifact_contract_invalid")
        if skill_name in activated:
            active.append(row)
    if not active:
        return {
            "schema": "chatds.native-artifact-receipt.v1",
            "status": "not_applicable",
            "activated_contract_count": 0,
            "finding_count": 0,
            "findings": [],
            "validated": [],
        }

    changed = {
        path for path, identity in after.items() if before.get(path) != identity
    }
    findings: list[dict[str, Any]] = []
    validated: list[dict[str, Any]] = []

    def finding(code: str, **values: Any) -> None:
        if len(findings) < MAX_FINDINGS:
            findings.append({"code": code, **values})

    for row in active:
        skill_name = str(row["skill_name"])
        final_pattern = workspace_contract_pattern(
            row.get("declared_final_artifact")
        )
        matches = sorted(
            path for path in after if fnmatch.fnmatchcase(path, final_pattern)
        )
        changed_matches = [path for path in matches if path in changed]
        if not matches:
            finding(
                "artifact_final_missing",
                skill_name=skill_name,
                pattern=final_pattern,
            )
            continue
        if not changed_matches:
            finding(
                "artifact_final_not_committed_this_turn",
                skill_name=skill_name,
                pattern=final_pattern,
            )
            continue
        if len(changed_matches) != 1:
            finding(
                "artifact_final_ambiguous",
                skill_name=skill_name,
                pattern=final_pattern,
                actual=len(changed_matches),
            )
            continue
        relative = changed_matches[0]
        identity = after[relative]
        if len(identity) != 6:
            raise RuntimeError("artifact_contract_invalid")
        size_bytes = int(identity[3])
        minimum = _bounded_nonnegative_integer(row, "expected_min_bytes")
        maximum = _bounded_nonnegative_integer(row, "expected_max_bytes")
        if minimum is not None and size_bytes < minimum:
            finding(
                "artifact_min_bytes_not_met",
                skill_name=skill_name,
                path=relative,
                actual=size_bytes,
                expected=minimum,
            )
        if maximum and size_bytes > maximum:
            finding(
                "artifact_max_bytes_exceeded",
                skill_name=skill_name,
                path=relative,
                actual=size_bytes,
                expected=maximum,
            )

        min_lines = _bounded_nonnegative_integer(row, "expected_min_lines")
        max_lines = _bounded_nonnegative_integer(row, "expected_max_lines")
        min_headings = _bounded_nonnegative_integer(row, "declared_section_count")
        line_count: int | None = None
        heading_count: int | None = None
        if min_lines is not None or max_lines is not None or min_headings is not None:
            path = workspace_root / relative
            try:
                info = os.lstat(path)
                if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                    raise RuntimeError("artifact_contract_audit_failed")
                line_count, heading_count = _artifact_text_stats(path)
                current = os.lstat(path)
            except OSError as exc:
                raise RuntimeError("artifact_contract_audit_failed") from exc
            current_identity = (
                current.st_dev,
                current.st_ino,
                current.st_mode,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
            )
            if current_identity != identity:
                raise RuntimeError("artifact_contract_audit_failed")
            if min_lines is not None and line_count < min_lines:
                finding(
                    "artifact_min_lines_not_met",
                    skill_name=skill_name,
                    path=relative,
                    actual=line_count,
                    expected=min_lines,
                )
            if max_lines and line_count > max_lines:
                finding(
                    "artifact_max_lines_exceeded",
                    skill_name=skill_name,
                    path=relative,
                    actual=line_count,
                    expected=max_lines,
                )
            if min_headings and heading_count < min_headings:
                finding(
                    "artifact_declared_sections_not_met",
                    skill_name=skill_name,
                    path=relative,
                    actual=heading_count,
                    expected=min_headings,
                )

        final_parent = PurePosixPath(relative).parent
        for field in ("declared_modular_files", "declared_ancillary_files"):
            declared_files = row.get(field) or []
            if (
                not isinstance(declared_files, list)
                or len(declared_files) > MAX_DECLARED_FILES
            ):
                raise RuntimeError("artifact_contract_invalid")
            for declared in declared_files:
                declared_pattern = workspace_contract_pattern(declared)
                resolved_pattern = (
                    declared_pattern
                    if str(final_parent) == "."
                    else (final_parent / declared_pattern).as_posix()
                )
                if not any(
                    fnmatch.fnmatchcase(path, resolved_pattern)
                    for path in after
                ):
                    finding(
                        "artifact_declared_module_missing"
                        if field == "declared_modular_files"
                        else "artifact_declared_ancillary_missing",
                        skill_name=skill_name,
                        pattern=declared_pattern,
                        final_parent=(
                            "" if str(final_parent) == "." else str(final_parent)
                        ),
                    )
        validated.append({
            "skill_name": skill_name,
            "path": relative,
            "size_bytes": size_bytes,
            "line_count": line_count,
            "heading_count": heading_count,
        })
    return {
        "schema": "chatds.native-artifact-receipt.v1",
        "status": "failed" if findings else "passed",
        "activated_contract_count": len(active),
        "finding_count": len(findings),
        "findings": findings,
        "validated": validated,
    }
