"""Run installed Skill Python only through the controlled session sandbox."""

from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import os
import re
import signal
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.python_env import (
    ensure_session_runtime,
    preflight_declared_skill_dependencies,
    resolve_session_python,
    runtime_env_for_subprocess,
)
from skills.scanner import USER_SKILLS_BASE
from tools.context import ToolContext
from tools.effect_receipt import (
    build_isolated_execution_effect_receipt,
)
from tools.execution_fence import require_execution_authority
from tools.isolated_skill_executor import (
    IsolatedSkillExecutorError,
    execute_isolated_session_code,
    execute_isolated_skill_script,
    snapshot_skill_package,
)
from tools.omission_guard import (
    compacted_history_omission_error,
    contains_compacted_history_omission,
)
from tools.path_security import sandbox_dir, validate_path
from tools.session_sandbox_policy import (
    SessionSandboxPolicyError,
    session_sandbox_egress_budget_binding,
)
from tools.skill_invocation_egress import (
    bind_python_invocation_parameters,
    invocation_bound_skill_egress_policy_for_invocations,
)
from tools.skill_runtime_profile import select_skill_runtime_profile
from tools.skill_script import (
    SkillScriptError,
    _expected_skill_package_sha256,
    _resolve_session_skill_script,
)

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 300
MAX_STDOUT = 80_000
MAX_STDERR = 20_000
MAX_ARTIFACTS = 40
MAX_ARTIFACT_SCAN_FILES = 20_000
MAX_CLI_ARGS = 64
MAX_CLI_ARG_CHARS = 4_096
MAX_CLI_ARG_BYTES = 32_768
MAX_FUNCTION_INPUT_BYTES = 128_000
MAX_FUNCTION_ARGS = 64
MAX_FUNCTION_KWARGS = 128
MAX_FUNCTION_JSON_DEPTH = 20
MAX_FUNCTION_SOURCE_BYTES = 2_000_000
MAX_FUNCTION_RESULT_CHARS = 120_000
MAX_RUNNER_ENVELOPE_BYTES = 260_000
MAX_PUBLIC_FUNCTIONS = 80
MAX_PUBLIC_CLASSES = 80
MAX_PUBLIC_METHODS_PER_CLASS = 80
PUBLIC_FUNCTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
ARTIFACT_SKIP_DIRS = {".chatds", "debug", "__pycache__", ".pytest_cache"}
FUNCTION_RUNNER = Path(__file__).with_name("skill_function_runner.py").resolve()
FUNCTION_RESULT_COMPONENTS = (".chatds", "function_calls")


def preflight_run_skill_python_args(
    args: dict[str, Any],
    context: ToolContext | None,
) -> dict[str, Any] | None:
    """Purely validate an exact Skill-Python invocation before dispatch.

    The registry already owns capability/resource/schema/omission checks. This
    adapter-specific hook resolves the granted local script and inspects its
    AST so nonexistent functions/classes/methods and mixed invocation modes do
    not consume an executor call or trigger repeated long model retries. It
    never initializes a runtime, imports Skill code, or invokes a sidecar.
    """

    if context is None:
        # Direct legacy dispatch without a runtime-owned identity retains the
        # handler's existing validation. Agent-loop calls always carry context.
        return None
    script: Path | None = None
    invocation_mode = _requested_invocation_mode(
        function_name=args.get("function_name"),
        class_name=args.get("class_name"),
        method_name=args.get("method_name"),
        instance_arguments_present=any(
            key in args
            for key in (
                "constructor_args",
                "constructor_kwargs",
                "method_args",
                "method_kwargs",
            )
        ),
    )
    try:
        script_path = args.get("script_path")
        if not isinstance(script_path, str) or not script_path.strip():
            raise ValueError("script_path must be a non-empty string.")
        cli_args = args.get("args")
        safe_cli_args = _validated_cli_args(cli_args)
        _validated_tool_timeout(args.get("timeout", DEFAULT_TIMEOUT))
        script, skill_root, _skill_name = _resolve_session_skill_script(
            script_path,
            context.user_id,
            context.session_id,
            list(context.enabled_user_skills),
        )
        function_name = args.get("function_name")
        function_fields_present = any(
            key in args
            for key in ("function_name", "function_args", "function_kwargs")
        )
        instance_fields_present = any(
            key in args
            for key in (
                "class_name",
                "method_name",
                "constructor_args",
                "constructor_kwargs",
                "method_args",
                "method_kwargs",
            )
        )
        if function_fields_present and instance_fields_present:
            raise ValueError(
                "Top-level function mode and instance-method mode are mutually "
                "exclusive; provide exactly one callable target."
            )
        if function_name is not None:
            _validated_function_payload(
                script,
                function_name=function_name,
                function_args=args.get("function_args"),
                function_kwargs=args.get("function_kwargs"),
                cli_args=safe_cli_args,
                skill_root=skill_root,
            )
        elif function_fields_present:
            raise ValueError(
                "function_args/function_kwargs require function_name."
            )
        elif instance_fields_present:
            _validated_instance_method_payload(
                script,
                class_name=args.get("class_name"),
                method_name=args.get("method_name"),
                constructor_args=args.get("constructor_args"),
                constructor_kwargs=args.get("constructor_kwargs"),
                method_args=args.get("method_args"),
                method_kwargs=args.get("method_kwargs"),
                cli_args=safe_cli_args,
                skill_root=skill_root,
            )
    except (SkillScriptError, FileNotFoundError, OSError, ValueError) as exc:
        result: dict[str, Any] = {
            "error": str(exc),
            "error_code": "skill_python_invocation_preflight_failed",
            "reason": "skill_python_invocation_preflight_failed",
            "invocation_mode": invocation_mode,
        }
        # A guessed callable name is a deterministic, non-dispatched
        # preflight error.  Return the bounded public inventory from the
        # already-authorized exact script so the next turn can correct it
        # without another guess or executor call.
        if script is not None:
            if invocation_mode == "function":
                result["available_functions"] = _public_function_inventory(
                    script
                )
            elif invocation_mode == "instance_method":
                result["available_classes"] = _public_class_inventory(script)
        return result
    return None


def _requested_invocation_mode(
    *,
    function_name: str | None,
    class_name: str | None,
    method_name: str | None,
    instance_arguments_present: bool = False,
) -> str:
    if class_name is not None or method_name is not None or instance_arguments_present:
        return "instance_method"
    if function_name is not None:
        return "function"
    return "cli"


