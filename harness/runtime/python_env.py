"""Session-scoped Python runtime environments for skill code."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from skills.dependencies import aggregate_dependency_reports, scan_skill_dependencies
from skills.scanner import USER_SKILLS_BASE

RUNTIME_ROOT = Path(os.environ.get("SKILL_RUNTIME_ROOT", "/app/data/runtime_envs"))
PIP_INDEX_URL = os.environ.get("SKILL_RUNTIME_PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")
ALLOW_NETWORK = os.environ.get("ALLOW_SKILL_RUNTIME_NETWORK", "true").lower() in {"1", "true", "yes", "on"}
ALLOW_AUTO_INSTALL = os.environ.get("ALLOW_SKILL_AUTO_PIP_INSTALL", "true").lower() in {"1", "true", "yes", "on"}
MAX_PACKAGES = int(os.environ.get("SKILL_RUNTIME_MAX_PACKAGES", "80"))
INSTALL_TIMEOUT_SECONDS = int(os.environ.get("SKILL_RUNTIME_INSTALL_TIMEOUT_SECONDS", "300"))
MAX_LOG_CHARS = 40_000
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def scan_skill_runtime(skill_dir: str | Path) -> dict[str, Any]:
    report = scan_skill_dependencies(skill_dir)
    return {
        "status": _dependency_status(report),
        "dependencies": report,
        "policy": runtime_policy(),
    }


def runtime_policy() -> dict[str, Any]:
    return {
        "allow_network": ALLOW_NETWORK,
        "allow_auto_install": ALLOW_AUTO_INSTALL,
        "pip_index_url": PIP_INDEX_URL,
        "max_packages": MAX_PACKAGES,
        "install_timeout_seconds": INSTALL_TIMEOUT_SECONDS,
    }


async def ensure_session_runtime(
    user_id: str,
    session_id: str,
    *,
    extra_skill_dirs: list[str | Path] | None = None,
) -> dict[str, Any]:
    safe_user = _safe_component(user_id, "user_id")
    safe_session = _safe_component(session_id, "session_id")
    reports = _scan_session_reports(safe_user, safe_session, extra_skill_dirs=extra_skill_dirs)
    manifest = aggregate_dependency_reports(reports)
    packages = list(manifest.get("python_packages") or [])
    manifest["policy"] = runtime_policy()
    manifest["user_id"] = safe_user
    manifest["session_id"] = safe_session

    status = _dependency_status(manifest)
    if manifest.get("unsupported"):
        return _write_status(
            safe_user,
            safe_session,
            _manifest_hash(manifest),
            {
                "status": "unsupported",
                "error": "unsupported_dependencies",
                "manifest": manifest,
            },
        )
    if not packages:
        return _write_status(
            safe_user,
            safe_session,
            "builtin",
            {
                "status": status,
                "env_hash": "builtin",
                "venv_python": sys.executable,
                "manifest": manifest,
                "message": "No declared Python packages; using harness Python.",
            },
        )
    if len(packages) > MAX_PACKAGES:
        manifest["warnings"] = list(manifest.get("warnings") or []) + [
            f"Package count {len(packages)} exceeds limit {MAX_PACKAGES}."
        ]
        return _write_status(
            safe_user,
            safe_session,
            _manifest_hash(manifest),
            {
                "status": "blocked",
                "error": "too_many_packages",
                "manifest": manifest,
            },
        )
    if not ALLOW_NETWORK or not ALLOW_AUTO_INSTALL:
        return _write_status(
            safe_user,
            safe_session,
            _manifest_hash(manifest),
            {
                "status": "blocked",
                "error": "auto_install_disabled",
                "manifest": manifest,
            },
        )

    env_hash = _manifest_hash(manifest)
    env_dir = _env_dir(safe_user, safe_session, env_hash)
    venv_dir = env_dir / "venv"
    python_bin = _venv_python(venv_dir)
    manifest_path = env_dir / "manifest.json"
    status_path = env_dir / "status.json"
    install_log_path = env_dir / "install.log"
    freeze_path = env_dir / "freeze.txt"

    if python_bin.exists() and status_path.exists():
        try:
            existing = json.loads(status_path.read_text(encoding="utf-8"))
            if existing.get("status") == "ready":
                return existing
        except (OSError, json.JSONDecodeError):
            pass

    env_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_status_file(status_path, {
        "status": "installing",
        "env_hash": env_hash,
        "env_dir": str(env_dir),
        "manifest": manifest,
    })

    try:
        if not python_bin.exists():
            await _run_command([sys.executable, "-m", "venv", str(venv_dir)], timeout=120)
        pip = _venv_pip(venv_dir)
        await _run_command([str(python_bin), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], timeout=INSTALL_TIMEOUT_SECONDS, log_path=install_log_path)
        install_cmd = [
            str(pip),
            "install",
            "--disable-pip-version-check",
            "--no-input",
        ]
        if PIP_INDEX_URL:
            install_cmd.extend(["-i", PIP_INDEX_URL])
        install_cmd.extend(packages)
        await _run_command(install_cmd, timeout=INSTALL_TIMEOUT_SECONDS, log_path=install_log_path)
        freeze = await _run_command([str(pip), "freeze"], timeout=60)
        freeze_path.write_text(freeze.get("stdout", ""), encoding="utf-8")
        return _write_status(
            safe_user,
            safe_session,
            env_hash,
            {
                "status": "ready",
                "env_hash": env_hash,
                "env_dir": str(env_dir),
                "venv_python": str(python_bin),
                "venv_path": str(venv_dir),
                "manifest": manifest,
                "install_log": str(install_log_path),
                "freeze": str(freeze_path),
            },
        )
    except Exception as exc:
        log_tail = _tail_file(install_log_path)
        return _write_status(
            safe_user,
            safe_session,
            env_hash,
            {
                "status": "install_failed",
                "env_hash": env_hash,
                "env_dir": str(env_dir),
                "error": f"{type(exc).__name__}: {exc}",
                "log_tail": log_tail,
                "manifest": manifest,
                "install_log": str(install_log_path),
            },
        )


def get_session_runtime_status(user_id: str, session_id: str) -> dict[str, Any]:
    safe_user = _safe_component(user_id, "user_id")
    safe_session = _safe_component(session_id, "session_id")
    current = _session_root(safe_user, safe_session) / "current.json"
    if current.is_file():
        try:
            return json.loads(current.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    reports = _scan_session_reports(safe_user, safe_session)
    manifest = aggregate_dependency_reports(reports)
    return {
        "status": _dependency_status(manifest),
        "env_hash": None,
        "manifest": manifest,
        "policy": runtime_policy(),
    }


def runtime_env_for_subprocess(status: dict[str, Any], base_env: dict[str, str]) -> dict[str, str]:
    env = dict(base_env)
    venv_path = status.get("venv_path")
    if isinstance(venv_path, str) and venv_path:
        bin_dir = str(Path(venv_path) / "bin")
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = venv_path
    return env


def resolve_session_python(status: dict[str, Any]) -> str | None:
    if status.get("status") != "ready":
        return None
    python = status.get("venv_python")
    return str(python) if python else None


def _scan_session_reports(
    user_id: str,
    session_id: str,
    *,
    extra_skill_dirs: list[str | Path] | None = None,
) -> list[dict[str, Any]]:
    roots: list[Path] = []
    session_root = USER_SKILLS_BASE / user_id / session_id
    if session_root.is_dir():
        roots.extend(path.parent for path in session_root.rglob("SKILL.md") if path.is_file())
    for extra in extra_skill_dirs or []:
        path = Path(extra).resolve()
        if path.is_dir() and (path / "SKILL.md").is_file():
            roots.append(path)
    seen: set[str] = set()
    reports: list[dict[str, Any]] = []
    for root in roots:
        key = str(root.resolve())
        if key in seen:
            continue
        seen.add(key)
        report = scan_skill_dependencies(root)
        report["skill_dir"] = key
        reports.append(report)
    return reports


def _dependency_status(manifest: dict[str, Any]) -> str:
    if manifest.get("unsupported"):
        return "unsupported"
    if manifest.get("python_packages"):
        return "needs_install"
    if manifest.get("heuristic_imports"):
        return "needs_review"
    return "ready"


def _manifest_hash(manifest: dict[str, Any]) -> str:
    relevant = {
        "python_packages": sorted(str(item) for item in manifest.get("python_packages") or []),
        "unsupported": sorted(str(item) for item in manifest.get("unsupported") or []),
        "policy": runtime_policy(),
    }
    raw = json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _safe_component(value: str, label: str) -> str:
    text = str(value or "default")
    if not SAFE_ID_RE.match(text):
        raise ValueError(f"Invalid {label}: {value}")
    return text


def _session_root(user_id: str, session_id: str) -> Path:
    return RUNTIME_ROOT / user_id / session_id


def _env_dir(user_id: str, session_id: str, env_hash: str) -> Path:
    return _session_root(user_id, session_id) / env_hash


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def _venv_pip(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "pip"


async def _run_command(
    cmd: list[str],
    *,
    timeout: int,
    log_path: Path | None = None,
) -> dict[str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_pip_env(),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"Command timed out after {timeout}s: {cmd[0]}")
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if log_path is not None:
        with log_path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write("\n$ " + _safe_cmd(cmd) + "\n")
            handle.write(stdout)
            handle.write(stderr)
            if handle.tell() > MAX_LOG_CHARS:
                handle.truncate(MAX_LOG_CHARS)
    if proc.returncode != 0:
        raise RuntimeError(stderr.strip() or stdout.strip() or f"Command failed: {cmd[0]}")
    return {"stdout": stdout, "stderr": stderr}


def _pip_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("PIP_NO_INPUT", "1")
    if PIP_INDEX_URL:
        env.setdefault("PIP_INDEX_URL", PIP_INDEX_URL)
    return env


def _safe_cmd(cmd: list[str]) -> str:
    return " ".join(str(part) for part in cmd[:4]) + (" ..." if len(cmd) > 4 else "")


def _write_status(user_id: str, session_id: str, env_hash: str, status: dict[str, Any]) -> dict[str, Any]:
    env_dir = _env_dir(user_id, session_id, env_hash)
    env_dir.mkdir(parents=True, exist_ok=True)
    status.setdefault("policy", runtime_policy())
    _write_status_file(env_dir / "status.json", status)
    current = _session_root(user_id, session_id) / "current.json"
    current.parent.mkdir(parents=True, exist_ok=True)
    _write_status_file(current, status)
    return status


def _write_status_file(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def _tail_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-4000:]


def clean_session_runtime(user_id: str, session_id: str) -> bool:
    root = _session_root(_safe_component(user_id, "user_id"), _safe_component(session_id, "session_id"))
    if not root.exists():
        return False
    shutil.rmtree(root)
    return True
