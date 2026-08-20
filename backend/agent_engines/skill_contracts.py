"""Bounded, domain-neutral execution contracts for Claude Skill views."""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml


MAX_CONTRACT_YAML_FILES = 64
MAX_CONTRACT_YAML_BYTES = 8 * 1024 * 1024
MAX_SKILL_INSTRUCTION_BYTES = 8 * 1024 * 1024
MAX_CONTRACT_ITEMS = 128
MAX_CONTRACT_DEPTH = 64
MAX_SKILL_WORKERS = 128
MAX_SKILL_WORKER_BYTES = 2 * 1024 * 1024
MAX_SKILL_WORKER_DESCRIPTION_BYTES = 8 * 1024
MAX_SKILL_WORKFLOW_ROUTES = 128
MAX_SKILL_WORKFLOW_PATTERNS = 128
MAX_SKILL_WORKFLOW_PATTERN_BYTES = 8 * 1024


_WORKER_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
)
_WORKFLOW_ROUTE_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
)


class SkillContractError(RuntimeError):
    pass


def compile_skill_workers(
    *,
    skill_name: str,
    root: Path,
    relative_files: Iterable[PurePosixPath],
) -> list[dict[str, str]]:
    """Compile conventional structured workers for a native agent surface.

    A worker file remains the authoritative instruction source. This compiler
    extracts only the bounded identity and routing description required to
    expose it as an engine-native subagent; it does not interpret or execute
    worker instructions itself.
    """

    candidates = sorted(
        (
            relative
            for relative in relative_files
            if _is_structured_worker_yaml(relative)
        ),
        key=lambda value: (value.as_posix().casefold(), value.as_posix()),
    )
    if len(candidates) > MAX_SKILL_WORKERS:
        raise SkillContractError("skill_worker_file_limit")

    workers: list[dict[str, str]] = []
    identities: set[str] = set()
    for relative in candidates:
        path = root / relative
        try:
            if path.stat().st_size > MAX_SKILL_WORKER_BYTES:
                raise SkillContractError("skill_worker_file_size_limit")
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except SkillContractError:
            raise
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SkillContractError("skill_worker_yaml_invalid") from exc
        if not isinstance(document, dict):
            raise SkillContractError("skill_worker_document_invalid")
        _validate_yaml_shape(document)

        raw_identity = (
            document.get("worker_id")
            if document.get("worker_id") is not None
            else document.get("id")
        )
        if (
            not isinstance(raw_identity, str)
            or _WORKER_ID_PATTERN.fullmatch(raw_identity) is None
        ):
            raise SkillContractError("skill_worker_identity_invalid")
        folded_identity = raw_identity.casefold()
        if folded_identity in identities:
            raise SkillContractError("skill_worker_identity_duplicate")
        identities.add(folded_identity)

        raw_description = document.get("description")
        if raw_description is None:
            raw_description = document.get("name")
        workers.append({
            "skill_name": skill_name,
            "worker_id": raw_identity,
            "description": _normalize_worker_description(raw_description),
            "source_path": relative.as_posix(),
        })
    return workers


