"""Runtime lowering for declarative worker knowledge gates.

The Skill loader owns syntax and package-path validation.  This module performs
the second, run-scoped compilation pass after the Harness has frozen the
available native/MCP tools and the exact Skill resource/script/HTTP/command
grants.  Model-authored text never creates a candidate.

The resulting plan is deliberately small:

* checks select a branch through one typed decision receipt;
* branches reference one or more candidate groups (groups are AND);
* every group is ``one_of`` (candidate IDs inside the group are OR);
* candidates carry exact backend-issued coordinates and are revalidated again
  by the delegated execution boundary.

Legacy ``checks[].tools`` is compiled by the loader into one conditional
``one_of`` group.  Skills that need stronger semantics can declare multiple
explicit groups without changing the runtime representation.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from knowledge_gate import (
    MAX_GATE_IDENTIFIER_CHARS,
    is_canonical_knowledge_gate_identifier,
)


KNOWLEDGE_GATE_PLAN_SCHEMA_VERSION = 1
KNOWLEDGE_GATE_DECISION_RECEIPT_SCHEMA_VERSION = 1
UNCONDITIONAL_CAPABILITY_PLAN_SCHEMA_VERSION = 1
KNOWLEDGE_GATE_DECISION_TOOL_NAME = "submit_knowledge_gate_decisions"
MAX_KNOWLEDGE_GATE_PLAN_BYTES = 512 * 1024
MAX_UNCONDITIONAL_CAPABILITY_PLAN_BYTES = 512 * 1024
MAX_GATE_CHECKS = 128
MAX_GATE_GROUPS = 256
MAX_GATE_CANDIDATES = 512
MAX_GATE_SELECTORS_PER_GROUP = 64
MAX_UNCONDITIONAL_CAPABILITY_SELECTORS = 256
MAX_UNCONDITIONAL_CAPABILITY_SELECTOR_CHARS = 4_096
MAX_GATE_TEXT_CHARS = 8_000
MAX_GATE_DECISION_REASON_CHARS = 1_000
_RESOURCE_SUFFIXES = frozenset({
    ".csv", ".html", ".json", ".md", ".pdf", ".tsv", ".txt", ".xml",
    ".yaml", ".yml",
})
_NON_ACTION_SKILL_TOOLS = frozenset({
    "skills_list",
    "skill_view",
    "skill_copy_resource",
    "submit_skill_capability_plan",
    KNOWLEDGE_GATE_DECISION_TOOL_NAME,
})
_EXACT_GRANT_BRIDGES = frozenset({
    "run_skill_python",
    "run_skill_script",
    "run_skill_process",
    "run_declared_command",
    "skill_http_get",
    "skill_http_post_json",
})


class KnowledgeGateCompileError(ValueError):
    """Stable fail-closed error raised for malformed compiler-owned input."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json_sha256(value: Any) -> str:
    """Return one deterministic digest for finite JSON metadata."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise KnowledgeGateCompileError(
            "knowledge_gate_noncanonical_json",
            "Knowledge-gate metadata must contain only finite, acyclic JSON values.",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _stable_id(kind: str, coordinates: Any) -> str:
    digest = canonical_json_sha256({"kind": kind, "coordinates": coordinates})[:24]
    identifier = f"gate-{kind}-{digest}"
    if not is_canonical_knowledge_gate_identifier(identifier):
        raise KnowledgeGateCompileError(
            "knowledge_gate_generated_id_invalid",
            "The runtime compiler produced a non-canonical group identifier.",
        )
    return identifier


def _candidate_id(kind: str, coordinates: Any) -> str:
    digest = canonical_json_sha256({"kind": kind, "coordinates": coordinates})[:24]
    identifier = f"{kind}-{digest}"
    if not is_canonical_knowledge_gate_identifier(identifier):
        raise KnowledgeGateCompileError(
            "knowledge_gate_generated_id_invalid",
            "The runtime compiler produced a non-canonical candidate identifier.",
        )
    return identifier


def candidate_tool_names(candidate: dict[str, Any]) -> list[str]:
    names: list[str] = []
    if candidate.get("kind") == "skill_resource":
        # Resource bindings follow the existing exact-capability schema: the
        # path/digest tuple is authority and ``skill_view`` is the fixed
        # transport implied by the kind, not a model/compiler-authored field.
        names.append("skill_view")
    tool_name = candidate.get("tool_name")
    if isinstance(tool_name, str) and tool_name:
        names.append(tool_name)
    raw = candidate.get("tool_names")
    if isinstance(raw, list):
        names.extend(
            str(name)
            for name in raw
            if isinstance(name, str) and name
        )
    return list(dict.fromkeys(names))


def ordinary_worker_capability_selectors(
    worker: dict[str, Any] | None,
    *,
    available_tools: Iterable[str],
    resolve_tool_selector: Callable[[str, Iterable[str]], list[str]],
) -> list[str]:
    """Return canonical selectors for one normalized worker's static surface.

    Input fields are the loader-owned ordinary declarations only:
    ``tools``, ``capabilities``, ``skills``, ``local_resources``, and
    ``environment_contract.allowed_tools``.  Knowledge-gate fields are
    deliberately never traversed.  ``skill:<name>`` means the exact runnable
    and declared HTTP/command/native adapters in that frozen package; concrete
    relative resource paths remain path selectors and are content-addressed by
    the runtime compiler.
    """

    if not isinstance(worker, dict):
        return []
    available = {str(name) for name in available_tools if str(name)}
    selectors: list[str] = []

    def add(candidate: Any, *, field: str = "") -> None:
        text = str(candidate or "").strip()
        if not text:
            return
        if field in {"skill", "skills"}:
            name = (
                text.split(":", 1)[1].strip()
                if text.casefold().startswith("skill:")
                else text
            )
            if name:
                selectors.append(f"skill:{name}")
            return
        if text.casefold().startswith("skill:"):
            selectors.append("skill:" + text.split(":", 1)[1].strip())
            return
        selectors.append(text)

    def source_selectors(value: Any) -> list[str]:
        text = str(value or "").strip()
        folded = text.casefold()
        if (
            not text
            or folded in {
                "builtin", "built-in", "harness", "internal", "project",
                "local", "workspace", "file", "resource",
                "available_skills",
            }
        ):
            return []
        if folded.startswith("skill:"):
            return [text]
        selector_pattern = r"[A-Za-z][A-Za-z0-9_.-]*(?:\([^)]*\))?"
        candidates: list[str] = []
        if folded.startswith("tool:"):
            candidates.append(text.split(":", 1)[1].strip())
        elif re.fullmatch(selector_pattern, text):
            candidates.append(text)
        else:
            tool_match = re.fullmatch(
                rf"(?i)({selector_pattern})\s+tool",
                text,
            )
            via_match = re.fullmatch(r"(?i)via\s+(.+)", text)
            if tool_match:
                candidates.append(tool_match.group(1))
            elif via_match:
                candidates.extend(
                    part
                    for part in re.split(
                        r"\s*(?:/|,|\||\bor\b)\s*",
                        via_match.group(1),
                        flags=re.IGNORECASE,
                    )[:8]
                    if re.fullmatch(selector_pattern, part)
                )
        # Preserve every independently resolvable spelling.  A source such as
        # ``via Bash/WebFetch`` may contain an intentionally unresolved command
        # family plus one exact supported fetch adapter; unresolved values are
        # omitted only when another spelling carries the declared capability.
        resolved = [
            candidate
            for candidate in dict.fromkeys(candidates)
            if resolve_tool_selector(candidate, available)
        ]
        return resolved or list(dict.fromkeys(candidates[:1]))

    path_fields = {
        "path", "paths", "file", "files", "resource", "resources",
        "local_resources",
    }
    native_fields = {
        "tool", "tools", "capability", "capabilities", "allowed_tools",
        "selector", "tool_selector",
    }

    def collect(value: Any, *, field: str) -> None:
        if isinstance(value, str):
            if field == "source":
                for selector in source_selectors(value):
                    add(selector)
            else:
                add(value, field=field)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item, field=field)
            return
        if not isinstance(value, dict):
            return
        semantic_keys = (
            native_fields
            | path_fields
            | {"skill", "skills", "source", "name", "id"}
        )
        recognized_descriptor = bool(semantic_keys.intersection(value))
        for key in (
            "tool", "tools", "capability", "capabilities", "selector",
            "tool_selector", "source", "path", "paths", "file", "files",
            "resource", "resources", "local_resources", "skill", "skills",
        ):
            if key in value:
                collect(value.get(key), field=key)
        if field in native_fields and not recognized_descriptor:
            for selector, enabled in value.items():
                if enabled in (False, None):
                    continue
                add(selector)
        elif field in {"skill", "skills"} and not recognized_descriptor:
            for skill_name, enabled in value.items():
                if enabled in (False, None):
                    continue
                add(skill_name, field="skills")

    collect(worker.get("tools"), field="tools")
    collect(worker.get("capabilities"), field="capabilities")
    collect(worker.get("skills"), field="skills")
    collect(worker.get("local_resources"), field="local_resources")
    environment = worker.get("environment_contract")
    if isinstance(environment, dict):
        collect(environment.get("allowed_tools"), field="allowed_tools")
    # Do not silently drop an unfamiliar declaration.  The exact compiler
    # either resolves every returned selector against parent-owned authority
    # or fails closed with the original selector in its diagnostic.
    return list(dict.fromkeys(
        selector for selector in selectors if selector
    ))


def _execution_contract(loaded: dict[str, Any]) -> dict[str, Any]:
    workflow = loaded.get("workflow_contract")
    if not isinstance(workflow, dict):
        return {}
    execution = workflow.get("execution_contract")
    return execution if isinstance(execution, dict) else {}


def _declared_package_tool_selectors(loaded: dict[str, Any]) -> list[str]:
    execution = _execution_contract(loaded)
    environment = execution.get("environment_contract")
    if not isinstance(environment, dict):
        return []
    return list(dict.fromkeys(
        str(value).strip()
        for value in environment.get("allowed_tools") or []
        if isinstance(value, str) and value.strip()
    ))


def _resource_candidate(
    *,
    owner_skill: str,
    resource_path: str,
    loaded_packages: dict[str, dict[str, Any]],
    allowed_resources: set[tuple[str, str]],
    allowed_package_digests: set[tuple[str, str]],
) -> dict[str, Any] | None:
    if (owner_skill, resource_path) not in allowed_resources:
        return None
    loaded = loaded_packages.get(owner_skill)
    skill_dir = loaded.get("skill_dir") if isinstance(loaded, dict) else None
    loaded_main_digest = (
        str(loaded.get("skill_md_sha256") or "")
        if isinstance(loaded, dict)
        else ""
    )
    if (
        not isinstance(skill_dir, str)
        or not skill_dir
        or not loaded_main_digest
    ):
        return None
    try:
        from skills.path_safety import validate_skill_resource
        from tools.isolated_skill_executor import snapshot_skill_package

        root = Path(skill_dir).resolve(strict=True)
        snapshot = snapshot_skill_package(root)
        if snapshot.file_sha256("SKILL.md") != loaded_main_digest:
            return None
        if (owner_skill, snapshot.sha256) not in allowed_package_digests:
            return None
        checked = validate_skill_resource(
            root,
            resource_path,
            expected_kind="file",
            require_relative=True,
        )
        if not checked.valid or checked.path is None:
            return None
        digest = hashlib.sha256(checked.path.read_bytes()).hexdigest()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    coordinates = {
        "skill_name": owner_skill,
        "skill_md_sha256": loaded_main_digest,
        "package_sha256": snapshot.sha256,
        "resource_path": resource_path,
        "sha256": digest,
    }
    return {
        "candidate_id": _candidate_id("resource", coordinates),
        "kind": "skill_resource",
        "tool_names": [],
        **coordinates,
    }


def _mcp_candidate(
    name: str,
    *,
    frozen_mcp_catalog: Any,
) -> dict[str, Any] | None:
    descriptor = (
        frozen_mcp_catalog.get(name)
        if frozen_mcp_catalog is not None
        and hasattr(frozen_mcp_catalog, "get")
        else None
    )
    if descriptor is None:
        return None
    coordinates = {
        "tool_name": name,
        "schema_sha256": str(descriptor.schema_sha256),
        "descriptor_sha256": str(descriptor.descriptor_sha256),
    }
    return {
        "candidate_id": _candidate_id("mcp", coordinates),
        "kind": "mcp_tool",
        "tool_names": [name],
        **coordinates,
    }


def _native_candidates(
    selector: str,
    *,
    available_tools: set[str],
    resolve_tool_selector: Callable[[str, Iterable[str]], list[str]],
    frozen_mcp_catalog: Any,
    browser_egress_rules: Iterable[
        tuple[str, tuple[str, ...]]
    ] = (),
) -> list[dict[str, Any]]:
    from tools.session_sandbox_policy import browser_egress_rule_tuples

    exact_browser_rules = browser_egress_rule_tuples(
        browser_egress_rules
    )
    resolved = resolve_tool_selector(selector, sorted(available_tools))
    candidates: list[dict[str, Any]] = []
    for name in resolved:
        if (
            name not in available_tools
            or name in _NON_ACTION_SKILL_TOOLS
            or name in _EXACT_GRANT_BRIDGES
        ):
            continue
        if name.startswith("mcp_"):
            mcp = _mcp_candidate(name, frozen_mcp_catalog=frozen_mcp_catalog)
            if mcp is not None:
                candidates.append(mcp)
            continue
        if name == "browser_navigate" and not exact_browser_rules:
            continue
        coordinates: dict[str, Any] = {"tool_name": name}
        if name == "browser_navigate":
            coordinates["browser_egress_rules"] = [
                {
                    "methods": list(methods),
                    "url_prefix": prefix,
                }
                for prefix, methods in exact_browser_rules
            ]
        candidates.append({
            "candidate_id": _candidate_id("native", coordinates),
            "kind": "native_tool",
            "tool_name": name,
            "tool_names": [name],
        })
    return candidates


def _narrowed_command_candidate(
    selector: str,
    *,
    owner_skill: str,
    worker_id: str,
    available_tools: set[str],
    loaded_packages: dict[str, dict[str, Any]],
    allowed_package_digests: set[tuple[str, str]],
    allowed_commands: set[tuple[str, str, str, tuple[str, ...]]],
    allowed_sandbox_egress: set[tuple[str, str]],
    allowed_sandbox_egress_rules: set[
        tuple[str, str, tuple[str, ...]]
    ],
) -> dict[str, Any] | None:
    """Bind a safe Bash/Shell selector to its exact worker-scoped grant."""

    if "run_declared_command" not in available_tools:
        return None
    try:
        from skills.command_grants import (
            command_grant_from_selector,
            grant_tuple,
            scope_command_grant,
        )
        from tools.isolated_skill_executor import snapshot_skill_package

        unscoped = command_grant_from_selector(selector)
        scoped = (
            scope_command_grant(unscoped, f"worker:{worker_id}")
            if unscoped is not None
            else None
        )
        if scoped is None:
            return None
        exact_grant = grant_tuple(owner_skill, scoped)
        if exact_grant not in allowed_commands:
            return None
        loaded = loaded_packages.get(owner_skill)
        if not isinstance(loaded, dict) or loaded.get("error"):
            return None
        skill_dir = str(loaded.get("skill_dir") or "")
        skill_md_sha256 = str(loaded.get("skill_md_sha256") or "")
        if not skill_dir or not skill_md_sha256:
            return None
        snapshot = snapshot_skill_package(Path(skill_dir))
        if (
            snapshot.file_sha256("SKILL.md") != skill_md_sha256
            or (owner_skill, snapshot.sha256)
            not in allowed_package_digests
        ):
            return None
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None
    coordinates = {
        "skill_name": owner_skill,
        "skill_md_sha256": skill_md_sha256,
        "package_sha256": snapshot.sha256,
        "command_id": str(scoped["id"]),
        "executable": str(scoped["executable"]),
        "fixed_argv": [
            str(item) for item in scoped.get("argv_prefix") or []
        ],
        "additional_argv": True,
        "tool_name": "run_declared_command",
        "tool_names": ["run_declared_command"],
        "sandbox_egress_url_prefixes": [
            prefix
            for granted_skill, prefix in sorted(
                allowed_sandbox_egress
            )
            if granted_skill == owner_skill
        ],
        "sandbox_egress_rules": [
            {
                "methods": list(methods),
                "url_prefix": prefix,
            }
            for granted_skill, prefix, methods in sorted(
                allowed_sandbox_egress_rules
            )
            if granted_skill == owner_skill
        ],
    }
    return {
        "candidate_id": _candidate_id("command", coordinates),
        "kind": "declared_command",
        **coordinates,
    }


def _skill_candidates(
    skill_name: str,
    *,
    available_tools: set[str],
    loaded_packages: dict[str, dict[str, Any]],
    allowed_scripts: set[tuple[str, str, str]],
    process_only_scripts: set[tuple[str, str, str]],
    allowed_package_digests: set[tuple[str, str]],
    allowed_commands: set[tuple[str, str, str, tuple[str, ...]]],
    allowed_http_get: set[tuple[str, str]],
    allowed_http_post: set[tuple[str, str]],
    allowed_sandbox_egress: set[tuple[str, str]],
    allowed_sandbox_egress_rules: set[
        tuple[str, str, tuple[str, ...]]
    ],
    resolve_tool_selector: Callable[[str, Iterable[str]], list[str]],
    frozen_mcp_catalog: Any,
    browser_egress_rules: Iterable[
        tuple[str, tuple[str, ...]]
    ] = (),
) -> list[dict[str, Any]]:
    loaded = loaded_packages.get(skill_name)
    if not isinstance(loaded, dict) or loaded.get("error"):
        return []
    skill_dir = loaded.get("skill_dir")
    loaded_main_digest = str(loaded.get("skill_md_sha256") or "")
    if not isinstance(skill_dir, str) or not skill_dir or not loaded_main_digest:
        return []
    try:
        from tools.isolated_skill_executor import snapshot_skill_package

        snapshot = snapshot_skill_package(Path(skill_dir))
        if snapshot.file_sha256("SKILL.md") != loaded_main_digest:
            return []
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return []
    skill_identity = {
        "skill_name": skill_name,
        "skill_md_sha256": loaded_main_digest,
        "package_sha256": snapshot.sha256,
    }
    package_identity_authorized = (
        skill_name,
        snapshot.sha256,
    ) in allowed_package_digests
    if not package_identity_authorized:
        return []
    candidates: list[dict[str, Any]] = []
    sandbox_egress_url_prefixes = [
        prefix
        for granted_skill, prefix in sorted(allowed_sandbox_egress)
        if granted_skill == skill_name
    ]
    sandbox_egress_rules = [
        {
            "methods": list(methods),
            "url_prefix": prefix,
        }
        for granted_skill, prefix, methods in sorted(
            allowed_sandbox_egress_rules
        )
        if granted_skill == skill_name
    ]
    for granted_skill, path, digest in sorted(allowed_scripts):
        if granted_skill != skill_name:
            continue
        grant = (granted_skill, path, digest)
        tool_names: list[str] = []
        if grant in process_only_scripts:
            if "run_skill_process" in available_tools:
                tool_names.append("run_skill_process")
        else:
            if "run_skill_script" in available_tools:
                tool_names.append("run_skill_script")
            if (
                PurePosixPath(path).suffix.casefold() == ".py"
                and "run_skill_python" in available_tools
            ):
                tool_names.append("run_skill_python")
        if not tool_names:
            continue
        coordinates: dict[str, Any] = {
            **skill_identity,
            "resource_path": path,
            "sha256": digest,
            "tool_names": tool_names,
            "sandbox_egress_url_prefixes": (
                sandbox_egress_url_prefixes
            ),
            "sandbox_egress_rules": sandbox_egress_rules,
        }
        candidates.append({
            "candidate_id": _candidate_id("script", coordinates),
            "kind": "skill_script",
            **coordinates,
        })
    for granted_skill, command_id, executable, fixed_argv in sorted(
        allowed_commands
    ):
        if (
            granted_skill != skill_name
            or "run_declared_command" not in available_tools
        ):
            continue
        coordinates = {
            **skill_identity,
            "command_id": command_id,
            "executable": executable,
            "fixed_argv": list(fixed_argv),
            "additional_argv": True,
            "sandbox_egress_url_prefixes": (
                sandbox_egress_url_prefixes
            ),
            "sandbox_egress_rules": sandbox_egress_rules,
        }
        candidates.append({
            "candidate_id": _candidate_id("command", coordinates),
            "kind": "declared_command",
            "tool_name": "run_declared_command",
            "tool_names": ["run_declared_command"],
            **coordinates,
        })
    for granted_skill, prefix in sorted(allowed_http_get):
        if granted_skill != skill_name or "skill_http_get" not in available_tools:
            continue
        coordinates = {
            **skill_identity,
            "url_prefix": prefix,
            "http_method": "GET",
            "tool_name": "skill_http_get",
        }
        candidates.append({
            "candidate_id": _candidate_id("http", coordinates),
            "kind": "skill_http_prefix",
            "tool_names": ["skill_http_get"],
            **coordinates,
        })
    for granted_skill, prefix in sorted(allowed_http_post):
        if (
            granted_skill != skill_name
            or "skill_http_post_json" not in available_tools
        ):
            continue
        coordinates = {
            **skill_identity,
            "url_prefix": prefix,
            "http_method": "POST JSON",
            "tool_name": "skill_http_post_json",
        }
        candidates.append({
            "candidate_id": _candidate_id("http", coordinates),
            "kind": "skill_http_prefix",
            "tool_names": ["skill_http_post_json"],
            **coordinates,
        })

    # Package-declared native/MCP selectors may provide a legitimate adapter,
    # but an ambient tool never becomes a fallback merely because the package
    # has no executable route.
    for selector in _declared_package_tool_selectors(loaded):
        if "(" in selector:
            # Argument-bearing declarations are not aliases. Exact command
            # grants were already bound above; all other wrapper policies
            # remain unresolved instead of being widened to a bare tool.
            continue
        for candidate in _native_candidates(
            selector,
            available_tools=available_tools,
            resolve_tool_selector=resolve_tool_selector,
            frozen_mcp_catalog=frozen_mcp_catalog,
            browser_egress_rules=browser_egress_rules,
        ):
            coordinates = {
                key: value
                for key, value in candidate.items()
                if key != "candidate_id"
            }
            coordinates.update(skill_identity)
            candidates.append({
                **coordinates,
                "candidate_id": _candidate_id(
                    "skill-" + str(candidate.get("kind") or "adapter"),
                    coordinates,
                ),
            })
    return candidates


def _runtime_candidates_for_selector(
    selector: str,
    *,
    owner_skill: str,
    worker_id: str,
    available_tools: set[str],
    loaded_packages: dict[str, dict[str, Any]],
    allowed_resources: set[tuple[str, str]],
    allowed_scripts: set[tuple[str, str, str]],
    process_only_scripts: set[tuple[str, str, str]],
    allowed_package_digests: set[tuple[str, str]],
    allowed_commands: set[tuple[str, str, str, tuple[str, ...]]],
    allowed_http_get: set[tuple[str, str]],
    allowed_http_post: set[tuple[str, str]],
    allowed_sandbox_egress: set[tuple[str, str]],
    allowed_sandbox_egress_rules: set[
        tuple[str, str, tuple[str, ...]]
    ],
    frozen_mcp_catalog: Any,
    browser_egress_rules: Iterable[
        tuple[str, tuple[str, ...]]
    ] = (),
    resolve_tool_selector: Callable[[str, Iterable[str]], list[str]],
    resource_expansions: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve one loader-owned selector to exact run-owned candidates.

    Both unconditional worker capabilities and conditional knowledge-gate
    branches use this single lowering boundary.  Keeping one resolver prevents
    aliases, MCP descriptor pinning, and Skill script/HTTP/command coordinates
    from drifting between the two authority classes.
    """

    if selector.casefold().startswith("skill:"):
        name = selector.split(":", 1)[1].strip()
        return _skill_candidates(
            name,
            available_tools=available_tools,
            loaded_packages=loaded_packages,
            allowed_scripts=allowed_scripts,
            process_only_scripts=process_only_scripts,
            allowed_package_digests=allowed_package_digests,
            allowed_commands=allowed_commands,
            allowed_http_get=allowed_http_get,
            allowed_http_post=allowed_http_post,
            allowed_sandbox_egress=allowed_sandbox_egress,
            allowed_sandbox_egress_rules=(
                allowed_sandbox_egress_rules
            ),
            resolve_tool_selector=resolve_tool_selector,
            frozen_mcp_catalog=frozen_mcp_catalog,
            browser_egress_rules=browser_egress_rules,
        )
    if "(" in selector:
        command = _narrowed_command_candidate(
            selector,
            owner_skill=owner_skill,
            worker_id=worker_id,
            available_tools=available_tools,
            loaded_packages=loaded_packages,
            allowed_package_digests=allowed_package_digests,
            allowed_commands=allowed_commands,
            allowed_sandbox_egress=allowed_sandbox_egress,
            allowed_sandbox_egress_rules=(
                allowed_sandbox_egress_rules
            ),
        )
        return [command] if command is not None else []
    expanded_resources = (resource_expansions or {}).get(selector)
    if expanded_resources is not None:
        return [
            candidate
            for resource_path in expanded_resources
            for candidate in [
                _resource_candidate(
                    owner_skill=owner_skill,
                    resource_path=resource_path,
                    loaded_packages=loaded_packages,
                    allowed_resources=allowed_resources,
                    allowed_package_digests=allowed_package_digests,
                )
            ]
            if candidate is not None
        ]
    suffix = PurePosixPath(selector).suffix.casefold()
    if suffix in {".py", ".sh", ".bash", ".js", ".mjs", ".cjs"}:
        exact_scripts = [
            candidate
            for candidate in _skill_candidates(
                owner_skill,
                available_tools=available_tools,
                loaded_packages=loaded_packages,
                allowed_scripts=allowed_scripts,
                process_only_scripts=process_only_scripts,
                allowed_package_digests=allowed_package_digests,
                allowed_commands=allowed_commands,
                allowed_http_get=allowed_http_get,
                allowed_http_post=allowed_http_post,
                allowed_sandbox_egress=allowed_sandbox_egress,
                allowed_sandbox_egress_rules=(
                    allowed_sandbox_egress_rules
                ),
                resolve_tool_selector=resolve_tool_selector,
                frozen_mcp_catalog=frozen_mcp_catalog,
                browser_egress_rules=browser_egress_rules,
            )
            if (
                candidate.get("kind") == "skill_script"
                and candidate.get("resource_path") == selector
            )
        ]
        if exact_scripts:
            return exact_scripts
    if "/" in selector or suffix in _RESOURCE_SUFFIXES:
        candidate = _resource_candidate(
            owner_skill=owner_skill,
            resource_path=selector,
            loaded_packages=loaded_packages,
            allowed_resources=allowed_resources,
            allowed_package_digests=allowed_package_digests,
        )
        return [candidate] if candidate is not None else []
    return _native_candidates(
        selector,
        available_tools=available_tools,
        resolve_tool_selector=resolve_tool_selector,
        frozen_mcp_catalog=frozen_mcp_catalog,
        browser_egress_rules=browser_egress_rules,
    )