async def run_skill_python(
    script_path: str,
    args: list[str] | None = None,
    function_name: str | None = None,
    function_args: list[Any] | None = None,
    function_kwargs: dict[str, Any] | None = None,
    class_name: str | None = None,
    method_name: str | None = None,
    constructor_args: list[Any] | None = None,
    constructor_kwargs: dict[str, Any] | None = None,
    method_args: list[Any] | None = None,
    method_kwargs: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str = "workspace",
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
    context: ToolContext | None = None,
) -> str:
    invocation_mode = _requested_invocation_mode(
        function_name=function_name,
        class_name=class_name,
        method_name=method_name,
        instance_arguments_present=any(
            value is not None
            for value in (
                constructor_args,
                constructor_kwargs,
                method_args,
                method_kwargs,
            )
        ),
    )
    for field, value in (
        ("script_path", script_path),
        ("cwd", cwd),
        ("args", args or []),
        ("function_name", function_name),
        ("function_args", function_args or []),
        ("function_kwargs", function_kwargs or {}),
        ("class_name", class_name),
        ("method_name", method_name),
        ("constructor_args", constructor_args or []),
        ("constructor_kwargs", constructor_kwargs or {}),
        ("method_args", method_args or []),
        ("method_kwargs", method_kwargs or {}),
    ):
        if contains_compacted_history_omission(value):
            error = compacted_history_omission_error(field)
            error["invocation_mode"] = invocation_mode
            return json.dumps(error, ensure_ascii=False)
    if isinstance(script_path, str) and script_path.strip().startswith("workspace/"):
        return json.dumps({
            "status": "error",
            "error": (
                "run_skill_python executes only an exact installed Skill script. "
                "Workspace Python is not accepted; use execute_code for bounded "
                "ad-hoc calculations."
            ),
            "error_code": "workspace_skill_execution_forbidden",
            "invocation_mode": invocation_mode,
            "script_path": script_path.strip(),
            "isolated_execution": True,
            "managed_fallback": False,
        }, ensure_ascii=False)
    try:
        if not isinstance(script_path, str) or not script_path.strip():
            raise ValueError("script_path must be a non-empty string.")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("cwd must be a non-empty string.")
        safe_cli_args = _validated_cli_args(args)
        safe_timeout = _validated_tool_timeout(timeout)
        script, skill_root, skill_name = _resolve_session_skill_script(
            script_path,
            user_id,
            session_id,
            enabled_user_skills,
        )
        expected_skill_sha256 = _expected_skill_package_sha256(
            context,
            skill_name,
        )
        if script.suffix != ".py":
            raise ValueError("script_path must point to a .py file.")
        workdir = _resolve_selected_skill_cwd(
            cwd,
            user_id=user_id,
            session_id=session_id,
            script=script,
            skill_root=skill_root,
            skill_name=skill_name,
        )
        display_path = _selected_skill_display_path(
            script,
            skill_root=skill_root,
            skill_name=skill_name,
        )
    except SkillScriptError as exc:
        message = str(exc)
        if "symlink" in str(exc.code).casefold():
            message = "Symlinks are not allowed in Skill script paths. " + message
        return json.dumps({
            "status": "error",
            "error": message,
            "error_code": exc.code,
            "invocation_mode": invocation_mode,
            "available_skill_scripts": _available_skill_scripts(
                user_id,
                session_id,
                enabled_user_skills,
            ),
        }, ensure_ascii=False)
    except (ValueError, FileNotFoundError, SessionSandboxPolicyError) as exc:
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "invocation_mode": invocation_mode,
            "available_skill_scripts": _available_skill_scripts(
                user_id,
                session_id,
                enabled_user_skills,
            ),
        }, ensure_ascii=False)

    runtime_preflight = preflight_declared_skill_dependencies(skill_root)
    if runtime_preflight.get("valid") is not True:
        return json.dumps({
            "status": "error",
            "error": (
                "The installed Skill's declared runtime dependencies are not "
                "available in the controlled isolated session sandbox."
            ),
            "error_code": "skill_runtime_prerequisites_unsatisfied",
            "invocation_mode": invocation_mode,
            "script_path": display_path,
            "runtime_preflight": runtime_preflight,
            "isolated_execution": True,
            "managed_fallback": False,
        }, ensure_ascii=False)

    try:
        entrypoint = str(PurePosixPath(script.resolve().relative_to(skill_root.resolve())))
        cwd_policy = _isolated_cwd_policy(
            workdir,
            script=script,
            skill_root=skill_root,
            user_id=user_id,
            session_id=session_id,
        )
    except (OSError, ValueError) as exc:
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "error_code": "unsupported_isolated_cwd",
            "invocation_mode": invocation_mode,
            "script_path": display_path,
            "isolated_execution": True,
            "managed_fallback": False,
        }, ensure_ascii=False)

    function_payload: dict[str, Any] | None = None
    instance_payload: dict[str, Any] | None = None
    function_fields_present = any(
        value is not None
        for value in (function_name, function_args, function_kwargs)
    )
    instance_fields_present = any(
        value is not None
        for value in (
            class_name,
            method_name,
            constructor_args,
            constructor_kwargs,
            method_args,
            method_kwargs,
        )
    )
    if function_fields_present and instance_fields_present:
        return json.dumps({
            "status": "error",
            "error": (
                "Top-level function mode and instance-method mode are mutually "
                "exclusive; provide exactly one callable target."
            ),
            "error_code": "mixed_invocation_modes",
            "invocation_mode": invocation_mode,
            "script_path": display_path,
        }, ensure_ascii=False)
    if function_name is not None:
        try:
            function_payload = _validated_function_payload(
                script,
                function_name=function_name,
                function_args=function_args,
                function_kwargs=function_kwargs,
                cli_args=args,
                skill_root=skill_root,
            )
        except ValueError as exc:
            return json.dumps({
                "status": "error",
                "error": str(exc),
                "invocation_mode": "function",
                "script_path": display_path,
                "function_name": function_name,
                "available_functions": _public_function_inventory(script),
            }, ensure_ascii=False)
    elif function_fields_present:
        return json.dumps({
            "status": "error",
            "error": "function_args/function_kwargs require function_name.",
            "invocation_mode": "cli",
            "script_path": display_path,
        }, ensure_ascii=False)
    elif instance_fields_present:
        try:
            instance_payload = _validated_instance_method_payload(
                script,
                class_name=class_name,
                method_name=method_name,
                constructor_args=constructor_args,
                constructor_kwargs=constructor_kwargs,
                method_args=method_args,
                method_kwargs=method_kwargs,
                cli_args=args,
                skill_root=skill_root,
            )
        except ValueError as exc:
            return json.dumps({
                "status": "error",
                "error": str(exc),
                "invocation_mode": "instance_method",
                "script_path": display_path,
                "class_name": class_name,
                "method_name": method_name,
                "available_classes": _public_class_inventory(script),
            }, ensure_ascii=False)
    try:
        snapshot = snapshot_skill_package(skill_root)
        profile_selection = select_skill_runtime_profile(
            snapshot,
            entrypoint,
        )
        if (
            expected_skill_sha256 is not None
            and profile_selection.package_sha256
            != expected_skill_sha256
        ):
            raise IsolatedSkillExecutorError(
                "skill_package_authority_mismatch",
                "The exact Skill package changed after capability "
                "compilation. Recompile before executing it.",
            )
        source = snapshot.read_bytes(entrypoint).decode(
            "utf-8",
            errors="replace",
        )
        proven_invocations: tuple[dict[str, Any], ...]
        if function_payload is not None and function_name is not None:
            parameters = bind_python_invocation_parameters(
                source,
                callable_name=function_name,
                positional=function_payload.get("args"),
                keywords=function_payload.get("kwargs"),
            )
            proven_invocations = (
                ({
                    "source": "python",
                    "callable": function_name,
                    "parameters": parameters,
                },)
                if parameters is not None
                else ()
            )
        elif (
            instance_payload is not None
            and class_name is not None
            and method_name is not None
        ):
            constructor_parameters = bind_python_invocation_parameters(
                source,
                callable_name=class_name,
                positional=instance_payload.get("constructor_args"),
                keywords=instance_payload.get("constructor_kwargs"),
            )
            method_callable = f"{class_name}.{method_name}"
            method_parameters = bind_python_invocation_parameters(
                source,
                callable_name=method_callable,
                positional=instance_payload.get("method_args"),
                keywords=instance_payload.get("method_kwargs"),
            )
            proven_invocations = (
                (
                    {
                        "source": "python",
                        "callable": class_name,
                        "parameters": constructor_parameters,
                    },
                    {
                        "source": "python",
                        "callable": method_callable,
                        "parameters": method_parameters,
                    },
                )
                if (
                    constructor_parameters is not None
                    and method_parameters is not None
                )
                else ()
            )
        else:
            proven_invocations = ({
                "source": "argv",
                "args": safe_cli_args,
            },)
        egress_policy = (
            invocation_bound_skill_egress_policy_for_invocations(
                context,
                skill_name,
                profile_selection,
                invocations=proven_invocations,
            )
        )
        egress_budget_binding = (
            session_sandbox_egress_budget_binding(
                context,
                operation="skill_python",
            )
            if egress_policy.rules
            else None
        )
    except (
        IsolatedSkillExecutorError,
        KeyError,
        SessionSandboxPolicyError,
        UnicodeError,
    ) as exc:
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "error_code": str(
                getattr(exc, "code", "skill_invocation_policy_invalid")
            ),
            "invocation_mode": invocation_mode,
            "script_path": display_path,
            "isolated_execution": True,
            "managed_fallback": False,
        }, ensure_ascii=False)
    try:
        isolated = await execute_isolated_skill_script(
            skill_root=skill_root,
            workspace=sandbox_dir(user_id, session_id, sub="workspace"),
            entrypoint=entrypoint,
            args=safe_cli_args,
            timeout=safe_timeout,
            cwd=cwd_policy,
            function_name=function_name,
            function_args=(function_payload or {}).get("args"),
            function_kwargs=(function_payload or {}).get("kwargs"),
            class_name=class_name,
            method_name=method_name,
            constructor_args=(instance_payload or {}).get("constructor_args"),
            constructor_kwargs=(instance_payload or {}).get("constructor_kwargs"),
            method_args=(instance_payload or {}).get("method_args"),
            method_kwargs=(instance_payload or {}).get("method_kwargs"),
            **(
                {"expected_skill_sha256": expected_skill_sha256}
                if expected_skill_sha256 is not None
                else {}
            ),
            **(
                {
                    "egress_rules": egress_policy.rule_payload(),
                    "private_origins": egress_policy.private_origins,
                    "budget_scope_sha256": (
                        egress_budget_binding.budget_scope_sha256
                    ),
                    "call_id_sha256": egress_budget_binding.call_id_sha256,
                }
                if egress_policy.rules else {}
            ),
            **(
                {
                    "execution_authority_check": lambda: (
                        require_execution_authority(
                            context,
                            boundary="skill_python.executor_commit",
                        )
                    )
                }
                if (
                    context is not None
                    and context.execution_fence is not None
                )
                else {}
            ),
        )
    except IsolatedSkillExecutorError as exc:
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "error_code": exc.code,
            "invocation_mode": invocation_mode,
            "function_name": function_name,
            "class_name": class_name,
            "method_name": method_name,
            "script_path": display_path,
            "cwd": cwd,
            "network": (
                "controlled_egress" if egress_policy.rules else "disabled"
            ),
            "isolated_execution": True,
            "managed_fallback": False,
            "artifacts": [],
        }, ensure_ascii=False)

    result = dict(isolated)
    result.update({
        "invocation_mode": invocation_mode,
        "script_path": display_path,
        "cwd": cwd,
        "runtime_status": "isolated_executor",
        "runtime_preflight": runtime_preflight,
        "managed_fallback": False,
        "isolated_execution": True,
        "workspace_output_dir": "workspace/output_result",
        "artifact_total": len(isolated.get("artifacts", []))
        if isinstance(isolated.get("artifacts"), list) else 0,
    })
    result["effect_receipt"] = build_isolated_execution_effect_receipt(
        result=isolated,
        egress_rules=egress_policy.rule_payload(),
        tool_operation_id=(
            context.tool_operation_id if context is not None else None
        ),
    )
    result.pop("egress_audit_receipt", None)
    if function_name is not None:
        result["function_name"] = function_name
    if class_name is not None and method_name is not None:
        result["class_name"] = class_name
        result["method_name"] = method_name
    return json.dumps(result, ensure_ascii=False)