def compile_skill_workflows(
    *,
    skill_name: str,
    root: Path,
    relative_files: Iterable[PurePosixPath],
    workers: Iterable[dict[str, str]],
) -> list[dict[str, Any]]:
    """Compile declarative worker routing into ordered mandatory phases.

    The compiler interprets only the conventional routing vocabulary needed
    to preserve parallel barriers and sequential dependencies. Worker prompts
    remain in their immutable YAML files and execution remains engine-native.
    """

    worker_ids = {
        str(worker.get("worker_id") or "")
        for worker in workers
        if isinstance(worker, dict)
    }
    candidates = sorted(
        (
            relative
            for relative in relative_files
            if _is_contract_yaml(relative)
        ),
        key=lambda value: (value.as_posix().casefold(), value.as_posix()),
    )
    if len(candidates) > MAX_CONTRACT_YAML_FILES:
        raise SkillContractError("skill_contract_file_limit")

    routes: list[dict[str, Any]] = []
    identities: set[str] = set()
    for relative in candidates:
        path = root / relative
        try:
            if path.stat().st_size > MAX_CONTRACT_YAML_BYTES:
                raise SkillContractError("skill_contract_file_size_limit")
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except SkillContractError:
            raise
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SkillContractError("skill_workflow_yaml_invalid") from exc
        if not isinstance(document, dict):
            continue
        _validate_yaml_shape(document)
        raw_routes = document.get("routing_rules")
        if raw_routes is None:
            continue
        if not isinstance(raw_routes, dict):
            raise SkillContractError("skill_workflow_routes_invalid")
        for route_id, raw_route in raw_routes.items():
            if len(routes) >= MAX_SKILL_WORKFLOW_ROUTES:
                raise SkillContractError("skill_workflow_route_limit")
            if (
                not isinstance(route_id, str)
                or _WORKFLOW_ROUTE_ID_PATTERN.fullmatch(route_id) is None
            ):
                raise SkillContractError("skill_workflow_route_identity_invalid")
            folded_route_id = route_id.casefold()
            if folded_route_id in identities:
                raise SkillContractError("skill_workflow_route_identity_duplicate")
            identities.add(folded_route_id)
            if not isinstance(raw_route, dict):
                raise SkillContractError("skill_workflow_route_invalid")

            patterns = _normalize_workflow_patterns(raw_route.get("patterns"))
            priority = raw_route.get("priority", 0)
            if (
                isinstance(priority, bool)
                or not isinstance(priority, int)
                or not -100_000 <= priority <= 100_000
            ):
                raise SkillContractError("skill_workflow_priority_invalid")

            direct_worker = raw_route.get("worker")
            listed_workers = raw_route.get("workers")
            if direct_worker is not None:
                if listed_workers is not None:
                    raise SkillContractError("skill_workflow_workers_invalid")
                initial_workers = _normalize_workflow_worker_ids(
                    [direct_worker], worker_ids=worker_ids
                )
                default_mode = "direct"
            else:
                initial_workers = _normalize_workflow_worker_ids(
                    listed_workers, worker_ids=worker_ids
                )
                default_mode = (
                    "direct" if len(initial_workers) == 1 else "parallel"
                )
            spawn_mode = raw_route.get("spawn_mode", default_mode)
            if spawn_mode not in {"direct", "parallel", "sequential"}:
                raise SkillContractError("skill_workflow_spawn_mode_invalid")
            if spawn_mode == "direct" and len(initial_workers) != 1:
                raise SkillContractError("skill_workflow_spawn_mode_invalid")

            sequential_workers = _normalize_workflow_worker_ids(
                raw_route.get("sequential_workers", []),
                worker_ids=worker_ids,
                allow_empty=True,
            )
            ordered_workers = initial_workers + sequential_workers
            if len(set(ordered_workers)) != len(ordered_workers):
                raise SkillContractError("skill_workflow_worker_duplicate")

            phases: list[dict[str, Any]] = []
            if spawn_mode == "parallel":
                phases.append({
                    "mode": "parallel",
                    "worker_ids": initial_workers,
                })
            else:
                phases.extend(
                    {"mode": "sequential", "worker_ids": [worker_id]}
                    for worker_id in initial_workers
                )
            phases.extend(
                {"mode": "sequential", "worker_ids": [worker_id]}
                for worker_id in sequential_workers
            )
            routes.append({
                "skill_name": skill_name,
                "route_id": route_id,
                "source_path": relative.as_posix(),
                "priority": priority,
                "patterns": patterns,
                "phases": phases,
            })
    return routes


def _normalize_workflow_patterns(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_SKILL_WORKFLOW_PATTERNS
    ):
        raise SkillContractError("skill_workflow_patterns_invalid")
    patterns: list[str] = []
    for pattern in value:
        if (
            not isinstance(pattern, str)
            or not pattern
            or len(pattern.encode("utf-8")) > MAX_SKILL_WORKFLOW_PATTERN_BYTES
            or any(ord(character) < 0x20 or ord(character) == 0x7F
                   for character in pattern)
        ):
            raise SkillContractError("skill_workflow_pattern_invalid")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SkillContractError("skill_workflow_pattern_invalid") from exc
        patterns.append(pattern)
    return patterns


