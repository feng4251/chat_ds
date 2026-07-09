"""Run Python entrypoints from the current session workspace or skills."""

from __future__ import annotations

import asyncio
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
    env = runtime_env_for_subprocess(runtime, _safe_env())
    proc = await asyncio.create_subprocess_exec(
        python,
        str(script),
        *safe_args,
        cwd=str(workdir),
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
        }, ensure_ascii=False)
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    return json.dumps({
        "status": "success" if proc.returncode == 0 else "error",
        "returncode": proc.returncode,
        "stdout": _truncate(stdout, MAX_STDOUT),
        "stderr": _truncate(stderr, MAX_STDERR),
        "script_path": _display_path(script, user_id, session_id),
        "cwd": _display_path(workdir, user_id, session_id),
        "runtime_status": runtime.get("status"),
        "env_hash": runtime.get("env_hash"),
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
        path = validate_path(path_text, user_id, session_id, sub="workspace", must_exist=True)
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


def _safe_env() -> dict[str, str]:
    keep = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR"}
    return {key: value for key, value in os.environ.items() if key in keep or key.startswith("XDG_")}


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
                "description": "Path to a .py script: workspace-relative path, workspace/<path>, skills/<skill>/<path>, or skills/<script.py> when unique.",
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