async def run_managed_python_code(
    code: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Compatibility adapter for ad-hoc code; never executes in the harness."""

    if not code or not code.strip():
        return json.dumps({"status": "error", "error": "No code provided."}, ensure_ascii=False)
    try:
        safe_timeout = _validated_tool_timeout(timeout)
    except ValueError as exc:
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "error_code": "invalid_timeout",
            "isolated_execution": True,
            "managed_fallback": False,
        }, ensure_ascii=False)
    workspace = sandbox_dir(user_id, session_id, sub="workspace")
    try:
        result = await execute_isolated_session_code(
            workspace=workspace,
            code=code,
            timeout=safe_timeout,
        )
    except IsolatedSkillExecutorError as exc:
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "error_code": exc.code,
            "network": "disabled",
            "isolated_execution": True,
            "managed_fallback": False,
            "artifacts": [],
        }, ensure_ascii=False)
    result = dict(result)
    result.update({
        "isolated_execution": True,
        "managed_fallback": False,
        "runtime_status": "isolated_executor",
    })
    return json.dumps(result, ensure_ascii=False)


def _validated_function_payload(
    script: Path,
    *,
    function_name: str,
    function_args: list[Any] | None,
    function_kwargs: dict[str, Any] | None,
    cli_args: list[str] | None,
    skill_root: Path,
) -> dict[str, Any]:
    """Validate a declarative function call without evaluating user input."""
    if not isinstance(function_name, str) or not PUBLIC_FUNCTION_RE.fullmatch(function_name):
        raise ValueError(
            "function_name must be one public, non-dotted Python identifier "
            "(letters/digits/underscore; no private names)."
        )
    if function_name.startswith("_"):
        raise ValueError("Private function names are not callable.")
    if cli_args:
        raise ValueError("Do not combine CLI args with function_name; use function_args/function_kwargs.")
    try:
        script.resolve(strict=True).relative_to(skill_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        raise ValueError(
            "Function invocation is restricted to the exact selected installed Skill .py file."
        )
    positional = [] if function_args is None else function_args
    keywords = {} if function_kwargs is None else function_kwargs
    if not isinstance(positional, list):
        raise ValueError("function_args must be a JSON array.")
    if not isinstance(keywords, dict):
        raise ValueError("function_kwargs must be a JSON object.")
    if len(positional) > MAX_FUNCTION_ARGS:
        raise ValueError(f"function_args exceeds the {MAX_FUNCTION_ARGS}-item limit.")
    if len(keywords) > MAX_FUNCTION_KWARGS:
        raise ValueError(f"function_kwargs exceeds the {MAX_FUNCTION_KWARGS}-item limit.")
    for key in keywords:
        if not isinstance(key, str) or not PUBLIC_FUNCTION_RE.fullmatch(key) or key.startswith("_"):
            raise ValueError(f"Invalid public keyword argument name: {key!r}.")
    _validate_json_depth(positional, field="function_args")
    _validate_json_depth(keywords, field="function_kwargs")

    inventory = _public_function_inventory(script, strict=True)
    if function_name not in {item["name"] for item in inventory}:
        raise ValueError(
            f"Public top-level function {function_name!r} is not declared by the selected Skill script."
        )
    try:
        encoded = json.dumps(
            {"args": positional, "kwargs": keywords},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Function arguments must contain only valid JSON values: {exc}") from exc
    if len(encoded) > MAX_FUNCTION_INPUT_BYTES:
        raise ValueError(
            f"Function argument JSON exceeds the {MAX_FUNCTION_INPUT_BYTES}-byte limit."
        )
    return {"args": positional, "kwargs": keywords}


def _validated_instance_method_payload(
    script: Path,
    *,
    class_name: str | None,
    method_name: str | None,
    constructor_args: list[Any] | None,
    constructor_kwargs: dict[str, Any] | None,
    method_args: list[Any] | None,
    method_kwargs: dict[str, Any] | None,
    cli_args: list[str] | None,
    skill_root: Path,
) -> dict[str, Any]:
    """Validate a declarative call to one direct public instance method."""

    for field, value in (("class_name", class_name), ("method_name", method_name)):
        if (
            not isinstance(value, str)
            or value.startswith("_")
            or not PUBLIC_FUNCTION_RE.fullmatch(value)
        ):
            raise ValueError(
                f"{field} must be one public, non-dotted Python identifier."
            )
    if cli_args:
        raise ValueError(
            "Do not combine CLI args with class_name/method_name; use "
            "constructor_args/constructor_kwargs and method_args/method_kwargs."
        )
    try:
        script.resolve(strict=True).relative_to(skill_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        raise ValueError(
            "Instance-method invocation is restricted to an installed Skill .py "
            "file in the exact selected package."
        )

    init_positional = [] if constructor_args is None else constructor_args
    init_keywords = {} if constructor_kwargs is None else constructor_kwargs
    call_positional = [] if method_args is None else method_args
    call_keywords = {} if method_kwargs is None else method_kwargs
    for field, value in (
        ("constructor_args", init_positional),
        ("method_args", call_positional),
    ):
        if not isinstance(value, list):
            raise ValueError(f"{field} must be a JSON array.")
    for field, value in (
        ("constructor_kwargs", init_keywords),
        ("method_kwargs", call_keywords),
    ):
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be a JSON object.")
    if len(init_positional) + len(call_positional) > MAX_FUNCTION_ARGS:
        raise ValueError(
            "Combined constructor_args and method_args exceed the "
            f"{MAX_FUNCTION_ARGS}-item limit."
        )
    if len(init_keywords) + len(call_keywords) > MAX_FUNCTION_KWARGS:
        raise ValueError(
            "Combined constructor_kwargs and method_kwargs exceed the "
            f"{MAX_FUNCTION_KWARGS}-key limit."
        )
    for field, keywords in (
        ("constructor_kwargs", init_keywords),
        ("method_kwargs", call_keywords),
    ):
        for key in keywords:
            if (
                not isinstance(key, str)
                or key.startswith("_")
                or not PUBLIC_FUNCTION_RE.fullmatch(key)
            ):
                raise ValueError(f"Invalid public keyword in {field}: {key!r}.")
    for field, value in (
        ("constructor_args", init_positional),
        ("constructor_kwargs", init_keywords),
        ("method_args", call_positional),
        ("method_kwargs", call_keywords),
    ):
        _validate_json_depth(value, field=field)

    inventory = _public_class_inventory(script, strict=True)
    matching_classes = [item for item in inventory if item["name"] == class_name]
    if len(matching_classes) != 1:
        raise ValueError(
            f"Public top-level class {class_name!r} is not uniquely declared by "
            "the selected Skill script."
        )
    methods = matching_classes[0].get("methods", [])
    matching_methods = [item for item in methods if item.get("name") == method_name]
    if len(matching_methods) != 1:
        raise ValueError(
            f"Direct public instance method {class_name}.{method_name} is not "
            "declared by the selected Skill script. Static, class, property, "
            "decorated, inherited, and private methods are not callable through "
            "this interface."
        )

    payload = {
        "constructor_args": init_positional,
        "constructor_kwargs": init_keywords,
        "method_args": call_positional,
        "method_kwargs": call_keywords,
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Instance-method arguments must contain only finite JSON values: {exc}"
        ) from exc
    if len(encoded) > MAX_FUNCTION_INPUT_BYTES:
        raise ValueError(
            "Instance-method argument JSON exceeds the "
            f"{MAX_FUNCTION_INPUT_BYTES}-byte limit."
        )
    return payload


def _validate_json_depth(value: Any, *, field: str, depth: int = 0) -> None:
    if depth > MAX_FUNCTION_JSON_DEPTH:
        raise ValueError(f"{field} exceeds the maximum JSON nesting depth {MAX_FUNCTION_JSON_DEPTH}.")
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_depth(item, field=field, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} JSON object keys must be strings.")
            _validate_json_depth(item, field=field, depth=depth + 1)
        return
    raise ValueError(f"{field} contains a non-JSON value of type {type(value).__name__}.")


def _public_function_inventory(script: Path, *, strict: bool = False) -> list[dict[str, Any]]:
    """Return bounded, non-executing metadata for public top-level functions."""
    try:
        if script.stat().st_size > MAX_FUNCTION_SOURCE_BYTES:
            raise ValueError(
                f"Skill script exceeds the {MAX_FUNCTION_SOURCE_BYTES}-byte function-inspection limit."
            )
        source = script.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(script))
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        if strict:
            raise ValueError(f"Cannot inspect Skill functions safely: {exc}") from exc
        return []
    functions: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_") or not PUBLIC_FUNCTION_RE.fullmatch(node.name):
            continue
        try:
            arguments = ast.unparse(node.args)
        except (AttributeError, ValueError):
            arguments = "..."
        doc = ast.get_docstring(node, clean=True) or ""
        functions.append({
            "name": node.name,
            "signature": _truncate(f"{node.name}({arguments})", 500),
            "async": isinstance(node, ast.AsyncFunctionDef),
            "description": _truncate(doc.splitlines()[0] if doc else "", 240),
        })
        if len(functions) >= MAX_PUBLIC_FUNCTIONS:
            break
    return functions


def _public_class_inventory(script: Path, *, strict: bool = False) -> list[dict[str, Any]]:
    """Return bounded AST metadata for directly callable instance methods."""

    try:
        if script.stat().st_size > MAX_FUNCTION_SOURCE_BYTES:
            raise ValueError(
                f"Skill script exceeds the {MAX_FUNCTION_SOURCE_BYTES}-byte "
                "class-inspection limit."
            )
        source = script.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(script))
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        if strict:
            raise ValueError(f"Cannot inspect Skill classes safely: {exc}") from exc
        return []

    classes: list[dict[str, Any]] = []
    for class_node in tree.body:
        if not isinstance(class_node, ast.ClassDef):
            continue
        if (
            class_node.name.startswith("_")
            or not PUBLIC_FUNCTION_RE.fullmatch(class_node.name)
        ):
            continue
        methods: list[dict[str, Any]] = []
        constructor_signature = f"{class_node.name}(...)"
        for node in class_node.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "__init__" and not node.decorator_list:
                try:
                    constructor_signature = _truncate(
                        f"{class_node.name}({ast.unparse(node.args)})", 500
                    )
                except (AttributeError, ValueError):
                    pass
                continue
            positional_parameters = [*node.args.posonlyargs, *node.args.args]
            if (
                node.name.startswith("_")
                or not PUBLIC_FUNCTION_RE.fullmatch(node.name)
                or node.decorator_list
                or not positional_parameters
            ):
                continue
            try:
                arguments = ast.unparse(node.args)
            except (AttributeError, ValueError):
                arguments = "..."
            doc = ast.get_docstring(node, clean=True) or ""
            methods.append({
                "name": node.name,
                "signature": _truncate(
                    f"{class_node.name}.{node.name}({arguments})", 500
                ),
                "async": isinstance(node, ast.AsyncFunctionDef),
                "description": _truncate(
                    doc.splitlines()[0] if doc else "", 240
                ),
            })
            if len(methods) >= MAX_PUBLIC_METHODS_PER_CLASS:
                break
        if not methods:
            continue
        doc = ast.get_docstring(class_node, clean=True) or ""
        classes.append({
            "name": class_node.name,
            "constructor_signature": constructor_signature,
            "description": _truncate(doc.splitlines()[0] if doc else "", 240),
            "methods": methods,
        })
        if len(classes) >= MAX_PUBLIC_CLASSES:
            break
    return classes


def inspect_public_python_callables(script: Path) -> dict[str, list[dict[str, Any]]]:
    """Return strict, bounded callable metadata without importing the script.

    Callers must resolve and authorize ``script`` before using this helper. It
    only performs the same bounded AST inspection used by
    ``run_skill_python`` preflight; Skill code is never imported or executed.
    Parse, encoding, size, and filesystem failures are reported as
    ``ValueError`` so a guidance-only caller can replace the inventory with a
    stable unavailable marker without exposing host details.
    """

    if not isinstance(script, Path) or script.suffix.casefold() != ".py":
        raise ValueError("Public callable inventory requires an exact .py file.")
    return {
        "functions": _public_function_inventory(script, strict=True),
        "classes": _public_class_inventory(script, strict=True),
    }


def _validated_cli_args(args: list[str] | None) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list) or len(args) > MAX_CLI_ARGS:
        raise ValueError(
            f"args must be a JSON array with at most {MAX_CLI_ARGS} strings."
        )
    normalized: list[str] = []
    total_bytes = 0
    for index, item in enumerate(args):
        if not isinstance(item, str):
            raise ValueError(f"args[{index}] must be a string.")
        if "\x00" in item or len(item) > MAX_CLI_ARG_CHARS:
            raise ValueError(
                f"args[{index}] contains NUL or exceeds {MAX_CLI_ARG_CHARS} characters."
            )
        total_bytes += len(item.encode("utf-8"))
        if total_bytes > MAX_CLI_ARG_BYTES:
            raise ValueError(
                f"args exceeds the {MAX_CLI_ARG_BYTES}-byte aggregate limit."
            )
        normalized.append(item)
    return normalized


def _validated_tool_timeout(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_TIMEOUT:
        raise ValueError(f"timeout must be an integer between 1 and {MAX_TIMEOUT} seconds.")
    return value


async def _run_python_script(
    script: Path,
    *,
    args: list[str] | None,
    timeout: int,
    cwd: Path,
    user_id: str,
    session_id: str,
    managed_fallback: bool = False,
) -> str:
    # Kept only as a fail-closed compatibility symbol for older imports. User
    # or Skill code must never execute in the harness process/container.
    return json.dumps({
        "status": "error",
        "error": "Direct managed Python execution is disabled; use the isolated executor.",
        "error_code": "direct_python_execution_disabled",
        "invocation_mode": "cli",
        "isolated_execution": True,
        "managed_fallback": False,
    }, ensure_ascii=False)

    # Unreachable legacy implementation retained temporarily for source-level
    # compatibility while callers migrate to execute_isolated_session_code.
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    lifecycle: dict[str, Any] = {"runtime_status": None}
    try:
        return await asyncio.wait_for(
            _initialize_and_run_python_script(
                script,
                args=args,
                cwd=cwd,
                user_id=user_id,
                session_id=session_id,
                managed_fallback=managed_fallback,
                lifecycle=lifecycle,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return json.dumps({
            "status": "error",
            "error": f"Operation timed out after {timeout}s during runtime initialization or script execution.",
            "timeout_scope": "runtime_initialization_and_script",
            "invocation_mode": "cli",
            "runtime_status": lifecycle.get("runtime_status"),
            "script_path": _display_path(script, user_id, session_id),
            "cwd": _display_path(cwd, user_id, session_id),
        }, ensure_ascii=False)


async def _initialize_and_run_python_script(
    script: Path,
    *,
    args: list[str] | None,
    cwd: Path,
    user_id: str,
    session_id: str,
    managed_fallback: bool,
    lifecycle: dict[str, Any],
) -> str:
    raise RuntimeError(
        "Direct managed Python execution is disabled; use the isolated executor."
    )

    target_skill_dir = _owning_session_skill_dir(script, user_id, session_id)
    if target_skill_dir is None:
        runtime = await ensure_session_runtime(user_id, session_id)
    else:
        runtime = await ensure_session_runtime(
            user_id,
            session_id,
            target_skill_dir=target_skill_dir,
        )
    lifecycle["runtime_status"] = runtime.get("status")
    if runtime.get("status") != "ready":
        return json.dumps({
            "status": "error",
            "error": "Python runtime is not ready.",
            "invocation_mode": "cli",
            "runtime_status": runtime.get("status"),
            "runtime_error": runtime.get("error"),
            "policy": runtime.get("policy"),
        }, ensure_ascii=False)
    python = resolve_session_python(runtime) or os.sys.executable
    safe_args = _validated_cli_args(args)
    before_artifacts = _workspace_artifact_snapshot(user_id, session_id)
    env = _runtime_env(runtime, user_id=user_id, session_id=session_id, script=script)
    proc = await asyncio.create_subprocess_exec(
        python,
        str(script),
        *safe_args,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **({"start_new_session": True} if os.name == "posix" else {}),
    )
    try:
        stdout_b, stderr_b, stdout_truncated, stderr_truncated = (
            await _communicate_capped(
                proc,
                b"",
                stdout_limit=MAX_STDOUT,
                stderr_limit=MAX_STDERR,
            )
        )
    except asyncio.CancelledError:
        await _terminate_script_subprocess(proc)
        raise
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    artifacts = _workspace_artifact_changes(user_id, session_id, before_artifacts)
    return json.dumps({
        "status": "success" if proc.returncode == 0 else "error",
        "returncode": proc.returncode,
        "invocation_mode": "cli",
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "script_path": _display_path(script, user_id, session_id),
        "cwd": _display_path(cwd, user_id, session_id),
        "runtime_status": runtime.get("status"),
        "env_hash": runtime.get("env_hash"),
        "managed_fallback": managed_fallback,
        "workspace_output_dir": "workspace/output_result",
        "artifacts": artifacts,
        "artifact_limit": MAX_ARTIFACTS,
    }, ensure_ascii=False)


async def _run_python_function(
    script: Path,
    *,
    payload: bytes,
    function_name: str,
    timeout: int,
    cwd: Path,
    user_id: str,
    session_id: str,
) -> str:
    return json.dumps({
        "status": "error",
        "error": "Direct managed Skill function execution is disabled; use the isolated executor.",
        "error_code": "direct_python_execution_disabled",
        "invocation_mode": "function",
        "function_name": function_name,
        "isolated_execution": True,
        "managed_fallback": False,
    }, ensure_ascii=False)

    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    lifecycle: dict[str, Any] = {
        "runtime_status": None,
        "function_name": function_name,
    }
    try:
        return await asyncio.wait_for(
            _initialize_and_run_python_function(
                script,
                payload=payload,
                cwd=cwd,
                user_id=user_id,
                session_id=session_id,
                lifecycle=lifecycle,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return json.dumps({
            "status": "error",
            "error": f"Operation timed out after {timeout}s during runtime initialization or function execution.",
            "timeout_scope": "runtime_initialization_and_function",
            "invocation_mode": "function",
            "runtime_status": lifecycle.get("runtime_status"),
            "script_path": _display_path(script, user_id, session_id),
            "function_name": lifecycle.get("function_name"),
            "cwd": _display_path(cwd, user_id, session_id),
        }, ensure_ascii=False)


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_function_result_directory(workspace: Path, *, create: bool) -> int:
    """Open ``workspace/.chatds/function_calls`` without following links.

    The returned descriptor refers to the directory actually authorized by
    each parent descriptor, rather than to a path resolved before a possible
    replacement.  Callers own and must close the descriptor.
    """

    if not workspace.is_absolute() or workspace.is_symlink():
        raise ValueError("Managed workspace is linked or not absolute.")
    flags = _directory_open_flags()
    try:
        current_fd = os.open(workspace, flags)
    except OSError as exc:
        raise ValueError("Managed workspace is missing, linked, or not a directory.") from exc
    try:
        for component in FUNCTION_RESULT_COMPONENTS:
            if create:
                try:
                    os.mkdir(component, mode=0o755, dir_fd=current_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ValueError(
                        "Managed function-call directory could not be created safely."
                    ) from exc
            try:
                child_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                raise ValueError(
                    "Managed function-call directory contains a linked or non-directory component."
                ) from exc
            try:
                if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                    raise ValueError(
                        "Managed function-call directory contains a non-directory component."
                    )
            except Exception:
                os.close(child_fd)
                raise
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _prepare_function_result_directory(workspace: Path) -> Path:
    directory_fd = _open_function_result_directory(workspace, create=True)
    try:
        # The managed runner may use a session interpreter with a different
        # group policy, so retain the previous readable-directory behavior via
        # the already-authorized descriptor rather than chmod'ing a path.
        os.fchmod(directory_fd, 0o755)
    finally:
        os.close(directory_fd)
    return workspace.joinpath(*FUNCTION_RESULT_COMPONENTS)


def _function_result_error(message: str) -> dict[str, Any]:
    return {"status": "error", "error": message}


def _consume_function_result_envelope(workspace: Path, result_name: str) -> dict[str, Any]:
    """Read and unlink one result envelope through no-follow descriptors."""

    if not result_name or "/" in result_name or result_name in {".", ".."}:
        return _function_result_error("Managed function result envelope name is invalid.")
    directory_fd: int | None = None
    result_fd: int | None = None
    opened_identity: tuple[int, int] | None = None
    try:
        directory_fd = _open_function_result_directory(workspace, create=False)
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            result_fd = os.open(result_name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return _function_result_error(
                "Managed function runner did not produce a result envelope."
            )
        except OSError as exc:
            return _function_result_error(
                f"Could not read managed function result envelope safely: {type(exc).__name__}: {exc}"
            )
        result_stat = os.fstat(result_fd)
        opened_identity = (result_stat.st_dev, result_stat.st_ino)
        if not stat.S_ISREG(result_stat.st_mode):
            return _function_result_error(
                "Managed function result envelope is not a regular file."
            )
        if result_stat.st_size > MAX_RUNNER_ENVELOPE_BYTES:
            return _function_result_error(
                "Managed function result envelope exceeded the "
                f"{MAX_RUNNER_ENVELOPE_BYTES}-byte limit."
            )
        chunks: list[bytes] = []
        remaining = MAX_RUNNER_ENVELOPE_BYTES + 1
        while remaining:
            chunk = os.read(result_fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_RUNNER_ENVELOPE_BYTES:
            return _function_result_error(
                "Managed function result envelope exceeded the "
                f"{MAX_RUNNER_ENVELOPE_BYTES}-byte limit."
            )
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            return _function_result_error(
                f"Could not read managed function result envelope: {type(exc).__name__}: {exc}"
            )
        if not isinstance(parsed, dict):
            return _function_result_error(
                "Managed function runner returned a non-object result envelope."
            )
        return parsed
    except (OSError, ValueError) as exc:
        return _function_result_error(
            f"Could not access managed function result directory safely: {type(exc).__name__}: {exc}"
        )
    finally:
        if result_fd is not None:
            try:
                os.close(result_fd)
            except OSError:
                pass
        if directory_fd is not None:
            # Delete only the exact inode we opened.  A concurrently replaced
            # name is left untouched rather than following or deleting it.
            if opened_identity is not None:
                try:
                    current = os.stat(result_name, dir_fd=directory_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == opened_identity:
                        os.unlink(result_name, dir_fd=directory_fd)
                except OSError:
                    pass
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _discard_function_result_envelope(workspace: Path, result_name: str) -> None:
    """Best-effort secure cleanup used when a function run is cancelled."""

    directory_fd: int | None = None
    try:
        directory_fd = _open_function_result_directory(workspace, create=False)
        current = os.stat(result_name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(current.st_mode):
            os.unlink(result_name, dir_fd=directory_fd)
    except (OSError, ValueError):
        pass
    finally:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass


async def _initialize_and_run_python_function(
    script: Path,
    *,
    payload: bytes,
    cwd: Path,
    user_id: str,
    session_id: str,
    lifecycle: dict[str, Any],
) -> str:
    raise RuntimeError(
        "Direct managed Skill function execution is disabled; use the isolated executor."
    )

    function_name = str(lifecycle.get("function_name") or "")
    if not function_name:
        return json.dumps({
            "status": "error",
            "error": "Internal function invocation metadata is missing.",
            "invocation_mode": "function",
        }, ensure_ascii=False)
    target_skill_dir = _owning_session_skill_dir(script, user_id, session_id)
    if target_skill_dir is None:
        return json.dumps({
            "status": "error",
            "error": "Function invocation target is no longer inside an installed session Skill.",
            "invocation_mode": "function",
            "function_name": function_name,
        }, ensure_ascii=False)
    runtime = await ensure_session_runtime(
        user_id,
        session_id,
        target_skill_dir=target_skill_dir,
    )
    lifecycle["runtime_status"] = runtime.get("status")
    if runtime.get("status") != "ready":
        return json.dumps({
            "status": "error",
            "error": "Python runtime is not ready.",
            "invocation_mode": "function",
            "function_name": function_name,
            "runtime_status": runtime.get("status"),
            "runtime_error": runtime.get("error"),
            "policy": runtime.get("policy"),
        }, ensure_ascii=False)
    if not FUNCTION_RUNNER.is_file():
        return json.dumps({
            "status": "error",
            "error": "Managed Skill function runner is unavailable.",
            "invocation_mode": "function",
            "function_name": function_name,
            "runtime_status": runtime.get("status"),
        }, ensure_ascii=False)

    python = resolve_session_python(runtime) or os.sys.executable
    workspace = sandbox_dir(user_id, session_id, sub="workspace")
    try:
        result_dir = _prepare_function_result_directory(workspace)
    except (OSError, ValueError) as exc:
        return json.dumps({
            "status": "error",
            "error": f"Managed function-call result directory is unsafe: {type(exc).__name__}: {exc}",
            "invocation_mode": "function",
            "function_name": function_name,
            "runtime_status": runtime.get("status"),
        }, ensure_ascii=False)
    result_name = f"{uuid.uuid4().hex}.json"
    result_path = result_dir / result_name
    before_artifacts = _workspace_artifact_snapshot(user_id, session_id)
    env = _runtime_env(runtime, user_id=user_id, session_id=session_id, script=script)
    proc = await asyncio.create_subprocess_exec(
        python,
        str(FUNCTION_RUNNER),
        "--script",
        str(script),
        "--function",
        function_name,
        "--result-file",
        str(result_path),
        "--max-result-chars",
        str(MAX_FUNCTION_RESULT_CHARS),
        "--max-stdout-chars",
        str(MAX_STDOUT),
        "--max-stderr-chars",
        str(MAX_STDERR),
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **({"start_new_session": True} if os.name == "posix" else {}),
    )
    try:
        (
            raw_stdout_b,
            raw_stderr_b,
            raw_stdout_truncated,
            raw_stderr_truncated,
        ) = await _communicate_capped(
            proc,
            payload,
            stdout_limit=MAX_STDOUT,
            stderr_limit=MAX_STDERR,
        )
    except asyncio.CancelledError:
        await _terminate_script_subprocess(proc)
        _discard_function_result_envelope(workspace, result_name)
        raise

    raw_stdout = raw_stdout_b.decode("utf-8", errors="replace")
    raw_stderr = raw_stderr_b.decode("utf-8", errors="replace")
    artifacts = _workspace_artifact_changes(user_id, session_id, before_artifacts)
    envelope = _consume_function_result_envelope(workspace, result_name)

    captured_stdout = envelope.get("stdout") if isinstance(envelope.get("stdout"), str) else ""
    captured_stderr = envelope.get("stderr") if isinstance(envelope.get("stderr"), str) else ""
    combined_stdout_truncated = bool(captured_stdout and raw_stdout) and (
        len(captured_stdout) + 1 + len(raw_stdout) > MAX_STDOUT
    )
    combined_stderr_truncated = bool(captured_stderr and raw_stderr) and (
        len(captured_stderr) + 1 + len(raw_stderr) > MAX_STDERR
    )
    envelope.update({
        "returncode": proc.returncode,
        "invocation_mode": "function",
        "function_name": function_name,
        "stdout": _join_captured_output(captured_stdout, raw_stdout, MAX_STDOUT),
        "stderr": _join_captured_output(captured_stderr, raw_stderr, MAX_STDERR),
        "stdout_truncated": bool(envelope.get("stdout_truncated"))
        or raw_stdout_truncated
        or combined_stdout_truncated,
        "stderr_truncated": bool(envelope.get("stderr_truncated"))
        or raw_stderr_truncated
        or combined_stderr_truncated,
        "script_path": _display_path(script, user_id, session_id),
        "cwd": _display_path(cwd, user_id, session_id),
        "runtime_status": runtime.get("status"),
        "env_hash": runtime.get("env_hash"),
        "managed_fallback": False,
        "workspace_output_dir": "workspace/output_result",
        "artifacts": artifacts,
        "artifact_limit": MAX_ARTIFACTS,
    })
    if proc.returncode not in (0, None) and envelope.get("status") == "success":
        envelope["status"] = "error"
        envelope["error"] = f"Managed function runner exited with return code {proc.returncode}."
    return json.dumps(envelope, ensure_ascii=False)


async def _communicate_capped(
    proc: asyncio.subprocess.Process,
    payload: bytes,
    *,
    stdout_limit: int,
    stderr_limit: int,
) -> tuple[bytes, bytes, bool, bool]:
    """Drain child pipes while retaining only bounded diagnostic output."""
    # Small process doubles used by lifecycle tests and older adapters expose
    # only ``communicate``. Real subprocesses always take the bounded streaming
    # branch below.
    if not hasattr(proc, "stdout") or not hasattr(proc, "stderr"):
        stdout, stderr = await proc.communicate()
        return (
            stdout[:stdout_limit],
            stderr[:stderr_limit],
            len(stdout) > stdout_limit,
            len(stderr) > stderr_limit,
        )
    if getattr(proc, "stdin", None) is not None:
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    async def read_bounded(
        stream: asyncio.StreamReader | None,
        limit: int,
    ) -> tuple[bytes, bool]:
        if stream is None:
            return b"", False
        kept = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            remaining = max(0, limit - len(kept))
            if remaining:
                kept.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        if truncated:
            kept.extend(b"\n... [truncated]")
        return bytes(kept), truncated

    stdout_task = asyncio.create_task(read_bounded(proc.stdout, stdout_limit))
    stderr_task = asyncio.create_task(read_bounded(proc.stderr, stderr_limit))
    try:
        stdout_result, stderr_result = await asyncio.gather(stdout_task, stderr_task)
    except asyncio.CancelledError:
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    await proc.wait()
    return stdout_result[0], stderr_result[0], stdout_result[1], stderr_result[1]


def _join_captured_output(captured: str, raw: str, limit: int) -> str:
    if captured and raw:
        return _truncate(captured + "\n" + raw, limit)
    return _truncate(captured or raw, limit)


async def _terminate_script_subprocess(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            process_id = getattr(proc, "pid", None)
            if os.name == "posix" and process_id:
                os.killpg(process_id, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (
        asyncio.TimeoutError,
        BrokenPipeError,
        ConnectionResetError,
        ProcessLookupError,
        ChildProcessError,
    ):
        pass


def _owning_session_skill_dir(
    script: Path,
    user_id: str,
    session_id: str,
) -> Path | None:
    """Return the nearest session Skill owning *script*, else ``None``.

    Workspace-managed Python intentionally returns ``None`` so its historical
    session-wide dependency environment remains unchanged.
    """
    session_root = (USER_SKILLS_BASE / user_id / session_id).resolve()
    resolved = script.resolve()
    try:
        resolved.relative_to(session_root)
    except ValueError:
        return None

    current = resolved.parent
    while current != session_root:
        if (current / "SKILL.md").is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    if (session_root / "SKILL.md").is_file():
        return session_root
    return None


def _resolve_script(script_path: str, user_id: str, session_id: str) -> Path:
    if not script_path or not script_path.strip():
        raise ValueError("script_path is required.")
    path_text = script_path.strip()
    if path_text.startswith("workspace/"):
        rel = path_text[len("workspace/"):]
        path = validate_path(rel, user_id, session_id, sub="workspace", must_exist=True)
    elif path_text.startswith("skills/"):
        path = _resolve_skill_path(path_text[len("skills/"):], user_id, session_id)
    else:
        try:
            path = validate_path(path_text, user_id, session_id, sub="workspace", must_exist=True)
        except FileNotFoundError:
            path = _resolve_unique_skill_script(path_text, (USER_SKILLS_BASE / user_id / session_id).resolve())
    if path.suffix != ".py":
        raise ValueError("script_path must point to a .py file.")
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"Script not found: {script_path}")
    return path


def _resolve_skill_path(rel_text: str, user_id: str, session_id: str) -> Path:
    rel = PurePosixPath(rel_text)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("Skill path escapes the current session skill directory.")
    root = (USER_SKILLS_BASE / user_id / session_id).resolve()
    if len(rel.parts) < 2:
        return _resolve_unique_skill_script(rel_text, root)
    lexical_candidate = root / Path(*rel.parts)
    if _path_has_symlink_component(lexical_candidate, root):
        raise ValueError("Symlinks are not allowed in Skill script paths.")
    candidate = lexical_candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("Skill path escapes the current session skill directory.")
    if not candidate.exists():
        matches = _skill_script_matches(rel_text, root)
        if matches:
            choices = ", ".join(_display_skill_candidate(path, root) for path in matches[:10])
            raise FileNotFoundError(
                f"Skill script not found: skills/{rel_text}. Did you mean one of: {choices}"
            )
        raise FileNotFoundError(f"Skill script not found: skills/{rel_text}")
    return candidate


def _resolve_unique_skill_script(rel_text: str, root: Path) -> Path:
    matches = _skill_script_matches(rel_text, root)
    if not matches:
        raise ValueError(
            "Skill paths must be skills/<skill>/<relative.py>, or skills/<script.py> "
            "when that script name is unique in the current session skills."
        )
    if len(matches) > 1:
        choices = ", ".join(_display_skill_candidate(path, root) for path in matches[:10])
        raise ValueError(
            f"Ambiguous skill script path skills/{rel_text}. Use a full path such as: {choices}"
        )
    return matches[0]


def _skill_script_matches(rel_text: str, root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    name = PurePosixPath(rel_text).name
    if not name.endswith(".py"):
        return []
    matches: list[Path] = []
    for path in sorted(root.rglob(name)):
        if _path_has_symlink_component(path, root):
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except ValueError:
            continue
        if not resolved.is_symlink() and resolved.is_file():
            matches.append(resolved)
    return matches


def _path_has_symlink_component(path: Path, root: Path) -> bool:
    """Reject a linked leaf or ancestor before ``resolve()`` hides it."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _display_skill_candidate(path: Path, root: Path) -> str:
    try:
        return "skills/" + str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def _available_skill_scripts(
    user_id: str,
    session_id: str,
    enabled_user_skills: list[str] | None = None,
) -> list[str]:
    """List only canonical visible Python entrypoints, never scope trees."""
    from skills.scanner import find_all_skills, skill_runnable_script_resources

    executable_user_skills = (
        list(enabled_user_skills) if enabled_user_skills is not None else []
    )
    result: list[str] = []
    for record in find_all_skills(
        user_id,
        session_id,
        enabled_user_skills=executable_user_skills,
    ):
        skill_name = str(record.get("name") or "")
        if not skill_name:
            continue
        result.extend(
            f"skills/{skill_name}/{relative_path}"
            for relative_path, _digest in skill_runnable_script_resources(
                skill_name,
                user_id,
                session_id,
                executable_user_skills,
            )
            if PurePosixPath(relative_path).suffix.casefold() == ".py"
        )
        if len(result) >= 40:
            return result[:40]
    return result


def _resolve_selected_skill_cwd(
    cwd: str,
    *,
    user_id: str,
    session_id: str,
    script: Path,
    skill_root: Path,
    skill_name: str,
) -> Path:
    """Resolve a cwd only to one of the sidecar's three canonical roots."""
    if cwd == "workspace":
        return sandbox_dir(user_id, session_id, sub="workspace")
    if cwd == "script":
        return script.parent
    if cwd in {"skill", f"skill:{skill_name}"}:
        return skill_root
    raise ValueError(
        "cwd must be workspace, script, or skill:<selected-skill>."
    )


def _selected_skill_display_path(
    script: Path,
    *,
    skill_root: Path,
    skill_name: str,
) -> str:
    relative = PurePosixPath(
        script.resolve(strict=True).relative_to(skill_root.resolve(strict=True))
    )
    return f"skills/{skill_name}/{relative}"


def _resolve_cwd(cwd: str, user_id: str, session_id: str, script: Path) -> Path:
    if cwd == "workspace":
        return sandbox_dir(user_id, session_id, sub="workspace")
    if cwd == "script":
        return script.parent
    if cwd.startswith("skill:"):
        skill = cwd.split(":", 1)[1].strip()
        if not skill or "/" in skill or ".." in skill:
            raise ValueError("Invalid skill cwd.")
        root = (USER_SKILLS_BASE / user_id / session_id / skill).resolve()
        session_root = (USER_SKILLS_BASE / user_id / session_id).resolve()
        try:
            root.relative_to(session_root)
        except ValueError:
            raise ValueError("Skill cwd escapes the current session.")
        if not root.is_dir():
            raise FileNotFoundError(f"Skill cwd not found: {cwd}")
        return root
    if cwd.startswith("workspace/"):
        cwd = cwd[len("workspace/"):]
    return validate_path(cwd, user_id, session_id, sub="workspace", must_exist=True)


def _isolated_cwd_policy(
    workdir: Path,
    *,
    script: Path,
    skill_root: Path,
    user_id: str,
    session_id: str,
) -> str:
    """Map a resolved directory to a sidecar-owned, non-host cwd policy.

    The isolated executor never receives a host path.  Supporting only these
    three canonical roots keeps cwd selection declarative and prevents a
    future caller from accidentally reintroducing direct harness execution.
    """

    resolved = workdir.resolve(strict=True)
    workspace = sandbox_dir(user_id, session_id, sub="workspace").resolve(strict=True)
    if resolved == workspace:
        return "workspace"
    if resolved == script.parent.resolve(strict=True):
        return "script"
    if resolved == skill_root.resolve(strict=True):
        return "skill"
    raise ValueError(
        "The isolated Skill executor supports cwd=workspace, cwd=script, or the "
        "selected Skill root (cwd=skill:<name>); arbitrary host subdirectories are forbidden."
    )


def _workspace_artifact_snapshot(user_id: str, session_id: str) -> dict[str, tuple[int, int]]:
    workspace = sandbox_dir(user_id, session_id, sub="workspace").resolve()
    snapshot: dict[str, tuple[int, int]] = {}
    for path in _iter_workspace_artifact_files(workspace):
        try:
            stat = path.stat()
            rel = str(PurePosixPath(path.resolve().relative_to(workspace)))
        except (OSError, ValueError):
            continue
        snapshot[rel] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _workspace_artifact_changes(
    user_id: str,
    session_id: str,
    before: dict[str, tuple[int, int]],
) -> list[dict[str, Any]]:
    workspace = sandbox_dir(user_id, session_id, sub="workspace").resolve()
    changes: list[dict[str, Any]] = []
    for path in _iter_workspace_artifact_files(workspace):
        try:
            resolved = path.resolve()
            rel = str(PurePosixPath(resolved.relative_to(workspace)))
            stat = resolved.stat()
        except (OSError, ValueError):
            continue
        current = (stat.st_size, stat.st_mtime_ns)
        if before.get(rel) == current:
            continue
        _make_workspace_path_readable(resolved)
        changes.append({
            "kind": "file",
            "path": rel,
            "title": resolved.name,
            "size_bytes": stat.st_size,
            "source": "workspace_diff",
        })
    changes.sort(key=lambda item: (0 if str(item.get("path", "")).startswith("output_result/") else 1, str(item.get("path", ""))))
    return changes[:MAX_ARTIFACTS]


def _iter_workspace_artifact_files(workspace: Path):
    if not workspace.is_dir():
        return
    examined = 0
    for walk_root, dirs, files in os.walk(workspace, followlinks=False):
        root = Path(walk_root)
        dirs[:] = sorted(
            name for name in dirs
            if name not in ARTIFACT_SKIP_DIRS and not (root / name).is_symlink()
        )
        for name in sorted(files):
            examined += 1
            if examined > MAX_ARTIFACT_SCAN_FILES:
                return
            path = root / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                path.resolve().relative_to(workspace)
            except (OSError, RuntimeError, ValueError):
                continue
            yield path


def _safe_env() -> dict[str, str]:
    keep = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR"}
    return {key: value for key, value in os.environ.items() if key in keep or key.startswith("XDG_")}


def _runtime_env(runtime: dict[str, Any], *, user_id: str, session_id: str, script: Path) -> dict[str, str]:
    env = runtime_env_for_subprocess(runtime, _safe_env(), user_id=user_id, session_id=session_id)
    workspace = sandbox_dir(user_id, session_id, sub="workspace")
    output_dir = workspace / "output_result"
    tmp_dir = workspace / ".chatds" / "tmp"
    compat_dir = workspace / ".chatds" / "runtime_compat"
    for path in (output_dir, tmp_dir, compat_dir):
        path.mkdir(parents=True, exist_ok=True)
        _make_workspace_path_readable(path)
    _write_sitecustomize(compat_dir)
    python_paths = [str(compat_dir)]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env.update({
        "CHATDS_WORKSPACE": str(workspace),
        "CHATDS_SKILL_ROOT": str((USER_SKILLS_BASE / user_id / session_id).resolve()),
        "CHATDS_OUTPUT_DIR": str(output_dir),
        "OUTPUT_DIR": str(output_dir),
        "RESULTS_DIR": str(output_dir),
        "TMPDIR": str(tmp_dir),
        "MPLCONFIGDIR": str(tmp_dir / "matplotlib"),
        "PYTHONPATH": os.pathsep.join(python_paths),
        "PYTHONUNBUFFERED": "1",
        "CHATDS_FILE_UMASK": "022",
    })
    return env


def _write_sitecustomize(compat_dir: Path) -> None:
    module = compat_dir / "sitecustomize.py"
    module.write_text(_SITECUSTOMIZE, encoding="utf-8")
    _make_workspace_path_readable(module)


def _make_workspace_path_readable(path: Path) -> None:
    try:
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.parent.chmod(0o755)
            path.chmod(0o644)
    except OSError:
        pass


_SITECUSTOMIZE = r'''
import builtins as _builtins
import io as _io
import os as _os

_ORIGINAL_OPEN = _builtins.open
_ORIGINAL_IO_OPEN = _io.open
_OUTPUT_DIR = _os.environ.get("CHATDS_OUTPUT_DIR")
_WORKSPACE = _os.environ.get("CHATDS_WORKSPACE")
_SKILL_ROOT = _os.environ.get("CHATDS_SKILL_ROOT")
_MARKERS = ("output", "outputs", "output_result", "results", "artifacts")
_WRITE_MODES = ("w", "a", "x", "+")

try:
    _os.umask(int(_os.environ.get("CHATDS_FILE_UMASK", "022"), 8))
except Exception:
    pass


def _map_path(file, mode):
    try:
        path = _os.fspath(file)
    except TypeError:
        return file
    if not _os.path.isabs(path):
        if _WORKSPACE and (path == "workspace" or path.startswith("workspace/")):
            return _os.path.join(_WORKSPACE, path.split("/", 1)[1] if "/" in path else "")
        if _SKILL_ROOT and (path == "skills" or path.startswith("skills/")):
            return _os.path.join(_SKILL_ROOT, path.split("/", 1)[1] if "/" in path else "")
        return file
    if not _OUTPUT_DIR or not any(flag in str(mode) for flag in _WRITE_MODES):
        return file
    lowered = path.lower()
    if not any(marker in lowered for marker in _MARKERS):
        return file
    target = _os.path.join(_OUTPUT_DIR, _os.path.basename(path) or "result")
    _os.makedirs(_os.path.dirname(target), exist_ok=True)
    return target


def open(file, mode="r", *args, **kwargs):
    return _ORIGINAL_OPEN(_map_path(file, mode), mode, *args, **kwargs)


def io_open(file, mode="r", *args, **kwargs):
    return _ORIGINAL_IO_OPEN(_map_path(file, mode), mode, *args, **kwargs)


_builtins.open = open
_io.open = io_open
'''


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def _display_path(path: Path, user_id: str, session_id: str) -> str:
    workspace = sandbox_dir(user_id, session_id, sub="workspace").resolve()
    skills = (USER_SKILLS_BASE / user_id / session_id).resolve()
    try:
        return "workspace/" + str(path.resolve().relative_to(workspace))
    except ValueError:
        pass
    try:
        return "skills/" + str(path.resolve().relative_to(skills))
    except ValueError:
        return str(path)


RUN_SKILL_PYTHON_SCHEMA = {
    "name": "run_skill_python",
    "description": (
        "Run a Python entrypoint from the exact installed Skill selected for this run in the controlled isolated session sandbox. "
        "The Skill and workspace are transferred as bounded content-addressed snapshots; host paths and harness "
        "secrets are not mounted. For importable Skill helpers, prefer the declarative function mode: provide "
        "function_name plus JSON "
        "function_args/function_kwargs. It invokes only a public top-level function declared by that exact Skill .py; "
        "or provide class_name plus method_name and bounded constructor/method JSON arguments to invoke one direct "
        "public instance method. Function, instance-method, and CLI modes are mutually exclusive. dotted/private "
        "names and static, class, property, decorated, inherited, or dynamically rebound methods are rejected. "
        "Omit all callable target fields for normal isolated CLI behavior. "
        "Workspace scripts fail closed; use execute_code for bounded ad-hoc calculations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "script_path": {
                "type": "string",
                "description": (
                    "Canonical path to a visible selected Skill .py: skills/<skill>/<path>. "
                    "Disabled user-package, workspace, traversal, and directory-alias paths are rejected."
                ),
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "CLI mode only: command-line arguments passed to the script. Do not combine non-empty args with function_name.",
                "default": [],
            },
            "function_name": {
                "type": "string",
                "pattern": "^[A-Za-z][A-Za-z0-9_]{0,127}$",
                "description": (
                    "Safe function mode: exact public top-level function declared in the selected installed Skill .py. "
                    "Dotted paths, imported functions, private names, expressions, and arbitrary code are rejected."
                ),
            },
            "function_args": {
                "type": "array",
                "items": {},
                "maxItems": MAX_FUNCTION_ARGS,
                "description": (
                    "Safe function mode: JSON positional arguments (max "
                    f"{MAX_FUNCTION_ARGS} items, {MAX_FUNCTION_JSON_DEPTH} nesting levels, and shared "
                    f"{MAX_FUNCTION_INPUT_BYTES}-byte encoded input cap)."
                ),
            },
            "function_kwargs": {
                "type": "object",
                "additionalProperties": True,
                "maxProperties": MAX_FUNCTION_KWARGS,
                "description": (
                    "Safe function mode: JSON keyword arguments using public identifier keys (max "
                    f"{MAX_FUNCTION_KWARGS} keys; shared input limits apply)."
                ),
            },
            "class_name": {
                "type": "string",
                "pattern": "^[A-Za-z][A-Za-z0-9_]{0,127}$",
                "description": (
                    "Safe instance-method mode: exact public top-level class "
                    "declared in the selected installed Skill .py. Must be used "
                    "with method_name and without function_name or CLI args."
                ),
            },
            "method_name": {
                "type": "string",
                "pattern": "^[A-Za-z][A-Za-z0-9_]{0,127}$",
                "description": (
                    "Safe instance-method mode: exact direct public plain "
                    "instance method on class_name. Dotted, inherited, static, "
                    "class, property, decorated, and private methods are rejected."
                ),
            },
            "constructor_args": {
                "type": "array",
                "items": {},
                "maxItems": MAX_FUNCTION_ARGS,
                "description": (
                    "Safe instance-method mode: JSON positional constructor "
                    "arguments. Constructor and method positional arguments share "
                    f"the {MAX_FUNCTION_ARGS}-item and {MAX_FUNCTION_INPUT_BYTES}-byte caps."
                ),
            },
            "constructor_kwargs": {
                "type": "object",
                "additionalProperties": True,
                "maxProperties": MAX_FUNCTION_KWARGS,
                "description": (
                    "Safe instance-method mode: JSON constructor keyword arguments "
                    "with public identifier keys; constructor and method keyword "
                    f"arguments share the {MAX_FUNCTION_KWARGS}-key cap."
                ),
            },
            "method_args": {
                "type": "array",
                "items": {},
                "maxItems": MAX_FUNCTION_ARGS,
                "description": (
                    "Safe instance-method mode: JSON positional arguments for "
                    "method_name; shared constructor/method bounds apply."
                ),
            },
            "method_kwargs": {
                "type": "object",
                "additionalProperties": True,
                "maxProperties": MAX_FUNCTION_KWARGS,
                "description": (
                    "Safe instance-method mode: JSON keyword arguments for "
                    "method_name using public identifier keys; shared bounds apply."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum execution time in seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}).",
                "default": DEFAULT_TIMEOUT,
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Isolated working directory: workspace, script, or skill:<selected-skill>. "
                    "Arbitrary host subdirectories are rejected."
                ),
                "default": "workspace",
            },
        },
        "required": ["script_path"],
    },
}
