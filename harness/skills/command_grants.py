"""Compile and verify least-privilege command grants for declarative Skills.

This module deliberately does *not* implement a shell.  A grant identifies one
PATH-resolved executable and, optionally, an immutable argv prefix.  The only
cross-client shell selectors accepted are narrowly-shaped ``Bash(cmd:*)`` or
``Shell(cmd:*)`` declarations; bare/wildcard shells and prose are never
interpreted as authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


MAX_COMMAND_GRANTS = 256
MAX_PREFIX_ARGS = 16
MAX_PREFIX_ARG_CHARS = 512
EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_SHELL_SELECTOR_RE = re.compile(r"^(?:Bash|Shell)\(([^()]*)\)$", re.IGNORECASE)
_UNSAFE_SELECTOR_CHARS_RE = re.compile(r"[;&|<>`$\\\r\\\n\\\x00]")
_PREFIX_ARG_RE = re.compile(r"^[A-Za-z0-9_./+=:@%,-]{1,512}$")


def parse_allowed_tool_selectors(value: Any) -> list[str]:
    """Split Agent Skills ``allowed-tools`` without breaking selectors.

    The standard scalar is space-delimited, but narrowed cross-client command
    selectors may themselves contain literal spaces, for example
    ``Shell(git status:*)``. Only whitespace and commas at parenthesis depth
    zero delimit selectors. Malformed/unbalanced input is returned as one
    intact item so the caller's selector validator can reject it explicitly;
    this parser never repairs malformed authority into broader capabilities.
    """
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    selectors: list[str] = []
    token: list[str] = []
    depth = 0
    malformed = False
    for character in text:
        if character == "(":
            depth += 1
            if depth > 1:
                malformed = True
            token.append(character)
            continue
        if character == ")":
            if depth == 0:
                malformed = True
            else:
                depth -= 1
            token.append(character)
            continue
        if depth == 0 and (character.isspace() or character == ","):
            candidate = "".join(token).strip()
            if candidate:
                selectors.append(candidate)
            token = []
            continue
        token.append(character)
    candidate = "".join(token).strip()
    if candidate:
        selectors.append(candidate)
    if malformed or depth != 0:
        return [text]
    return selectors


def _grant_id(executable: str, argv_prefix: Iterable[str], scope: str = "") -> str:
    canonical = json.dumps(
        {
            "executable": executable,
            "argv_prefix": list(argv_prefix),
            "scope": str(scope or ""),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "command-" + hashlib.sha256(canonical).hexdigest()[:24]


def _grant(
    executable: str,
    argv_prefix: Iterable[str] = (),
    *,
    declaration_kind: str,
    source: str = "",
    scope: str = "",
) -> dict[str, Any] | None:
    executable = str(executable or "").strip()
    prefix = tuple(str(item) for item in argv_prefix)
    if EXECUTABLE_RE.fullmatch(executable) is None:
        return None
    if (
        len(prefix) > MAX_PREFIX_ARGS
        or any(
            not item
            or len(item) > MAX_PREFIX_ARG_CHARS
            or _UNSAFE_SELECTOR_CHARS_RE.search(item)
            or _PREFIX_ARG_RE.fullmatch(item) is None
            for item in prefix
        )
    ):
        return None
    return {
        "id": _grant_id(executable, prefix, scope),
        "executable": executable,
        "argv_prefix": list(prefix),
        "declaration_kind": declaration_kind,
        **({"source": source} if source else {}),
        **({"scope": scope} if scope else {}),
    }


def command_grant_from_selector(selector: Any) -> dict[str, Any] | None:
    """Return a grant only for an exactly narrowed Bash/Shell selector.

    Examples accepted: ``Bash(python:*)`` and ``Shell(git status:*)``.
    ``Bash``, ``Bash(*)``, ``Bash(*:*)``, executable paths, shell operators,
    interpolation, redirection, and additional wildcards are rejected.
    """
    if not isinstance(selector, str) or selector != selector.strip():
        return None
    match = _SHELL_SELECTOR_RE.fullmatch(selector)
    if match is None:
        return None
    inner = match.group(1)
    if not inner.endswith(":*"):
        return None
    literal = inner[:-2].strip()
    if (
        not literal
        or "*" in literal
        or "?" in literal
        or _UNSAFE_SELECTOR_CHARS_RE.search(literal)
    ):
        return None
    # This is intentionally not shlex: quotes and escapes are shell syntax.
    # Declarative prefixes use simple whitespace-separated literal tokens.
    tokens = literal.split()
    if not tokens or any("/" in tokens[0] for _ in (0,)):
        return None
    return _grant(
        tokens[0],
        tokens[1:],
        declaration_kind="narrowed_shell_selector",
        source=selector,
        scope=selector,
    )


def compile_environment_command_grants(
    commands: Any,
    selectors: Any = (),
) -> list[dict[str, Any]]:
    """Compile explicit structured commands and safe selector translations."""
    grants: list[dict[str, Any]] = []
    if isinstance(commands, list):
        for declaration in commands[:MAX_COMMAND_GRANTS]:
            if not isinstance(declaration, dict):
                continue
            # ``prerequisites.commands`` is an availability contract, not an
            # execution capability. Only a future compiler stage that has
            # independently proven explicit executable authority may set this
            # internal IR bit. Standard Skills grant commands through exact
            # allowed-tools selectors such as Bash(git status:*).
            if declaration.get("execution_authorized") is not True:
                continue
            grant = _grant(
                str(declaration.get("name") or ""),
                declaration_kind="environment_contract.commands",
                source=",".join(
                    str(item) for item in declaration.get("source_files") or []
                    if str(item)
                ),
                scope=",".join(
                    str(item) for item in declaration.get("source_files") or []
                    if str(item)
                ),
            )
            if grant is not None:
                grants.append(grant)
    if isinstance(selectors, str):
        selector_items = parse_allowed_tool_selectors(selectors)
    elif isinstance(selectors, (list, tuple, set)):
        selector_items = [
            parsed
            for selector in selectors
            for parsed in (
                parse_allowed_tool_selectors(selector)
                if isinstance(selector, str) else []
            )
        ]
    else:
        selector_items = []
    for selector in selector_items[:MAX_COMMAND_GRANTS]:
        grant = command_grant_from_selector(selector)
        if grant is not None:
            grants.append(grant)
    deduped: dict[str, dict[str, Any]] = {}
    for grant in grants:
        deduped.setdefault(str(grant["id"]), grant)
    return list(deduped.values())[:MAX_COMMAND_GRANTS]


def command_grants_from_environment(environment: Any) -> list[dict[str, Any]]:
    if not isinstance(environment, dict):
        return []
    compiled = environment.get("command_grants")
    if isinstance(compiled, list):
        valid: list[dict[str, Any]] = []
        for item in compiled[:MAX_COMMAND_GRANTS]:
            if not isinstance(item, dict):
                continue
            grant = _grant(
                str(item.get("executable") or ""),
                item.get("argv_prefix") or [],
                declaration_kind=str(item.get("declaration_kind") or "compiled"),
                source=str(item.get("source") or ""),
                scope=str(item.get("scope") or ""),
            )
            if grant is not None and grant["id"] == item.get("id"):
                valid.append(grant)
        return valid
    return compile_environment_command_grants(
        environment.get("commands"), environment.get("allowed_tools") or []
    )


def _selectors(value: Any) -> list[str]:
    if isinstance(value, str):
        return parse_allowed_tool_selectors(value)
    if isinstance(value, (list, tuple, set)):
        return [
            selector
            for item in value
            for selector in parse_allowed_tool_selectors(item)
            if isinstance(item, str)
        ]
    if isinstance(value, dict):
        return [
            str(name).strip() for name, enabled in value.items()
            if enabled not in (False, None) and str(name).strip()
        ]
    return []


def scope_command_grant(grant: Any, scope: str) -> dict[str, Any] | None:
    if not isinstance(grant, dict) or not isinstance(scope, str) or not scope:
        return None
    return _grant(
        str(grant.get("executable") or ""),
        grant.get("argv_prefix") or [],
        declaration_kind=str(grant.get("declaration_kind") or "compiled"),
        source=str(grant.get("source") or ""),
        scope=scope,
    )


def grants_for_declaration(
    declaration: Any,
    *,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """Collect only capabilities explicitly present in one compiled node."""
    if not isinstance(declaration, dict):
        return []
    grants = command_grants_from_environment(declaration.get("environment_contract"))
    for selector in _selectors(declaration.get("tools")) + _selectors(declaration.get("tool")):
        grant = command_grant_from_selector(selector)
        if grant is not None:
            grants.append(grant)
    if scope:
        grants = [
            scoped for item in grants
            if (scoped := scope_command_grant(item, scope)) is not None
        ]
    deduped = {str(item["id"]): item for item in grants}
    return list(deduped.values())[:MAX_COMMAND_GRANTS]


def selected_plan_command_grants(
    execution_contract: Any,
    plan: Any,
) -> list[dict[str, Any]]:
    """Collect package plus selected-node grants from a compiled plan."""
    grants: list[dict[str, Any]] = []
    if isinstance(execution_contract, dict):
        grants.extend(
            scoped for item in command_grants_from_environment(
                execution_contract.get("environment_contract")
            )
            if (scoped := scope_command_grant(item, "package")) is not None
        )
    if isinstance(plan, dict):
        workers = plan.get("workers")
        if isinstance(workers, dict):
            for worker_id in plan.get("required_workers") or []:
                grants.extend(grants_for_declaration(
                    workers.get(worker_id), scope=f"worker:{worker_id}"
                ))
        for source in plan.get("bootstrap_sources") or []:
            source_id = str(source.get("id") or "") if isinstance(source, dict) else ""
            grants.extend(grants_for_declaration(
                source, scope=f"bootstrap:{source_id}"
            ))
        for step in plan.get("aggregation_steps") or []:
            step_id = str(step.get("id") or "") if isinstance(step, dict) else ""
            grants.extend(grants_for_declaration(
                step, scope=f"aggregation:{step_id}"
            ))
    deduped = {str(item["id"]): item for item in grants}
    return list(deduped.values())[:MAX_COMMAND_GRANTS]


def all_compiled_command_grants(loaded_skill: Any) -> list[dict[str, Any]]:
    """Collect all current package grants for dispatch-time revalidation."""
    execution = {}
    if isinstance(loaded_skill, dict):
        execution = loaded_skill.get("execution_contract") or {}
        if not isinstance(execution, dict):
            workflow = loaded_skill.get("workflow_contract") or {}
            execution = workflow.get("execution_contract") if isinstance(workflow, dict) else {}
    grants: list[dict[str, Any]] = [
        scoped for item in command_grants_from_environment(
            execution.get("environment_contract") if isinstance(execution, dict) else {}
        )
        if (scoped := scope_command_grant(item, "package")) is not None
    ]
    if isinstance(execution, dict):
        for worker in execution.get("workers") or []:
            worker_id = str(worker.get("id") or "") if isinstance(worker, dict) else ""
            grants.extend(grants_for_declaration(
                worker, scope=f"worker:{worker_id}"
            ))
        bootstrap = execution.get("knowledge_bootstrap") or {}
        if isinstance(bootstrap, dict):
            for source in bootstrap.get("sources") or []:
                source_id = str(source.get("id") or "") if isinstance(source, dict) else ""
                grants.extend(grants_for_declaration(
                    source, scope=f"bootstrap:{source_id}"
                ))
        aggregation = execution.get("aggregation") or {}
        if isinstance(aggregation, dict):
            for step in aggregation.get("steps") or []:
                step_id = str(step.get("id") or "") if isinstance(step, dict) else ""
                grants.extend(grants_for_declaration(
                    step, scope=f"aggregation:{step_id}"
                ))
    deduped = {str(item["id"]): item for item in grants}
    return list(deduped.values())[:MAX_COMMAND_GRANTS]


def load_current_skill_command_grants(
    skill_name: str,
    user_id: str,
    session_id: str,
    enabled_user_skills: list[str] | None = None,
) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    """Reload one canonical visible Skill and its compiled exact grants."""
    from skills.loader import load_skill_content
    from skills.scanner import resolve_skill_path

    executable_user_skills = (
        list(enabled_user_skills) if enabled_user_skills is not None else []
    )
    skill_md = resolve_skill_path(
        skill_name,
        user_id,
        session_id,
        enabled_user_skills=executable_user_skills,
    )
    if skill_md is None:
        raise ValueError("The declared Skill is not enabled and installed for this run.")
    skill_root = skill_md.parent.resolve(strict=True)
    loaded = load_skill_content(
        skill_md,
        skill_dir=str(skill_root),
        session_id=session_id,
    )
    if loaded.get("error") or loaded.get("name") != skill_name:
        raise ValueError("The current Skill package cannot be compiled canonically.")
    return skill_root, loaded, all_compiled_command_grants(loaded)


def grant_tuple(skill_name: str, grant: dict[str, Any]) -> tuple[str, str, str, tuple[str, ...]]:
    return (
        skill_name,
        str(grant.get("id") or ""),
        str(grant.get("executable") or ""),
        tuple(str(item) for item in grant.get("argv_prefix") or []),
    )