def compile_runtime_unconditional_capability_plan(
    selectors: Iterable[str],
    *,
    worker_id: str,
    owner_skill: str,
    available_tools: Iterable[str],
    loaded_packages: dict[str, dict[str, Any]],
    allowed_resources: Iterable[tuple[str, str]],
    allowed_scripts: Iterable[tuple[str, str, str]],
    process_only_scripts: Iterable[tuple[str, str, str]],
    allowed_package_digests: Iterable[tuple[str, str]],
    allowed_commands: Iterable[tuple[str, str, str, tuple[str, ...]]],
    allowed_http_get: Iterable[tuple[str, str]],
    allowed_http_post: Iterable[tuple[str, str]],
    allowed_sandbox_egress: Iterable[tuple[str, str]],
    frozen_mcp_catalog: Any,
    resolve_tool_selector: Callable[[str, Iterable[str]], list[str]],
    allowed_sandbox_egress_rules: Iterable[
        tuple[str, str, tuple[str, ...]]
    ] = (),
    allowed_browser_egress_rules: Iterable[
        tuple[str, tuple[str, ...]]
    ] = (),
) -> tuple[dict[str, Any] | None, str | None]:
    """Compile ordinary worker declarations into exact optional authority.

    This plan intentionally carries no receipt semantics.  A candidate is
    available to the delegated node, but it is not mandatory merely because
    the Skill declared it.  Explicit required/mandatory markers remain a
    separate result-contract concern.
    """

    raw_selectors = list(selectors)
    if not raw_selectors:
        return None, None
    if (
        len(raw_selectors) > MAX_UNCONDITIONAL_CAPABILITY_SELECTORS
        or any(
            not isinstance(selector, str)
            or not selector
            or selector != selector.strip()
            or len(selector)
            > MAX_UNCONDITIONAL_CAPABILITY_SELECTOR_CHARS
            or any(char in selector for char in "\r\n\x00")
            for selector in raw_selectors
        )
    ):
        raise KnowledgeGateCompileError(
            "unconditional_capability_selector_invalid",
            "Unconditional capability selectors must be one bounded list of "
            "non-empty canonical strings.",
        )
    if not is_canonical_knowledge_gate_identifier(worker_id):
        raise KnowledgeGateCompileError(
            "unconditional_capability_worker_id_invalid",
            "The unconditional capability worker ID is not one canonical "
            f"bounded identifier of at most {MAX_GATE_IDENTIFIER_CHARS} "
            "characters.",
        )
    if not isinstance(owner_skill, str) or not owner_skill.strip():
        raise KnowledgeGateCompileError(
            "unconditional_capability_owner_invalid",
            "An unconditional capability plan requires one owning Skill.",
        )

    normalized_selectors = list(dict.fromkeys(raw_selectors))
    available = {str(name) for name in available_tools if str(name)}
    resource_grants = set(allowed_resources)
    script_grants = set(allowed_scripts)
    process_grants = set(process_only_scripts)
    package_grants = set(allowed_package_digests)
    command_grants = set(allowed_commands)
    http_get_grants = set(allowed_http_get)
    http_post_grants = set(allowed_http_post)
    sandbox_egress_grants = set(allowed_sandbox_egress)
    sandbox_egress_rule_grants = set(allowed_sandbox_egress_rules)
    candidate_by_id: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []

    def is_instruction_only_skill_selector(selector: str) -> bool:
        if not selector.casefold().startswith("skill:"):
            return False
        name = selector.split(":", 1)[1].strip()
        loaded = loaded_packages.get(name)
        if not isinstance(loaded, dict) or loaded.get("error"):
            return False
        skill_dir = loaded.get("skill_dir")
        main_digest = str(loaded.get("skill_md_sha256") or "")
        if (
            not isinstance(skill_dir, str)
            or not skill_dir
            or not main_digest
            or (name, "SKILL.md") not in resource_grants
        ):
            return False
        if (
            any(skill == name for skill, _path, _digest in script_grants)
            or any(skill == name for skill, *_rest in command_grants)
            or any(skill == name for skill, _prefix in http_get_grants)
            or any(skill == name for skill, _prefix in http_post_grants)
            or _declared_package_tool_selectors(loaded)
        ):
            return False
        try:
            from tools.isolated_skill_executor import snapshot_skill_package

            snapshot = snapshot_skill_package(Path(skill_dir))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return False
        return bool(
            snapshot.file_sha256("SKILL.md") == main_digest
            and (name, snapshot.sha256) in package_grants
        )

    for selector in normalized_selectors:
        resolved = _runtime_candidates_for_selector(
            selector,
            owner_skill=owner_skill,
            worker_id=worker_id,
            available_tools=available,
            loaded_packages=loaded_packages,
            allowed_resources=resource_grants,
            allowed_scripts=script_grants,
            process_only_scripts=process_grants,
            allowed_package_digests=package_grants,
            allowed_commands=command_grants,
            allowed_http_get=http_get_grants,
            allowed_http_post=http_post_grants,
            allowed_sandbox_egress=sandbox_egress_grants,
            allowed_sandbox_egress_rules=sandbox_egress_rule_grants,
            frozen_mcp_catalog=frozen_mcp_catalog,
            resolve_tool_selector=resolve_tool_selector,
            browser_egress_rules=allowed_browser_egress_rules,
        )
        if not resolved:
            if is_instruction_only_skill_selector(selector):
                continue
            unresolved.append(selector)
            continue
        for candidate in resolved:
            candidate_by_id.setdefault(str(candidate["candidate_id"]), candidate)
    if unresolved:
        raise KnowledgeGateCompileError(
            "unconditional_capability_selector_unresolved",
            "Ordinary worker capability selectors did not resolve to exact "
            "run-owned authority: " + ", ".join(unresolved[:32]),
        )
    candidates = sorted(
        candidate_by_id.values(),
        key=lambda item: str(item.get("candidate_id") or ""),
    )
    if not candidates:
        # A declared supporting Skill may be instruction-only. Its exact
        # SKILL.md preload remains in required_capability_skills, but it does
        # not create a callable static authority plan or a false receipt
        # obligation.
        return None, None
    if len(candidates) > MAX_GATE_CANDIDATES:
        raise KnowledgeGateCompileError(
            "unconditional_capability_candidate_limit",
            "An unconditional capability plan may contain at most "
            f"{MAX_GATE_CANDIDATES} candidates.",
        )
    plan = {
        "schema_version": UNCONDITIONAL_CAPABILITY_PLAN_SCHEMA_VERSION,
        "worker_id": worker_id,
        "owner_skill": owner_skill,
        "selectors": normalized_selectors,
        "candidates": candidates,
    }
    encoded_plan = json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded_plan) > MAX_UNCONDITIONAL_CAPABILITY_PLAN_BYTES:
        raise KnowledgeGateCompileError(
            "unconditional_capability_plan_size_limit",
            "The frozen unconditional capability plan exceeds the bounded "
            f"canonical size of {MAX_UNCONDITIONAL_CAPABILITY_PLAN_BYTES} "
            "bytes.",
        )
    return plan, canonical_json_sha256(plan)


