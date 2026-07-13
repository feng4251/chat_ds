"""Run Python entrypoints from the current session workspace or skills."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from runtime.python_env import ensure_session_runtime, resolve_session_python, runtime_env_for_subprocess
from skills.scanner import USER_SKILLS_BASE
from tools.path_security import sandbox_dir, validate_path

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 300
MAX_STDOUT = 80_000
MAX_STDERR = 20_000
MAX_ARTIFACTS = 40
ARTIFACT_EXTENSIONS = {".md", ".json", ".csv", ".tsv", ".txt", ".yaml", ".yml"}
ARTIFACT_SKIP_DIRS = {".chatds", "debug", "__pycache__", ".pytest_cache"}


async def run_skill_python(
    script_path: str,
    args: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str = "workspace",
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    try:
        script = _resolve_script(script_path, user_id, session_id)
        workdir = _resolve_cwd(cwd, user_id, session_id, script)
    except (ValueError, FileNotFoundError) as exc:
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "available_skill_scripts": _available_skill_scripts(user_id, session_id),
        }, ensure_ascii=False)
    return await _run_python_script(
        script,
        args=args,
        timeout=timeout,
        cwd=workdir,
        user_id=user_id,
        session_id=session_id,
    )


async def run_managed_python_code(
    code: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    if not code or not code.strip():
        return json.dumps({"status": "error", "error": "No code provided."}, ensure_ascii=False)
    workspace = sandbox_dir(user_id, session_id, sub="workspace")
    managed_dir = workspace / ".chatds" / "managed_execute_code"
    managed_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(code.encode("utf-8", errors="replace")).hexdigest()[:16]
    script = managed_dir / f"execute_code_{digest}.py"
    script.write_text(code, encoding="utf-8")
    _make_workspace_path_readable(script)
    return await _run_python_script(
        script,
        args=[],
        timeout=timeout,
        cwd=workspace,
        user_id=user_id,
        session_id=session_id,
        managed_fallback=True,
    )


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
    runtime = await ensure_session_runtime(user_id, session_id)
    if runtime.get("status") != "ready":
        return json.dumps({
            "status": "error",
            "error": "Python runtime is not ready.",
            "runtime_status": runtime.get("status"),
            "runtime_error": runtime.get("error"),
            "policy": runtime.get("policy"),
        }, ensure_ascii=False)
    python = resolve_session_python(runtime) or os.sys.executable
    safe_args = [str(item) for item in (args or [])]
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
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
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return json.dumps({
            "status": "error",
            "error": f"Process timed out after {timeout}s.",
            "runtime_status": runtime.get("status"),
            "script_path": _display_path(script, user_id, session_id),
            "cwd": _display_path(cwd, user_id, session_id),
        }, ensure_ascii=False)
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    artifacts = _workspace_artifact_changes(user_id, session_id, before_artifacts)
    return json.dumps({
        "status": "success" if proc.returncode == 0 else "error",
        "returncode": proc.returncode,
        "stdout": _truncate(stdout, MAX_STDOUT),
        "stderr": _truncate(stderr, MAX_STDERR),
        "script_path": _display_path(script, user_id, session_id),
        "cwd": _display_path(cwd, user_id, session_id),
        "runtime_status": runtime.get("status"),
        "env_hash": runtime.get("env_hash"),
        "managed_fallback": managed_fallback,
        "workspace_output_dir": "workspace/output_result",
        "artifacts": artifacts,
    }, ensure_ascii=False)


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
    candidate = (root / Path(*rel.parts)).resolve()
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
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except ValueError:
            continue
        if not resolved.is_symlink() and resolved.is_file():
            matches.append(resolved)
    return matches


def _display_skill_candidate(path: Path, root: Path) -> str:
    try:
        return "skills/" + str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def _available_skill_scripts(user_id: str, session_id: str) -> list[str]:
    root = (USER_SKILLS_BASE / user_id / session_id).resolve()
    if not root.is_dir():
        return []
    result: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved.is_symlink() or not resolved.is_file():
            continue
        result.append(_display_skill_candidate(resolved, root))
        if len(result) >= 40:
            break
    return result


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
    for path in sorted(workspace.rglob("*")):
        rel_parts = path.relative_to(workspace).parts
        if any(part in ARTIFACT_SKIP_DIRS for part in rel_parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in ARTIFACT_EXTENSIONS:
            continue
        try:
            path.resolve().relative_to(workspace)
        except ValueError:
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
        "Run a Python script from the current session workspace or installed session skills using the managed session Python runtime. "
        "This runtime can install declared skill dependencies and may have network access when policy allows. "
        "Use this for skill-provided Python entrypoints; use execute_code for pure ad-hoc calculations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "script_path": {
                "type": "string",
                "description": "Path to a .py script: workspace-relative path, workspace/<path>, skills/<skill>/<path>, or a unique installed skill script path such as scripts/foo.py.",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Command-line arguments passed to the script.",
                "default": [],
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum execution time in seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}).",
                "default": DEFAULT_TIMEOUT,
            },
            "cwd": {
                "type": "string",
                "description": "Working directory: workspace, script, skill:<name>, or a workspace-relative directory.",
                "default": "workspace",
            },
        },
        "required": ["script_path"],
    },
}
