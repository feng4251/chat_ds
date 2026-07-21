"""Run declared scripts from an exact visible Skill without shell evaluation.

The model selects only a Skill-owned script path, argv, timeout, and one of two
working-directory policies.  The harness selects the interpreter from the
file extension.  No caller-controlled executable, command string, shell
expansion, or fallback evaluation path exists.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.python_env import preflight_declared_skill_dependencies
from skills.path_safety import validate_skill_resource, validate_skill_root
from skills import scanner as skill_scanner
from tools.omission_guard import (
    compacted_history_omission_error,
    find_compacted_history_omission_path,
)
from tools.isolated_skill_executor import (
    IsolatedSkillExecutorError,
    execute_isolated_skill_script,
)
from tools.path_security import sandbox_dir


DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 300
MAX_ARGS = 64
MAX_ARG_CHARS = 4_096
MAX_ARG_BYTES = 32_768
MAX_SCRIPT_PATH_CHARS = 2_048
MAX_SCRIPT_PATH_COMPONENTS = 32
MAX_STDOUT = 80_000
MAX_STDERR = 20_000
MAX_ARTIFACTS = 40
MAX_ARTIFACT_SCAN_FILES = 20_000

SUPPORTED_INTERPRETERS = {
    ".py": "python",
    ".sh": "bash",
    ".bash": "bash",
    ".js": "node",
    ".mjs": "node",
}
SUPPORTED_EXTENSIONS = tuple(SUPPORTED_INTERPRETERS)
SYSTEM_INTERPRETER_PATH = os.pathsep.join(("/usr/local/bin", "/usr/bin", "/bin"))
INTERPRETER_EXECUTABLES = {
    "bash": "bash",
    "node": "node",
}
ARTIFACT_SKIP_DIRS = frozenset({
    ".chatds",
    "debug",
    "__pycache__",
    ".pytest_cache",
    ".git",
    "node_modules",
})

# Kept as a module-level policy root so deployments and tests can override the
# storage location without widening path parsing.
USER_SKILLS_BASE = skill_scanner.USER_SKILLS_BASE


class SkillScriptError(ValueError):
    """A stable, model-actionable validation or resolution failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _error(code: str, message: str, **extra: Any) -> str:
    payload: dict[str, Any] = {
        "status": "error",
        "error_code": code,
        "error": message,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


async def run_skill_script(
    script_path: str,
    args: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str = "workspace",
    user_id: str = "default",
    session_id: str = "default",
    enabled_user_skills: list[str] | None = None,
) -> str:
    """Run one script from an installed Skill selected for this run."""

    for field, value in (
        ("script_path", script_path),
        ("args", args or []),
        ("cwd", cwd),
    ):
        omitted_path = find_compacted_history_omission_path(value, field)
        if omitted_path:
            detail = compacted_history_omission_error(omitted_path)
            return _error(
                str(detail.get("reason") or "invalid_placeholder_content"),
                str(detail.get("error") or "Compacted history placeholder rejected."),
                field=detail.get("field") or field,
            )

    try:
        safe_args = _validated_args(args)
        safe_timeout = _validated_timeout(timeout)
        safe_cwd = _validated_cwd(cwd)
        script, skill_dir, skill_name = _resolve_session_skill_script(
            script_path,
            user_id,
            session_id,
            enabled_user_skills,
        )
    except SkillScriptError as exc:
        return _error(
            exc.code,
            str(exc),
            script_path=str(script_path or ""),
            supported_extensions=list(SUPPORTED_EXTENSIONS),
        )

    display_path = _display_requested_path(script_path)
    resolved_path = _display_script_path(script, skill_dir, skill_name)
    runtime_preflight = preflight_declared_skill_dependencies(skill_dir)
    if runtime_preflight.get("valid") is not True:
        return _error(
            "skill_runtime_prerequisites_unsatisfied",
            (
                "The installed Skill's declared runtime dependencies are not "
                "available in the network-disabled isolated executor."
            ),
            script_path=display_path,
            runtime_preflight=runtime_preflight,
            execution_runtime="isolated_skill_executor",
            fallback_attempted=False,
        )
    workspace = sandbox_dir(user_id, session_id, sub="workspace").resolve()
    try:
        payload = await execute_isolated_skill_script(
            skill_root=skill_dir,
            workspace=workspace,
            entrypoint=script.relative_to(skill_dir).as_posix(),
            args=safe_args,
            timeout=safe_timeout,
            cwd=safe_cwd,
        )
    except IsolatedSkillExecutorError as exc:
        return _error(
            exc.code,
            str(exc),
            script_path=display_path,
            execution_runtime="isolated_skill_executor",
            network="disabled",
            fallback_attempted=False,
        )
    payload["script_path"] = display_path
    payload["resolved_skill_script_path"] = resolved_path
    payload["script_type"] = script.suffix.lstrip(".")
    payload["execution_runtime"] = "isolated_skill_executor"
    payload["environment_policy"] = "ephemeral_snapshot_no_secrets"
    payload["runtime_preflight"] = runtime_preflight
    payload["fallback_attempted"] = False
    payload["workspace_output_dir"] = "workspace/output_result"
    if resolved_path != display_path:
        payload["resolved_skill_script_path"] = resolved_path
    return json.dumps(payload, ensure_ascii=False)


def _validated_args(args: list[str] | None) -> list[str]:
    if args is None:
        return []
    if not isinstance(args, list):
        raise SkillScriptError("invalid_args", "args must be a JSON array of strings.")
    if len(args) > MAX_ARGS:
        raise SkillScriptError(
            "argument_limit_exceeded",
            f"args exceeds the {MAX_ARGS}-item limit.",
        )
    result: list[str] = []
    encoded_bytes = 0
    for index, item in enumerate(args):
        if not isinstance(item, str):
            raise SkillScriptError(
                "invalid_args",
                f"args[{index}] must be a string.",
            )
        if "\x00" in item:
            raise SkillScriptError(
                "invalid_args",
                f"args[{index}] contains a NUL byte.",
            )
        if len(item) > MAX_ARG_CHARS:
            raise SkillScriptError(
                "argument_limit_exceeded",
                f"args[{index}] exceeds the {MAX_ARG_CHARS}-character limit.",
            )
        encoded_bytes += len(item.encode("utf-8"))
        if encoded_bytes > MAX_ARG_BYTES:
            raise SkillScriptError(
                "argument_limit_exceeded",
                f"Encoded args exceed the {MAX_ARG_BYTES}-byte aggregate limit.",
            )
        result.append(item)
    return result


def _validated_timeout(timeout: int) -> int:
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise SkillScriptError("invalid_timeout", "timeout must be an integer number of seconds.")
    if timeout < 1 or timeout > MAX_TIMEOUT:
        raise SkillScriptError(
            "invalid_timeout",
            f"timeout must be between 1 and {MAX_TIMEOUT} seconds.",
        )
    return timeout


def _validated_cwd(cwd: str) -> str:
    if cwd not in {"workspace", "script"}:
        raise SkillScriptError(
            "invalid_cwd",
            "cwd must be exactly 'workspace' or 'script'.",
        )
    return cwd


def _resolve_session_skill_script(
    script_path: str,
    user_id: str,
    session_id: str,
    enabled_user_skills: list[str] | None = None,
) -> tuple[Path, Path, str]:
    if not isinstance(script_path, str) or not script_path.strip():
        raise SkillScriptError("invalid_script_path", "script_path is required.")
    path_text = script_path.strip()
    if len(path_text) > MAX_SCRIPT_PATH_CHARS:
        raise SkillScriptError(
            "invalid_script_path",
            f"script_path exceeds the {MAX_SCRIPT_PATH_CHARS}-character limit.",
        )
    if "\x00" in path_text or "\\" in path_text:
        raise SkillScriptError(
            "invalid_script_path",
            "script_path contains an unsafe path character.",
        )
    if path_text.startswith("skills/"):
        path_text = path_text[len("skills/"):]
    components = path_text.split("/")
    rel = PurePosixPath(path_text)
    if (
        rel.is_absolute()
        or len(components) < 2
        or len(components) > MAX_SCRIPT_PATH_COMPONENTS
        or any(part in {"", ".", ".."} for part in components)
    ):
        raise SkillScriptError(
            "invalid_script_path",
            "Use a full relative Skill path such as skills/<skill>/scripts/task.py.",
        )
    suffix = rel.suffix
    if suffix not in SUPPORTED_INTERPRETERS:
        raise SkillScriptError(
            "unsupported_script_type",
            "script_path must use an allowlisted extension: "
            + ", ".join(SUPPORTED_EXTENSIONS),
        )

    for field, identifier in (("user_id", user_id), ("session_id", session_id)):
        if (
            not isinstance(identifier, str)
            or not identifier
            or "/" in identifier
            or "\\" in identifier
            or identifier in {".", ".."}
        ):
            raise SkillScriptError(
                "invalid_session_identity",
                f"Runtime-owned {field} is not a single safe path component.",
            )
    session_skills = _session_skill_registry(
        user_id=user_id,
        session_id=session_id,
        enabled_user_skills=(
            list(enabled_user_skills)
            if enabled_user_skills is not None
            else []
        ),
    )
    canonical = _resolve_declared_skill_script(
        components,
        session_skills=session_skills,
    )
    if canonical is not None:
        return canonical
    raise SkillScriptError(
        "unknown_session_skill",
        "script_path must begin with a canonical Skill name visible in this run; disabled user packages and directory-name aliases are not executable.",
    )


def _session_skill_registry(
    *,
    user_id: str,
    session_id: str,
    enabled_user_skills: list[str] | None = None,
) -> dict[str, Path]:
    registry: dict[str, Path] = {}
    for item in skill_scanner.find_all_skills(
        user_id,
        session_id,
        enabled_user_skills=enabled_user_skills,
    ):
        if not isinstance(item.get("name"), str):
            continue
        try:
            skill_dir = Path(str(item.get("path"))).parent.resolve(strict=True)
            root_check = validate_skill_root(skill_dir)
            if not root_check.valid or root_check.path != skill_dir:
                continue
            advertised_main = Path(str(item.get("path"))).resolve(strict=True)
            if advertised_main != (skill_dir / "SKILL.md").resolve(strict=True):
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        registry[str(item["name"])] = skill_dir
    return registry


def _resolve_declared_skill_script(
    components: list[str],
    *,
    session_skills: dict[str, Path],
) -> tuple[Path, Path, str] | None:
    """Resolve the longest canonical frontmatter-name prefix, if present.

    A package directory may legitimately differ from its declared name.  The
    scanner exposes the declared name, so this execution bridge must accept
    the same model-visible identity while retaining an exact package root.
    """

    for split_at in range(len(components) - 1, 0, -1):
        declared_name = "/".join(components[:split_at])
        declared_dir = session_skills.get(declared_name)
        if declared_dir is None:
            continue
        try:
            skill_dir = declared_dir.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            # A stale registry entry does not authorize execution and must not
            # prevent a physical session-package path from resolving.
            continue
        relative_script = Path(*components[split_at:])
        check = validate_skill_resource(
            skill_dir,
            relative_script,
            expected_kind="file",
            require_relative=True,
        )
        if not check.valid or check.path is None:
            raise SkillScriptError(
                str(check.code or "invalid_script_path"),
                str(check.message or "Declared Skill script is unavailable or unsafe."),
            )
        return check.path, skill_dir, declared_name
    return None


def _display_script_path(script: Path, skill_root: Path, skill_name: str) -> str:
    relative = PurePosixPath(script.resolve().relative_to(skill_root.resolve()))
    return f"skills/{skill_name}/{relative}"


def _display_requested_path(script_path: str) -> str:
    clean = script_path.strip()
    return clean if clean.startswith("skills/") else "skills/" + clean


def _resolve_interpreter(kind: str) -> Path | None:
    executable = INTERPRETER_EXECUTABLES.get(kind)
    if executable is None:
        return None
    found = shutil.which(executable, path=SYSTEM_INTERPRETER_PATH)
    if not found:
        return None
    candidate = Path(found)
    try:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            return None
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _annotate_python_result(
    raw: str,
    *,
    display_path: str,
    resolved_path: str,
) -> str:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return _error(
            "managed_python_invalid_response",
            "run_skill_python returned a non-JSON response.",
        )
    if not isinstance(parsed, dict):
        return _error(
            "managed_python_invalid_response",
            "run_skill_python returned a non-object response.",
        )
    parsed.update({
        "runner": "run_skill_script",
        "script_type": "python",
        "interpreter": "managed_python",
        "interpreter_policy": "extension_allowlist",
        "delegated_tool": "run_skill_python",
        "fallback_attempted": False,
        "script_path": display_path,
    })
    if resolved_path != display_path:
        parsed["resolved_skill_script_path"] = resolved_path
    return json.dumps(parsed, ensure_ascii=False)


def _safe_script_env(*, workspace: Path, session_root: Path, skill_dir: Path) -> dict[str, str]:
    output_dir = _ensure_safe_workspace_directory(workspace, ("output_result",))
    tmp_dir = _ensure_safe_workspace_directory(workspace, (".chatds", "script_tmp"))
    home_dir = _ensure_safe_workspace_directory(workspace, (".chatds", "script_home"))

    env: dict[str, str] = {}
    for key in ("LANG", "LC_ALL", "TZ"):
        value = os.environ.get(key)
        if value and "\x00" not in value and len(value) <= 1_024:
            env[key] = value
    env.update({
        "PATH": SYSTEM_INTERPRETER_PATH,
        "HOME": str(home_dir),
        "TMPDIR": str(tmp_dir),
        "CHATDS_WORKSPACE": str(workspace),
        "CHATDS_SKILL_ROOT": str(session_root),
        "CHATDS_SKILL_DIR": str(skill_dir),
        "SKILL_DIR": str(skill_dir),
        "CHATDS_OUTPUT_DIR": str(output_dir),
        "OUTPUT_DIR": str(output_dir),
        "RESULTS_DIR": str(output_dir),
        "NO_COLOR": "1",
    })
    return env


def _ensure_safe_workspace_directory(workspace: Path, parts: tuple[str, ...]) -> Path:
    current = workspace
    for part in parts:
        candidate = current / part
        try:
            mode = os.lstat(candidate).st_mode
        except FileNotFoundError:
            try:
                candidate.mkdir(mode=0o755)
                mode = os.lstat(candidate).st_mode
            except (FileExistsError, OSError) as exc:
                raise SkillScriptError(
                    "unsafe_workspace_runtime_directory",
                    f"Could not create safe workspace runtime directory {part!r}: {exc}",
                ) from exc
        except OSError as exc:
            raise SkillScriptError(
                "unsafe_workspace_runtime_directory",
                f"Could not inspect workspace runtime directory {part!r}: {exc}",
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise SkillScriptError(
                "unsafe_workspace_runtime_directory",
                f"Workspace runtime directory {part!r} is linked or not a directory.",
            )
        current = candidate
    try:
        current.resolve(strict=True).relative_to(workspace)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SkillScriptError(
            "unsafe_workspace_runtime_directory",
            "Workspace runtime directory escapes the current workspace.",
        ) from exc
    return current


async def _communicate_capped(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes, bool, bool]:
    async def read_stream(
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
        return bytes(kept), truncated

    stdout_task = asyncio.create_task(read_stream(proc.stdout, MAX_STDOUT))
    stderr_task = asyncio.create_task(read_stream(proc.stderr, MAX_STDERR))
    try:
        stdout_result, stderr_result = await asyncio.gather(stdout_task, stderr_task)
        await proc.wait()
    except asyncio.CancelledError:
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    return stdout_result[0], stderr_result[0], stdout_result[1], stderr_result[1]


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            if os.name == "posix" and proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError, ChildProcessError):
        pass


def _workspace_snapshot(workspace: Path) -> tuple[dict[str, tuple[int, int]], bool]:
    snapshot: dict[str, tuple[int, int]] = {}
    truncated = False
    if not workspace.is_dir():
        return snapshot, truncated
    examined = 0
    for walk_root, dirs, files in os.walk(workspace, followlinks=False):
        root = Path(walk_root)
        dirs[:] = sorted(
            name for name in dirs
            if name not in ARTIFACT_SKIP_DIRS and not (root / name).is_symlink()
        )
        for name in sorted(files):
            path = root / name
            if path.is_symlink():
                continue
            examined += 1
            if examined > MAX_ARTIFACT_SCAN_FILES:
                truncated = True
                return snapshot, truncated
            try:
                resolved = path.resolve(strict=True)
                relative = str(PurePosixPath(resolved.relative_to(workspace)))
                stat_result = resolved.stat()
            except (OSError, RuntimeError, ValueError):
                continue
            if not resolved.is_file():
                continue
            snapshot[relative] = (stat_result.st_size, stat_result.st_mtime_ns)
    return snapshot, truncated


def _workspace_artifact_changes(
    workspace: Path,
    before: dict[str, tuple[int, int]],
) -> tuple[list[dict[str, Any]], bool]:
    after, truncated = _workspace_snapshot(workspace)
    changes: list[dict[str, Any]] = []
    for relative, current in after.items():
        if before.get(relative) == current:
            continue
        path = workspace / Path(*PurePosixPath(relative).parts)
        changes.append({
            "kind": "file",
            "path": relative,
            "title": path.name,
            "size_bytes": current[0],
            "change": "created" if relative not in before else "modified",
            "source": "workspace_diff",
        })
    changes.sort(key=lambda item: (
        0 if str(item["path"]).startswith("output_result/") else 1,
        str(item["path"]),
    ))
    return changes[:MAX_ARTIFACTS], truncated


def _display_cwd(workdir: Path, workspace: Path, script: Path) -> str:
    if workdir == workspace:
        return "workspace"
    if workdir == script.parent:
        return "script"
    return str(workdir)


RUN_SKILL_SCRIPT_SCHEMA = {
    "name": "run_skill_script",
    "description": (
        "Run a real script declared inside the exact installed Skill selected for this run. "
        "Supported extensions are .py, .sh, .bash, .js, and .mjs. The harness "
        "sends an immutable, content-addressed Skill/workspace snapshot to a "
        "network-disabled sidecar and selects a fixed Python, bash, or node "
        "interpreter from the extension. Provide argv as "
        "separate strings; command strings, custom interpreters, shell eval, and "
        "fallback execution are never accepted. Only validated changed workspace "
        "files are atomically applied and returned as complete artifact receipts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "script_path": {
                "type": "string",
                "maxLength": MAX_SCRIPT_PATH_CHARS,
                "description": (
                    "Full canonical path inside the selected Skill, for example "
                    "skills/<skill>/scripts/task.sh. Disabled user packages, workspace, "
                    "absolute, traversal, and symlink paths are rejected."
                ),
            },
            "args": {
                "type": "array",
                "items": {"type": "string", "maxLength": MAX_ARG_CHARS},
                "maxItems": MAX_ARGS,
                "default": [],
                "description": (
                    "Literal argv entries, never a command string. Maximum "
                    f"{MAX_ARGS} items and {MAX_ARG_BYTES} encoded bytes total."
                ),
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TIMEOUT,
                "default": DEFAULT_TIMEOUT,
                "description": (
                    f"Execution timeout in seconds (1-{MAX_TIMEOUT}); timeout kills "
                    "the isolated interpreter process group."
                ),
            },
            "cwd": {
                "type": "string",
                "enum": ["workspace", "script"],
                "default": "workspace",
                "description": (
                    "Use workspace for artifact-producing runs. Use script only when "
                    "the declared Skill script requires package-relative resources."
                ),
            },
        },
        "required": ["script_path"],
    },
}