def compile_runtime_knowledge_gate_plan(
    symbolic_ir: dict[str, Any] | None,
    *,
    worker_id: str,
    owner_skill: str,
    available_tools: Iterable[str],
    loaded_packages: dict[str, dict[str, Any]],
    allowed_resources: Iterable[tuple[str, str]],
    allowed_scripts: Iterable[tuple[str, str, str]],
    process_only_scripts: Iterable[tuple[str, str, str]],
    allowed_package_digests: Iterable[tuple[str, str]],
    allowed_commands: Iterable[tuple[str, str, str, tuple[str, ...]]],
    allowed_http_get: Iterable[tuple[str, str]],
    allowed_http_post: Iterable[tuple[str, str]],
    allowed_sandbox_egress: Iterable[tuple[str, str]],
    frozen_mcp_catalog: Any,
    resolve_tool_selector: Callable[[str, Iterable[str]], list[str]],
    allowed_sandbox_egress_rules: Iterable[
        tuple[str, str, tuple[str, ...]]
    ] = (),
    allowed_browser_egress_rules: Iterable[
        tuple[str, tuple[str, ...]]
    ] = (),
) -> tuple[dict[str, Any] | None, str | None]:
    """Lower one loader-owned symbolic gate IR into a frozen exact plan."""

    if not isinstance(symbolic_ir, dict) or not symbolic_ir.get("checks"):
        return None, None
    if symbolic_ir.get("schema_version") != 1:
        raise KnowledgeGateCompileError(
            "knowledge_gate_symbolic_schema_unsupported",
            "Unsupported symbolic knowledge-gate schema version.",
        )
    if symbolic_ir.get("valid") is not True:
        raise KnowledgeGateCompileError(
            "knowledge_gate_symbolic_contract_invalid",
            "The loader-owned symbolic knowledge-gate contract is invalid.",
        )
    if not is_canonical_knowledge_gate_identifier(worker_id):
        raise KnowledgeGateCompileError(
            "knowledge_gate_worker_id_invalid",
            "The knowledge-gate worker ID is not one canonical bounded "
            f"identifier of at most {MAX_GATE_IDENTIFIER_CHARS} characters.",
        )
    supplied_symbolic_digest = symbolic_ir.get("ir_sha256")
    symbolic_digest_projection = {
        key: value
        for key, value in symbolic_ir.items()
        if key not in {
            "diagnostic_summary",
            "ir_sha256",
            "valid",
        }
    }
    if (
        not isinstance(supplied_symbolic_digest, str)
        or supplied_symbolic_digest
        != canonical_json_sha256(symbolic_digest_projection)
    ):
        raise KnowledgeGateCompileError(
            "knowledge_gate_symbolic_identity_mismatch",
            "The loader-owned symbolic knowledge-gate digest does not match.",
        )
    raw_checks = symbolic_ir.get("checks")
    if not isinstance(raw_checks, list) or len(raw_checks) > MAX_GATE_CHECKS:
        raise KnowledgeGateCompileError(
            "knowledge_gate_check_limit",
            f"Knowledge gate may contain at most {MAX_GATE_CHECKS} checks.",
        )

    available = {str(name) for name in available_tools if str(name)}
    resource_grants = set(allowed_resources)
    script_grants = set(allowed_scripts)
    process_grants = set(process_only_scripts)
    package_grants = set(allowed_package_digests)
    command_grants = set(allowed_commands)
    http_get_grants = set(allowed_http_get)
    http_post_grants = set(allowed_http_post)
    sandbox_egress_grants = set(allowed_sandbox_egress)
    sandbox_egress_rule_grants = set(allowed_sandbox_egress_rules)
    candidate_by_id: dict[str, dict[str, Any]] = {}
    groups: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    resource_expansions = {
        str(expansion.get("selector") or ""): [
            str(path)
            for path in expansion.get("resources") or []
            if isinstance(path, str) and path
        ]
        for expansion in symbolic_ir.get("resource_expansions") or []
        if isinstance(expansion, dict)
        and str(expansion.get("selector") or "")
    }

    for check_index, raw_check in enumerate(raw_checks):
        if not isinstance(raw_check, dict):
            raise KnowledgeGateCompileError(
                "knowledge_gate_check_invalid",
                "Every symbolic knowledge-gate check must be an object.",
            )
        check_id = str(raw_check.get("id") or "")
        branches = raw_check.get("branches")
        question = raw_check.get("question")
        if (
            not is_canonical_knowledge_gate_identifier(check_id)
            or not isinstance(question, str)
            or not question
            or len(question) > MAX_GATE_TEXT_CHARS
            or not isinstance(branches, list)
        ):
            raise KnowledgeGateCompileError(
                "knowledge_gate_check_invalid",
                "Every symbolic knowledge-gate check requires one bounded ID, "
                "question, and branch list.",
            )
        compiled_branches: list[dict[str, Any]] = []
        for branch_index, raw_branch in enumerate(branches):
            if not isinstance(raw_branch, dict):
                raise KnowledgeGateCompileError(
                    "knowledge_gate_branch_invalid",
                    f"Knowledge-gate branch {check_id}[{branch_index}] is invalid.",
                )
            outcome = str(raw_branch.get("outcome") or "")
            action = raw_branch.get("action")
            if outcome not in {"yes", "no", "unknown"}:
                raise KnowledgeGateCompileError(
                    "knowledge_gate_branch_invalid",
                    f"Knowledge-gate branch {check_id} has an unsupported outcome.",
                )
            if (
                not isinstance(action, str)
                or len(action) > MAX_GATE_TEXT_CHARS
            ):
                raise KnowledgeGateCompileError(
                    "knowledge_gate_branch_invalid",
                    f"Knowledge-gate branch {check_id} has an invalid action.",
                )
            raw_groups = raw_branch.get("selector_groups") or []
            if not isinstance(raw_groups, list):
                raise KnowledgeGateCompileError(
                    "knowledge_gate_group_invalid",
                    f"Knowledge-gate branch {check_id} groups must be a list.",
                )
            branch_group_ids: list[str] = []
            for group_index, raw_group in enumerate(raw_groups):
                if not isinstance(raw_group, dict):
                    raise KnowledgeGateCompileError(
                        "knowledge_gate_group_invalid",
                        f"Knowledge-gate group {check_id}[{group_index}] is invalid.",
                    )
                selectors = raw_group.get("selectors")
                if (
                    raw_group.get("mode") != "one_of"
                    or (
                        raw_group.get("id") is not None
                        and not is_canonical_knowledge_gate_identifier(
                            raw_group.get("id")
                        )
                    )
                    or not isinstance(selectors, list)
                    or not selectors
                    or len(selectors) > MAX_GATE_SELECTORS_PER_GROUP
                    or any(
                        not isinstance(value, str)
                        or not value
                        or value != value.strip()
                        for value in selectors
                    )
                ):
                    raise KnowledgeGateCompileError(
                        "knowledge_gate_group_invalid",
                        f"Knowledge-gate group {check_id}[{group_index}] is malformed.",
                    )
                candidate_ids: list[str] = []
                unresolved: list[str] = []
                for selector in dict.fromkeys(selectors):
                    resolved = _runtime_candidates_for_selector(
                        selector,
                        owner_skill=owner_skill,
                        worker_id=worker_id,
                        available_tools=available,
                        loaded_packages=loaded_packages,
                        allowed_resources=resource_grants,
                        allowed_scripts=script_grants,
                        process_only_scripts=process_grants,
                        allowed_package_digests=package_grants,
                        allowed_commands=command_grants,
                        allowed_http_get=http_get_grants,
                        allowed_http_post=http_post_grants,
                        allowed_sandbox_egress=sandbox_egress_grants,
                        allowed_sandbox_egress_rules=(
                            sandbox_egress_rule_grants
                        ),
                        frozen_mcp_catalog=frozen_mcp_catalog,
                        resolve_tool_selector=resolve_tool_selector,
                        browser_egress_rules=(
                            allowed_browser_egress_rules
                        ),
                        resource_expansions=resource_expansions,
                    )
                    if not resolved:
                        unresolved.append(selector)
                    for candidate in resolved:
                        candidate_id = str(candidate["candidate_id"])
                        candidate_by_id.setdefault(candidate_id, candidate)
                        candidate_ids.append(candidate_id)
                group_id = _stable_id(
                    "group",
                    {
                        "owner_skill": owner_skill,
                        "worker_id": worker_id,
                        "check_id": check_id,
                        "outcome": outcome,
                        "group_index": group_index,
                        "selectors": selectors,
                    },
                )
                groups.append({
                    "id": group_id,
                    "check_id": check_id,
                    "outcome": outcome,
                    "mode": "one_of",
                    "candidate_ids": list(dict.fromkeys(candidate_ids)),
                    "selectors": list(dict.fromkeys(selectors)),
                    "unresolved_selectors": unresolved,
                })
                branch_group_ids.append(group_id)
                if len(groups) > MAX_GATE_GROUPS:
                    raise KnowledgeGateCompileError(
                        "knowledge_gate_group_limit",
                        f"Knowledge gate may contain at most {MAX_GATE_GROUPS} groups.",
                    )
            compiled_branches.append({
                "outcome": outcome,
                "action": action,
                "group_ids": branch_group_ids,
            })
        checks.append({
            "id": check_id,
            "question": question,
            "branches": compiled_branches,
            "legacy_ambiguous": bool(raw_check.get("legacy_ambiguous")),
        })
        if check_index >= MAX_GATE_CHECKS:
            raise KnowledgeGateCompileError(
                "knowledge_gate_check_limit",
                f"Knowledge gate may contain at most {MAX_GATE_CHECKS} checks.",
            )
    candidates = sorted(
        candidate_by_id.values(),
        key=lambda item: str(item.get("candidate_id") or ""),
    )
    if len(candidates) > MAX_GATE_CANDIDATES:
        raise KnowledgeGateCompileError(
            "knowledge_gate_candidate_limit",
            f"Knowledge gate may contain at most {MAX_GATE_CANDIDATES} candidates.",
        )
    plan = {
        "schema_version": KNOWLEDGE_GATE_PLAN_SCHEMA_VERSION,
        "worker_id": worker_id,
        "owner_skill": owner_skill,
        "checks": checks,
        "groups": groups,
        "candidates": candidates,
    }
    encoded_plan = json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded_plan) > MAX_KNOWLEDGE_GATE_PLAN_BYTES:
        raise KnowledgeGateCompileError(
            "knowledge_gate_plan_size_limit",
            "The frozen knowledge-gate plan exceeds the bounded canonical "
            f"size of {MAX_KNOWLEDGE_GATE_PLAN_BYTES} bytes.",
        )
    return plan, canonical_json_sha256(plan)