def _normalize_workflow_worker_ids(
    value: object,
    *,
    worker_ids: set[str],
    allow_empty: bool = False,
) -> list[str]:
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or len(value) > MAX_SKILL_WORKERS
    ):
        raise SkillContractError("skill_workflow_workers_invalid")
    normalized: list[str] = []
    for worker_id in value:
        if (
            not isinstance(worker_id, str)
            or _WORKER_ID_PATTERN.fullmatch(worker_id) is None
        ):
            raise SkillContractError("skill_workflow_worker_identity_invalid")
        if worker_id not in worker_ids:
            raise SkillContractError("skill_workflow_worker_unknown")
        normalized.append(worker_id)
    return normalized


def compile_skill_contract(
    *,
    skill_name: str,
    root: Path,
    relative_files: Iterable[PurePosixPath],
    primary: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any], list[dict[str, str]]]:
    """Compile artifact/runtime declarations without business literals.

    Structured ``output_contract`` is authoritative.  The legacy
    ``final_report_template.auto_merge`` shape remains a bounded compatibility
    input because portable Skills already use it to declare final artifact,
    size and post-merge checks.
    """

    files = tuple(relative_files)
    skill_md = next(
        (path for path in files if path.as_posix() == "SKILL.md"),
        None,
    )
    instructions = ""
    if skill_md is not None:
        try:
            instruction_path = root / skill_md
            if instruction_path.stat().st_size > MAX_SKILL_INSTRUCTION_BYTES:
                raise SkillContractError("skill_contract_instruction_size_limit")
            instructions = instruction_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except SkillContractError:
            raise
        except OSError as exc:
            raise SkillContractError("skill_contract_source_unavailable") from exc
    requirements = _runtime_requirements(instructions)
    diagnostics = _instruction_diagnostics(skill_name, instructions)
    if not primary:
        return None, requirements, diagnostics

    candidates = [
        relative
        for relative in files
        if _is_contract_yaml(relative)
    ]
    if len(candidates) > MAX_CONTRACT_YAML_FILES:
        raise SkillContractError("skill_contract_file_limit")
    structured_contract: dict[str, Any] = {}
    compatible_contract: dict[str, Any] = {}
    for relative in candidates:
        path = root / relative
        try:
            if path.stat().st_size > MAX_CONTRACT_YAML_BYTES:
                raise SkillContractError("skill_contract_file_size_limit")
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except SkillContractError:
            raise
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SkillContractError("skill_contract_yaml_invalid") from exc
        if not isinstance(document, dict):
            continue
        _validate_yaml_shape(document)
        structured = document.get("output_contract")
        if structured is not None:
            normalized = _normalize_output_contract(structured)
            _merge_contract(
                structured_contract, normalized, authoritative=True
            )
        template = document.get("final_report_template")
        if isinstance(template, dict):
            compatible = _legacy_template_contract(template)
            _merge_contract(
                compatible_contract, compatible, authoritative=True
            )
    contract = {**compatible_contract, **structured_contract}
    if not contract.get("declared_final_artifact"):
        return None, requirements, diagnostics
    _validate_compiled_contract(contract)
    return {"skill_name": skill_name, **contract}, requirements, diagnostics


def _validate_compiled_contract(contract: dict[str, Any]) -> None:
    for minimum_name, maximum_name in (
        ("expected_min_bytes", "expected_max_bytes"),
        ("expected_min_lines", "expected_max_lines"),
    ):
        minimum = contract.get(minimum_name)
        maximum = contract.get(maximum_name)
        if (
            isinstance(minimum, int)
            and isinstance(maximum, int)
            and maximum > 0
            and minimum > maximum
        ):
            raise SkillContractError("skill_output_contract_range_invalid")


def _is_contract_yaml(relative: PurePosixPath) -> bool:
    if relative.suffix.casefold() not in {".yaml", ".yml"}:
        return False
    parts = relative.parts
    if len(parts) == 2 and parts[0].casefold() in {
        "orchestration", "workflows", "orchestrator",
    }:
        return True
    return (
        len(parts) == 1
        and relative.stem.casefold().startswith("orchestrat")
    )


