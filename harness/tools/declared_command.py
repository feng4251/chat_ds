"""Execute one compiler-authorized command without shell evaluation."""

from __future__ import annotations

import json
from typing import Any

from runtime.python_env import (
    preflight_declared_skill_dependencies,
    preflight_isolated_skill_runtime,
)
from skills.command_grants import grant_tuple, load_current_skill_command_grants
from tools.context import ToolContext
from tools.execution_fence import require_execution_authority
from tools.isolated_skill_executor import (
    IsolatedSkillExecutorError,
    MAX_ARG_BYTES,
    MAX_ARG_CHARS,
    MAX_ARGS,
    execute_isolated_declared_command,
)
from tools.path_security import sandbox_dir


DEFAULT_TIMEOUT = 120


def _error(code: str, message: str, **extra: Any) -> str:
    return json.dumps({
        "status": "error",
        "error_code": code,
        "error": message,
        "execution_runtime": "isolated_skill_executor",
        "shell": False,
        "network": "disabled",
        "fallback_attempted": False,
        **extra,
    }, ensure_ascii=False)


def _validated_argv(argv: Any) -> list[str]:
    if not isinstance(argv, list) or len(argv) > MAX_ARGS:
        raise ValueError(f"argv must contain at most {MAX_ARGS} strings.")
    total = 0
    result: list[str] = []
    for item in argv:
        if not isinstance(item, str) or "\x00" in item or len(item) > MAX_ARG_CHARS:
            raise ValueError("Every argv item must be a bounded NUL-free string.")
        try:
            total += len(item.encode("utf-8", errors="strict"))
        except UnicodeError as exc:
            raise ValueError("Every argv item must be valid UTF-8.") from exc
        if total > MAX_ARG_BYTES:
            raise ValueError(f"argv exceeds the {MAX_ARG_BYTES}-byte aggregate limit.")
        result.append(item)
    return result


async def run_declared_command(
    skill_name: str,
    command_id: str,
    argv: list[str],
    cwd: str,
    context: ToolContext | None = None,
) -> str:
    """Run an exact current Skill grant using literal argv and shell=False."""
    if context is None:
        return _error(
            "missing_command_context",
            "Declared commands require a runtime-owned Skill capability context.",
        )
    if cwd not in {"workspace", "skill"}:
        return _error("invalid_cwd", "cwd must be exactly 'workspace' or 'skill'.")
    try:
        safe_argv = _validated_argv(argv)
    except ValueError as exc:
        return _error("invalid_argv", str(exc))

    allowed = [
        item for item in context.allowed_skill_commands
        if item[0] == skill_name and item[1] == command_id
    ]
    if len(allowed) != 1:
        return _error(
            "command_grant_not_authorized",
            "The command id is outside this run's compiled Skill capability closure.",
            skill_name=skill_name,
            command_id=command_id,
        )
    try:
        skill_root, _loaded, current_grants = load_current_skill_command_grants(
            skill_name,
            context.user_id,
            context.session_id,
            list(context.enabled_user_skills),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _error(
            "command_grant_revalidation_failed",
            str(exc),
            skill_name=skill_name,
            command_id=command_id,
        )
    current = [
        grant for grant in current_grants
        if grant_tuple(skill_name, grant) == allowed[0]
    ]
    if len(current) != 1:
        return _error(
            "command_grant_changed",
            "The declared command changed after compilation; recompile the Skill before execution.",
            skill_name=skill_name,
            command_id=command_id,
        )
    grant = current[0]
    executable = str(grant["executable"])
    prefix = [str(item) for item in grant.get("argv_prefix") or []]
    combined_argv = [*prefix, *safe_argv]
    if len(combined_argv) > MAX_ARGS:
        return _error("invalid_argv", f"Compiled prefix plus argv exceeds {MAX_ARGS} items.")

    dependency_preflight = preflight_declared_skill_dependencies(skill_root)
    if dependency_preflight.get("valid") is not True:
        return _error(
            "skill_runtime_prerequisites_unsatisfied",
            "The Skill's declared dependencies are unavailable in the isolated runtime.",
            runtime_preflight=dependency_preflight,
        )
    command_preflight = preflight_isolated_skill_runtime(commands=[executable])
    if command_preflight.get("valid") is not True:
        return _error(
            "declared_command_unavailable",
            "The granted executable is unavailable in the isolated runtime image.",
            runtime_preflight=command_preflight,
        )
    workspace = sandbox_dir(
        context.user_id, context.session_id, sub="workspace"
    ).resolve()
    try:
        result = await execute_isolated_declared_command(
            skill_root=skill_root,
            workspace=workspace,
            executable=executable,
            argv=combined_argv,
            cwd=cwd,
            timeout=DEFAULT_TIMEOUT,
            **(
                {
                    "execution_authority_check": lambda: (
                        require_execution_authority(
                            context,
                            boundary="declared_command.executor_commit",
                        )
                    )
                }
                if context.execution_fence is not None
                else {}
            ),
        )
    except IsolatedSkillExecutorError as exc:
        return _error(exc.code, str(exc), skill_name=skill_name, command_id=command_id)
    result.update({
        "skill_name": skill_name,
        "command_id": command_id,
        "execution_runtime": "isolated_skill_executor",
        "environment_policy": "ephemeral_snapshot_no_secrets",
        "fallback_attempted": False,
        "runtime_preflight": command_preflight,
    })
    return json.dumps(result, ensure_ascii=False)


RUN_DECLARED_COMMAND_SCHEMA = {
    "name": "run_declared_command",
    "description": (
        "Run one exact command grant compiled from the selected Skill. The executable "
        "is fixed by command_id; argv items are passed literally with no shell, "
        "redirection, interpolation, or expansion."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "minLength": 1, "maxLength": 64},
            "command_id": {
                "type": "string",
                "pattern": "^command-[0-9a-f]{24}$",
            },
            "argv": {
                "type": "array",
                "items": {"type": "string", "maxLength": MAX_ARG_CHARS},
                "maxItems": MAX_ARGS,
            },
            "cwd": {"type": "string", "enum": ["workspace", "skill"]},
        },
        "required": ["skill_name", "command_id", "argv", "cwd"],
        "additionalProperties": False,
    },
}