_CANDIDATE_AUTHORITY_KEYS = frozenset({
    "resource_grants",
    "script_grants",
    "process_only_script_grants",
    "script_authority_grants",
    "package_grants",
    "command_grants",
    "http_get_grants",
    "http_post_grants",
    "sandbox_egress_grants",
    "sandbox_egress_rule_grants",
    "browser_egress_rule_grants",
    "tool_names",
    "receipt_bindings",
})


def _authority_projection(
    candidates: Iterable[dict[str, Any]],
    *,
    script_authority_grants: Iterable[
        tuple[str, str, str, str, str, str]
    ] = (),
) -> dict[str, Any]:
    candidate_rows = [
        dict(candidate)
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    resources: list[tuple[str, str]] = []
    scripts: list[tuple[str, str, str]] = []
    process_only: list[tuple[str, str, str]] = []
    packages: list[tuple[str, str]] = []
    commands: list[tuple[str, str, str, tuple[str, ...]]] = []
    http_get: list[tuple[str, str]] = []
    http_post: list[tuple[str, str]] = []
    sandbox_egress: list[tuple[str, str]] = []
    sandbox_rules: list[tuple[str, str, tuple[str, ...]]] = []
    browser_rules: list[tuple[str, tuple[str, ...]]] = []
    tools: list[str] = []
    for candidate in candidate_rows:
        kind = str(candidate.get("kind") or "")
        skill_name = str(candidate.get("skill_name") or "")
        tools.extend(candidate_tool_names(candidate))
        package_sha256 = str(candidate.get("package_sha256") or "")
        if skill_name and package_sha256:
            packages.append((skill_name, package_sha256))
        if kind == "skill_resource":
            resources.append((
                skill_name,
                str(candidate.get("resource_path") or ""),
            ))
        elif (
            kind == "native_tool"
            and candidate.get("tool_name") == "browser_navigate"
        ):
            browser_rules.extend(
                (
                    str(rule.get("url_prefix") or ""),
                    tuple(
                        str(method)
                        for method in rule.get("methods") or []
                    ),
                )
                for rule in candidate.get("browser_egress_rules") or []
                if isinstance(rule, dict)
            )
        elif kind == "skill_script":
            script = (
                skill_name,
                str(candidate.get("resource_path") or ""),
                str(candidate.get("sha256") or ""),
            )
            scripts.append(script)
            if "run_skill_process" in candidate_tool_names(candidate):
                process_only.append(script)
            sandbox_egress.extend(
                (skill_name, str(prefix))
                for prefix in (
                    candidate.get("sandbox_egress_url_prefixes") or []
                )
            )
            sandbox_rules.extend(
                (
                    skill_name,
                    str(rule.get("url_prefix") or ""),
                    tuple(
                        str(method)
                        for method in rule.get("methods") or []
                    ),
                )
                for rule in candidate.get("sandbox_egress_rules") or []
                if isinstance(rule, dict)
            )
        elif kind == "declared_command":
            commands.append((
                skill_name,
                str(candidate.get("command_id") or ""),
                str(candidate.get("executable") or ""),
                tuple(
                    str(value)
                    for value in candidate.get("fixed_argv") or []
                ),
            ))
            sandbox_egress.extend(
                (skill_name, str(prefix))
                for prefix in (
                    candidate.get("sandbox_egress_url_prefixes") or []
                )
            )
            sandbox_rules.extend(
                (
                    skill_name,
                    str(rule.get("url_prefix") or ""),
                    tuple(
                        str(method)
                        for method in rule.get("methods") or []
                    ),
                )
                for rule in candidate.get("sandbox_egress_rules") or []
                if isinstance(rule, dict)
            )
        elif kind == "skill_http_prefix":
            grant = (
                skill_name,
                str(candidate.get("url_prefix") or ""),
            )
            if candidate.get("tool_name") == "skill_http_post_json":
                http_post.append(grant)
            else:
                http_get.append(grant)
    script_set = set(scripts)
    authorities = [
        tuple(str(value) for value in row)
        for row in script_authority_grants
        if (
            isinstance(row, (list, tuple))
            and len(row) == 6
            and (
                str(row[0]),
                str(row[4]),
                str(row[5]),
            ) in script_set
        )
    ]
    return {
        "resource_grants": list(dict.fromkeys(resources)),
        "script_grants": list(dict.fromkeys(scripts)),
        "process_only_script_grants": list(dict.fromkeys(process_only)),
        "script_authority_grants": list(dict.fromkeys(authorities)),
        "package_grants": list(dict.fromkeys(packages)),
        "command_grants": list(dict.fromkeys(commands)),
        "http_get_grants": list(dict.fromkeys(http_get)),
        "http_post_grants": list(dict.fromkeys(http_post)),
        "sandbox_egress_grants": list(dict.fromkeys(
            sandbox_egress
        )),
        "sandbox_egress_rule_grants": list(dict.fromkeys(
            sandbox_rules
        )),
        "browser_egress_rule_grants": list(dict.fromkeys(
            browser_rules
        )),
        "tool_names": list(dict.fromkeys(tools)),
        "receipt_bindings": candidate_rows,
    }


def validate_knowledge_gate_candidate_authority(
    plan: dict[str, Any] | None,
    authority: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate the hidden, parent-intersected conditional authority bundle.

    The bundle is not model-authored and never grants more than the frozen
    plan. It is kept separate from the child's initial ToolContext so no
    branch capability exists before the typed decision is accepted.
    """

    if not isinstance(plan, dict) or not isinstance(authority, dict):
        raise KnowledgeGateCompileError(
            "knowledge_gate_candidate_authority_missing",
            "A frozen knowledge-gate plan requires one conditional authority bundle.",
        )
    supplied_keys = set(authority)
    accepted_key_sets = {
        frozenset(_CANDIDATE_AUTHORITY_KEYS),
        frozenset(
            set(_CANDIDATE_AUTHORITY_KEYS)
            - {"browser_egress_rule_grants"}
        ),
        frozenset(
            set(_CANDIDATE_AUTHORITY_KEYS)
            - {
                "sandbox_egress_rule_grants",
                "browser_egress_rule_grants",
            }
        ),
    }
    if frozenset(supplied_keys) not in accepted_key_sets:
        raise KnowledgeGateCompileError(
            "knowledge_gate_candidate_authority_schema_invalid",
            "The conditional authority bundle has an invalid field set.",
        )
    candidates = plan.get("candidates")
    if not isinstance(candidates, list) or any(
        not isinstance(candidate, dict) for candidate in candidates
    ):
        raise KnowledgeGateCompileError(
            "knowledge_gate_candidate_authority_schema_invalid",
            "The frozen plan candidate list is invalid.",
        )
    raw_authorities = authority.get("script_authority_grants")
    if not isinstance(raw_authorities, list) or len(raw_authorities) > 2_048:
        raise KnowledgeGateCompileError(
            "knowledge_gate_candidate_authority_schema_invalid",
            "Conditional script authorities must be one bounded list.",
        )
    normalized_authorities: list[tuple[str, str, str, str, str, str]] = []
    for row in raw_authorities:
        if (
            not isinstance(row, (list, tuple))
            or len(row) != 6
            or any(
                not isinstance(value, str) or not value
                for value in row
            )
        ):
            raise KnowledgeGateCompileError(
                "knowledge_gate_candidate_authority_schema_invalid",
                "A conditional script authority has an invalid exact tuple.",
            )
        normalized_authorities.append(tuple(row))
    expected = _authority_projection(
        candidates,
        script_authority_grants=normalized_authorities,
    )
    supplied_projection = dict(authority)
    supplied_projection.setdefault("sandbox_egress_rule_grants", [])
    supplied_projection.setdefault("browser_egress_rule_grants", [])
    supplied_projection["script_authority_grants"] = (
        normalized_authorities
    )
    for key, expected_rows in expected.items():
        supplied = supplied_projection.get(key)
        if key == "receipt_bindings":
            if supplied != expected_rows:
                raise KnowledgeGateCompileError(
                    "knowledge_gate_candidate_authority_mismatch",
                    "Conditional receipt bindings differ from the frozen plan.",
                )
            continue
        if not isinstance(supplied, list):
            raise KnowledgeGateCompileError(
                "knowledge_gate_candidate_authority_schema_invalid",
                f"Conditional authority field {key} must be a list.",
            )
        try:
            supplied_set = {
                tuple(
                    tuple(value) if isinstance(value, list) else value
                    for value in row
                )
                if isinstance(row, (list, tuple))
                else row
                for row in supplied
            }
            expected_set = {
                tuple(
                    tuple(value) if isinstance(value, list) else value
                    for value in row
                )
                if isinstance(row, (list, tuple))
                else row
                for row in expected_rows
            }
        except TypeError as exc:
            raise KnowledgeGateCompileError(
                "knowledge_gate_candidate_authority_schema_invalid",
                f"Conditional authority field {key} contains invalid values.",
            ) from exc
        if supplied_set != expected_set or len(supplied) != len(expected_rows):
            raise KnowledgeGateCompileError(
                "knowledge_gate_candidate_authority_mismatch",
                f"Conditional authority field {key} differs from the frozen plan.",
            )
    return expected


def activated_knowledge_gate_candidate_authority(
    plan: dict[str, Any],
    decision_receipt: dict[str, Any],
    authority: dict[str, Any],
) -> dict[str, Any]:
    """Return only exact authority reachable from accepted branch groups."""

    group_by_id = {
        str(group.get("id") or ""): group
        for group in plan.get("groups") or []
        if isinstance(group, dict)
    }
    active_candidate_ids = {
        str(candidate_id)
        for group_id in decision_receipt.get("activated_group_ids") or []
        for candidate_id in (
            (group_by_id.get(str(group_id)) or {}).get("candidate_ids")
            or []
        )
    }
    active_candidates = [
        candidate
        for candidate in authority.get("receipt_bindings") or []
        if (
            isinstance(candidate, dict)
            and str(candidate.get("candidate_id") or "")
            in active_candidate_ids
        )
    ]
    return _authority_projection(
        active_candidates,
        script_authority_grants=(
            authority.get("script_authority_grants") or []
        ),
    )


def validate_knowledge_gate_decisions(
    plan: dict[str, Any] | None,
    *,
    expected_sha256: str,
    supplied_sha256: str,
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate one typed child decision and return its activated frontier."""

    if not isinstance(plan, dict):
        return {
            "status": "error",
            "error_code": "knowledge_gate_plan_unavailable",
            "error": "No runtime-owned knowledge-gate plan is active.",
        }
    actual_sha = canonical_json_sha256(plan)
    if (
        supplied_sha256 != expected_sha256
        or supplied_sha256 != actual_sha
    ):
        return {
            "status": "error",
            "error_code": "knowledge_gate_plan_identity_mismatch",
            "error": "The submitted decision does not match the active plan digest.",
            "expected_plan_sha256": expected_sha256,
        }
    if not isinstance(decisions, list) or len(decisions) > MAX_GATE_CHECKS:
        return {
            "status": "error",
            "error_code": "knowledge_gate_decisions_invalid",
            "error": "Knowledge-gate decisions must be one bounded list.",
        }
    check_by_id = {
        str(check.get("id") or ""): check
        for check in plan.get("checks") or []
        if isinstance(check, dict) and str(check.get("id") or "")
    }
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict) or set(decision) - {
            "check_id", "outcome", "reason",
        }:
            return {
                "status": "error",
                "error_code": "knowledge_gate_decisions_invalid",
                "error": f"Decision {index} contains invalid fields.",
            }
        check_id = decision.get("check_id")
        outcome = decision.get("outcome")
        reason = decision.get("reason")
        if (
            not isinstance(check_id, str)
            or check_id not in check_by_id
            or check_id in seen
            or outcome not in {"yes", "no", "unknown"}
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > MAX_GATE_DECISION_REASON_CHARS
        ):
            return {
                "status": "error",
                "error_code": "knowledge_gate_decisions_invalid",
                "error": f"Decision {index} is malformed or duplicated.",
            }
        seen.add(check_id)
        normalized.append({
            "check_id": check_id,
            "outcome": str(outcome),
            "reason": reason.strip(),
        })
    missing = sorted(set(check_by_id) - seen)
    if missing:
        return {
            "status": "error",
            "error_code": "knowledge_gate_decisions_incomplete",
            "error": "Every compiled check requires exactly one decision.",
            "missing_check_ids": missing,
        }
    group_by_id = {
        str(group.get("id") or ""): group
        for group in plan.get("groups") or []
        if isinstance(group, dict) and str(group.get("id") or "")
    }
    activated: list[str] = []
    unknown: list[str] = []
    for decision in normalized:
        outcome = decision["outcome"]
        if outcome == "unknown":
            unknown.append(decision["check_id"])
        check = check_by_id[decision["check_id"]]
        branch = next(
            (
                branch
                for branch in check.get("branches") or []
                if isinstance(branch, dict)
                and branch.get("outcome") == outcome
            ),
            None,
        )
        if isinstance(branch, dict):
            activated.extend(
                str(group_id)
                for group_id in branch.get("group_ids") or []
                if str(group_id) in group_by_id
            )
    activated = list(dict.fromkeys(activated))
    unresolved = [
        group_id
        for group_id in activated
        if not group_by_id[group_id].get("candidate_ids")
    ]
    required_tool_groups: list[list[str]] = []
    candidate_by_id = {
        str(candidate.get("candidate_id") or ""): candidate
        for candidate in plan.get("candidates") or []
        if isinstance(candidate, dict)
    }
    for group_id in activated:
        tools: list[str] = []
        for candidate_id in group_by_id[group_id].get("candidate_ids") or []:
            candidate = candidate_by_id.get(str(candidate_id))
            if candidate is not None:
                        tools.extend(candidate_tool_names(candidate))
        required_tool_groups.append(list(dict.fromkeys(tools)))
    return {
        "status": "accepted",
        "plan_sha256": expected_sha256,
        "decisions": normalized,
        "activated_group_ids": activated,
        "unresolved_group_ids": unresolved,
        "unknown_check_ids": unknown,
        "required_tool_groups": required_tool_groups,
    }


def build_knowledge_gate_decision_receipt(
    accepted_decision: dict[str, Any],
) -> dict[str, Any]:
    """Build the secret-free handler-owned receipt used across run layers.

    Decision reasons are model-authored explanatory text. They are useful in
    the immediate child conversation but are neither authority nor safe audit
    identifiers. The durable receipt therefore binds only the exact plan,
    check/outcome set, and runtime-derived activated frontier.
    """

    if (
        not isinstance(accepted_decision, dict)
        or accepted_decision.get("status") != "accepted"
    ):
        raise KnowledgeGateCompileError(
            "knowledge_gate_decision_receipt_not_accepted",
            "Only an accepted knowledge-gate decision can produce a receipt.",
        )
    core = {
        "schema_version": KNOWLEDGE_GATE_DECISION_RECEIPT_SCHEMA_VERSION,
        "plan_sha256": str(
            accepted_decision.get("plan_sha256") or ""
        ),
        "decision_outcomes": [
            {
                "check_id": str(row.get("check_id") or ""),
                "outcome": str(row.get("outcome") or ""),
            }
            for row in accepted_decision.get("decisions") or []
            if isinstance(row, dict)
        ],
        "activated_group_ids": [
            str(value)
            for value in accepted_decision.get("activated_group_ids") or []
        ],
        "unresolved_group_ids": [
            str(value)
            for value in accepted_decision.get("unresolved_group_ids") or []
        ],
        "unknown_check_ids": [
            str(value)
            for value in accepted_decision.get("unknown_check_ids") or []
        ],
    }
    return {
        **core,
        "receipt_sha256": canonical_json_sha256(core),
    }


def validate_knowledge_gate_decision_receipt(
    plan: dict[str, Any] | None,
    *,
    expected_sha256: str,
    receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate a handler-owned decision receipt and rederive its frontier."""

    if not isinstance(receipt, dict):
        return {
            "status": "error",
            "error_code": "knowledge_gate_decision_receipt_missing",
            "error": "The handler-owned knowledge-gate decision receipt is missing.",
        }
    required_fields = {
        "schema_version",
        "plan_sha256",
        "decision_outcomes",
        "activated_group_ids",
        "unresolved_group_ids",
        "unknown_check_ids",
        "receipt_sha256",
    }
    if (
        set(receipt) != required_fields
        or receipt.get("schema_version")
        != KNOWLEDGE_GATE_DECISION_RECEIPT_SCHEMA_VERSION
    ):
        return {
            "status": "error",
            "error_code": "knowledge_gate_decision_receipt_invalid",
            "error": "The knowledge-gate decision receipt schema is invalid.",
        }
    core = {
        key: receipt.get(key)
        for key in required_fields
        if key != "receipt_sha256"
    }
    try:
        expected_receipt_sha256 = canonical_json_sha256(core)
    except KnowledgeGateCompileError:
        expected_receipt_sha256 = ""
    if (
        not isinstance(receipt.get("receipt_sha256"), str)
        or receipt.get("receipt_sha256") != expected_receipt_sha256
    ):
        return {
            "status": "error",
            "error_code": "knowledge_gate_decision_receipt_digest_mismatch",
            "error": "The knowledge-gate decision receipt digest is invalid.",
        }
    outcomes = receipt.get("decision_outcomes")
    if not isinstance(outcomes, list):
        return {
            "status": "error",
            "error_code": "knowledge_gate_decision_receipt_invalid",
            "error": "The knowledge-gate decision receipt outcomes are invalid.",
        }
    decisions: list[dict[str, str]] = []
    for row in outcomes:
        if (
            not isinstance(row, dict)
            or set(row) != {"check_id", "outcome"}
            or not isinstance(row.get("check_id"), str)
            or row.get("outcome") not in {"yes", "no", "unknown"}
        ):
            return {
                "status": "error",
                "error_code": "knowledge_gate_decision_receipt_invalid",
                "error": "The knowledge-gate decision receipt outcomes are invalid.",
            }
        decisions.append({
            "check_id": row["check_id"],
            "outcome": row["outcome"],
            "reason": "handler-owned typed decision receipt",
        })
    derived = validate_knowledge_gate_decisions(
        plan,
        expected_sha256=expected_sha256,
        supplied_sha256=str(receipt.get("plan_sha256") or ""),
        decisions=decisions,
    )
    if derived.get("status") != "accepted":
        return derived
    for field_name in (
        "activated_group_ids",
        "unresolved_group_ids",
        "unknown_check_ids",
    ):
        declared = receipt.get(field_name)
        if (
            not isinstance(declared, list)
            or declared != derived.get(field_name)
        ):
            return {
                "status": "error",
                "error_code": (
                    "knowledge_gate_decision_receipt_frontier_mismatch"
                ),
                "error": (
                    "The knowledge-gate decision receipt frontier differs "
                    "from the frozen plan."
                ),
            }
    return {
        **derived,
        "decision_receipt_sha256": receipt["receipt_sha256"],
    }


def decision_tool_schema(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the generic public schema; plan content stays in the prompt."""

    check_ids = [
        str(check.get("id") or "")
        for check in plan.get("checks") or []
        if isinstance(check, dict) and str(check.get("id") or "")
    ]
    return {
        "name": KNOWLEDGE_GATE_DECISION_TOOL_NAME,
        "description": (
            "Submit exactly one evidence-sufficiency decision for every "
            "Harness-compiled worker knowledge-gate check. This control-plane "
            "tool activates only the exact conditional candidate groups in "
            "the runtime-owned plan; it grants no new capability."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "plan_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "decisions": {
                    "type": "array",
                    "minItems": len(check_ids),
                    "maxItems": len(check_ids),
                    "items": {
                        "type": "object",
                        "properties": {
                            "check_id": {"type": "string", "enum": check_ids},
                            "outcome": {
                                "type": "string",
                                "enum": [
                                    "yes", "no", "unknown",
                                ],
                            },
                            "reason": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_GATE_DECISION_REASON_CHARS,
                            },
                        },
                        "required": ["check_id", "outcome", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["plan_sha256", "decisions"],
            "additionalProperties": False,
        },
    }


def plan_prompt_payload(plan: dict[str, Any], digest: str) -> dict[str, Any]:
    """Return the bounded decision projection without inactive coordinates.

    Exact candidates are authority, not decision input.  Keeping every branch's
    URLs/scripts/resources in the model context after a decision lets public
    bridge tools such as ``skill_http_get`` drift across branches.  The runtime
    publishes only the selected frontier after accepting the typed decision.
    """

    groups = [
        {
            "id": group.get("id"),
            "check_id": group.get("check_id"),
            "outcome": group.get("outcome"),
            "mode": group.get("mode"),
            "selector_count": len(group.get("selectors") or []),
            "unresolved_selector_count": len(
                group.get("unresolved_selectors") or []
            ),
            "resolved_candidate_count": len(
                group.get("candidate_ids") or []
            ),
        }
        for group in plan.get("groups") or []
        if isinstance(group, dict)
    ]

    return {
        "schema_version": plan.get("schema_version"),
        "worker_id": plan.get("worker_id"),
        "owner_skill": plan.get("owner_skill"),
        "plan_sha256": digest,
        "checks": plan.get("checks") or [],
        "groups": groups,
        "instructions": (
            "Before ordinary execution, call submit_knowledge_gate_decisions "
            "once with every check. yes/no activates only the matching branch; "
            "unknown never guesses a branch and must remain an explicit "
            "WARN/degraded knowledge gap. Exact candidate coordinates are "
            "intentionally withheld during this decision phase. After the "
            "decision is accepted, follow only the runtime-owned activated "
            "frontier: candidate IDs within one group are alternatives and "
            "separate groups require separate dispatch receipts. "
            "Unresolved selectors are machine-owned gaps, never permission to "
            "invent a callable or ambient fallback."
        ),
    }