def _is_structured_worker_yaml(relative: PurePosixPath) -> bool:
    parts = relative.parts
    return (
        len(parts) == 3
        and parts[0].casefold() == "orchestration"
        and parts[1].casefold() == "workers"
        and relative.suffix.casefold() in {".yaml", ".yml"}
    )


def _normalize_worker_description(value: object) -> str:
    if not isinstance(value, str):
        raise SkillContractError("skill_worker_description_invalid")
    description = " ".join(value.split())
    if (
        not description
        or len(description.encode("utf-8"))
        > MAX_SKILL_WORKER_DESCRIPTION_BYTES
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in description
        )
    ):
        raise SkillContractError("skill_worker_description_invalid")
    return description


def _validate_yaml_shape(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    visited: set[int] = set()
    count = 0
    while stack:
        item, depth = stack.pop()
        if depth > MAX_CONTRACT_DEPTH:
            raise SkillContractError("skill_contract_yaml_depth_limit")
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in visited:
                continue
            visited.add(identity)
            count += len(item)
            if count > 100_000:
                raise SkillContractError("skill_contract_yaml_item_limit")
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise SkillContractError("skill_contract_yaml_key_invalid")
                stack.extend((child, depth + 1) for child in item.values())
            else:
                stack.extend((child, depth + 1) for child in item)


def _normalize_output_contract(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillContractError("skill_output_contract_invalid")
    result: dict[str, Any] = {}
    final = next((
        value.get(name)
        for name in (
            "declared_final_artifact", "final_artifact", "output_artifact",
        )
        if value.get(name) is not None
    ), None)
    if final is not None:
        result["declared_final_artifact"] = _safe_artifact_pattern(final)
    for field in (
        "declared_modular_files", "declared_ancillary_files",
    ):
        raw = value.get(field)
        if raw is None:
            continue
        if not isinstance(raw, list) or len(raw) > MAX_CONTRACT_ITEMS:
            raise SkillContractError("skill_output_contract_invalid")
        result[field] = [_safe_artifact_pattern(item) for item in raw]
    for field in (
        "expected_min_bytes", "expected_max_bytes",
        "expected_min_lines", "expected_max_lines",
    ):
        raw = value.get(field)
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise SkillContractError("skill_output_contract_invalid")
        result[field] = raw
    return result


def _legacy_template_contract(template: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    sections = template.get("sections")
    if isinstance(sections, list) and len(sections) <= MAX_CONTRACT_ITEMS:
        titles = [
            str(item.get("section")).strip()
            for item in sections
            if isinstance(item, dict) and str(item.get("section") or "").strip()
        ]
        if titles:
            result["declared_section_count"] = len(titles)
            result["section_titles"] = titles
    auto_merge = template.get("auto_merge")
    if not isinstance(auto_merge, dict):
        return result
    output = auto_merge.get("output_artifact")
    if output is not None:
        result["declared_final_artifact"] = _safe_artifact_pattern(output)
    command = auto_merge.get("command_template") or auto_merge.get("command")
    merge_inputs = _declared_merge_inputs(command)
    if merge_inputs:
        result["declared_modular_files"] = merge_inputs
    size_range = auto_merge.get("expected_size_range")
    if isinstance(size_range, str) and "-" in size_range:
        low, high = size_range.split("-", 1)
        low_bytes = _parse_bytes(low)
        high_bytes = _parse_bytes(high)
        if low_bytes is not None:
            result["expected_min_bytes"] = low_bytes
        if high_bytes is not None:
            result["expected_max_bytes"] = high_bytes
    checks = auto_merge.get("post_merge_verification")
    if isinstance(checks, list) and len(checks) <= MAX_CONTRACT_ITEMS:
        for raw in checks:
            check = str(raw)
            line_match = re.search(
                r"(?:line\s*count\s*>\s*([0-9][0-9,]*)"
                r"|>\s*([0-9][0-9,]*)\s*lines?\b)",
                check,
                re.IGNORECASE,
            )
            if line_match:
                value = int(
                    (line_match.group(1) or line_match.group(2)).replace(",", "")
                )
                result["expected_min_lines"] = max(
                    int(result.get("expected_min_lines") or 0), value
                )
            size_match = re.search(
                r"(?:file\s*size\s*)?>\s*([0-9]+(?:\.[0-9]+)?\s*[KMGT]?B)",
                check,
                re.IGNORECASE,
            )
            if size_match:
                parsed = _parse_bytes(size_match.group(1))
                if parsed is not None:
                    result["expected_min_bytes"] = max(
                        int(result.get("expected_min_bytes") or 0), parsed
                    )
    return result


def _declared_merge_inputs(value: object) -> list[str]:
    """Extract only inert Markdown glob operands from a declared cat merge."""

    if not isinstance(value, str) or not value or len(value) > 64 * 1024:
        return []
    try:
        tokens = shlex.split(value.replace("\\\n", " "), posix=True)
    except ValueError:
        return []
    try:
        start = tokens.index("cat") + 1
    except ValueError:
        return []
    result: list[str] = []
    for token in tokens[start:]:
        if token in {">", ">>", "|", "&&", ";"} or token.startswith(">"):
            break
        if token.startswith("-") or not token.casefold().endswith(".md"):
            continue
        try:
            pattern = _safe_artifact_pattern(token)
        except SkillContractError:
            continue
        if pattern not in result:
            result.append(pattern)
        if len(result) >= MAX_CONTRACT_ITEMS:
            break
    return result


def _merge_contract(
    target: dict[str, Any],
    incoming: dict[str, Any],
    *,
    authoritative: bool,
) -> None:
    for key, value in incoming.items():
        if key not in target:
            target[key] = value
        elif authoritative and target[key] != value:
            raise SkillContractError("skill_output_contract_conflict")


def _parse_bytes(value: str) -> int | None:
    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?B)\s*",
        value,
        re.IGNORECASE,
    )
    if match is None:
        return None
    multiplier = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4,
    }[match.group(2).upper()]
    result = int(float(match.group(1)) * multiplier)
    return result if 0 <= result <= 8 * 1024 ** 3 else None


def _safe_artifact_pattern(value: object) -> str:
    if not isinstance(value, str):
        raise SkillContractError("skill_output_contract_path_invalid")
    path = value.strip().replace("\\", "/")
    placeholder_stripped = re.sub(
        r"\{[A-Za-z_][A-Za-z0-9_]{0,63}\}", "", path
    )
    if (
        not path
        or path.startswith("/")
        or "\x00" in path
        or len(path.encode("utf-8")) > 1024
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or "{" in placeholder_stripped
        or "}" in placeholder_stripped
    ):
        raise SkillContractError("skill_output_contract_path_invalid")
    return path


def _runtime_requirements(text: str) -> dict[str, Any]:
    lowered = text.casefold()
    stdin = bool(re.search(r"\bstdin\b|standard input|标准输入", lowered))
    persistent = bool(re.search(
        r"\bpersistent\b|long[- ]running|keep.{0,40}alive|持久|持续.{0,20}会话",
        lowered,
    ))
    return {
        "persistent_stdin_process": stdin and persistent,
    }


def _instruction_diagnostics(
    skill_name: str,
    text: str,
) -> list[dict[str, str]]:
    lowered = text.casefold()
    permits_evasion = bool(re.search(
        r"(?:allow|may|can|must|should|支持|允许|必须|可).{0,100}"
        r"(?:bypass|captcha|anti[- ]?bot|access control|绕过|验证码|反爬)",
        lowered,
    )) or bool(re.search(
        r"(?:bypass|captcha|anti[- ]?bot|access control|绕过|验证码|反爬)"
        r".{0,100}(?:allow|may|can|must|should|支持|允许|必须|可)",
        lowered,
    ))
    forbids_concealment = bool(re.search(
        r"(?:never|do not|don't|must not|forbid|prohibit|禁止|不得|不要)"
        r".{0,100}(?:stealth|conceal|webdriver|automation signal|隐藏|伪装)",
        lowered,
    ))
    if permits_evasion and forbids_concealment:
        return [{
            "code": "contradictory_automation_evasion_policy",
            "skill_name": skill_name,
        }]
    return []
