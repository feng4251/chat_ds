"""Session-scoped Python runtime environments for skill code."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from skills.dependencies import (
    aggregate_dependency_reports,
    scan_declared_skill_dependencies,
    scan_dependency_manifests,
    scan_skill_dependencies,
)
from skills.scanner import USER_SKILLS_BASE

RUNTIME_ROOT = Path(os.environ.get("SKILL_RUNTIME_ROOT", "/app/data/runtime_envs"))
PIP_INDEX_URL = os.environ.get("SKILL_RUNTIME_PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")
ALLOW_NETWORK = os.environ.get("ALLOW_SKILL_RUNTIME_NETWORK", "true").lower() in {"1", "true", "yes", "on"}
ALLOW_AUTO_INSTALL = os.environ.get("ALLOW_SKILL_AUTO_PIP_INSTALL", "true").lower() in {"1", "true", "yes", "on"}
MAX_PACKAGES = int(os.environ.get("SKILL_RUNTIME_MAX_PACKAGES", "80"))
INSTALL_TIMEOUT_SECONDS = int(os.environ.get("SKILL_RUNTIME_INSTALL_TIMEOUT_SECONDS", "300"))
INSTALL_FAILURE_CACHE_SECONDS = max(
    0,
    int(os.environ.get("SKILL_RUNTIME_INSTALL_FAILURE_CACHE_SECONDS", "30")),
)
MAX_LOG_CHARS = 40_000
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
REGISTRY_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_.,-]+\])?"
    r"(?:\s*(?:===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9*_.+!-]+"
    r"(?:\s*,\s*(?:===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9*_.+!-]+)*)?"
    r"(?:\s*;\s*[^\r\n]+)?$"
)


class _InitializationFlight:
    """One in-process installation flight shared by equivalent callers."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.callers = 0


_INITIALIZATION_FLIGHTS: dict[tuple[str, str, str], _InitializationFlight] = {}


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
        "install_failure_cache_seconds": INSTALL_FAILURE_CACHE_SECONDS,
    }


def preflight_isolated_skill_runtime(
    *,
    requirements: list[str] | tuple[str, ...] | None = None,
    commands: list[str] | tuple[str, ...] | None = None,
    environment_variables: list[str] | tuple[str, ...] | None = None,
    platform_groups: list[dict[str, Any] | list[str] | tuple[str, ...]] | None = None,
    runtime_profile: str | None = None,
) -> dict[str, Any]:
    """Fail closed against the exact network-disabled Skill executor image.

    This path never inspects the harness interpreter, command PATH, or secret
    values, and never installs a dependency.  The sidecar evaluates PEP 508
    requirements and checks only the fixed environment passed to Skill
    subprocesses; the response carries declaration names and status only.
    """

    required_packages = list(dict.fromkeys(
        str(item).strip() for item in requirements or [] if str(item).strip()
    ))
    required_commands = list(dict.fromkeys(
        str(item).strip() for item in commands or [] if str(item).strip()
    ))
    required_environment = list(dict.fromkeys(
        str(item).strip()
        for item in environment_variables or []
        if str(item).strip()
    ))
    source_groups: list[dict[str, Any]] = []
    allowed_groups: list[list[str]] = []
    for raw_group in platform_groups or []:
        if isinstance(raw_group, dict):
            allowed = [
                str(item).strip().casefold()
                for item in raw_group.get("allowed") or []
                if str(item).strip()
            ]
            source_groups.append({
                "source_file": raw_group.get("source_file"),
                "field": raw_group.get("field"),
            })
        elif isinstance(raw_group, (list, tuple)):
            allowed = [
                str(item).strip().casefold()
                for item in raw_group
                if str(item).strip()
            ]
            source_groups.append({})
        else:
            allowed = []
            source_groups.append({})
        allowed_groups.append(list(dict.fromkeys(allowed)))
    checked = bool(
        required_packages
        or required_commands
        or required_environment
        or allowed_groups
        or runtime_profile is not None
    )
    if not checked:
        return {
            "valid": True,
            "checked": False,
            "execution_runtime": "isolated_skill_executor",
            "blockers": [],
            "packages": {"requirements": [], "status": "not_declared"},
        }

    runtime_binding: dict[str, str] | None = None
    socket_path: str | None = None
    if runtime_profile is not None:
        try:
            from tools.skill_runtime_profile import (
                runtime_profile_socket_binding,
            )

            binding = runtime_profile_socket_binding(runtime_profile)
            socket_path = binding.socket_path
            runtime_binding = {
                "runtime_profile": binding.runtime_profile,
                "socket_identity_sha256": (
                    binding.socket_identity_sha256
                ),
            }
        except (ImportError, ValueError) as exc:
            error_code = str(
                getattr(exc, "code", None)
                or "runtime_profile_client_unavailable"
            )
            return {
                "valid": False,
                "checked": True,
                "execution_runtime": "isolated_skill_executor",
                "runtime_binding": {
                    "runtime_profile": str(runtime_profile or ""),
                },
                "blockers": [{
                    "code": "isolated_executor_preflight_unavailable",
                    "items": [error_code],
                }],
                "packages": {
                    "requirements": required_packages,
                    "status": "executor_unavailable",
                },
                "error_code": error_code,
            }

    try:
        from tools.isolated_skill_executor import (
            IsolatedSkillExecutorError,
            probe_isolated_runtime_capabilities,
        )
    except ImportError as exc:
        error_code = "executor_capability_client_unavailable"
        return {
            "valid": False,
            "checked": True,
            "execution_runtime": "isolated_skill_executor",
            "blockers": [{
                "code": "isolated_executor_preflight_unavailable",
                "items": [error_code],
            }],
            "packages": {
                "requirements": required_packages,
                "status": "executor_unavailable",
            },
            "error_code": error_code,
        }
    try:
        probe_kwargs: dict[str, Any] = {
            "requirements": required_packages,
            "commands": required_commands,
            "environment_variables": required_environment,
            "platform_groups": allowed_groups,
        }
        if socket_path is not None:
            probe_kwargs["socket_path"] = socket_path
        response = probe_isolated_runtime_capabilities(
            **probe_kwargs,
        )
    except IsolatedSkillExecutorError as exc:
        error_code = exc.code
        return {
            "valid": False,
            "checked": True,
            "execution_runtime": "isolated_skill_executor",
            "blockers": [{
                "code": "isolated_executor_preflight_unavailable",
                "items": [str(error_code)],
            }],
            "packages": {
                "requirements": required_packages,
                "status": "executor_unavailable",
            },
            "error_code": str(error_code),
        }

    response_identity = response.get("runtime_identity")
    if runtime_profile is not None and (
        not isinstance(response_identity, dict)
        or response_identity.get("runtime_profile") != runtime_profile
    ):
        return {
            "valid": False,
            "checked": True,
            "execution_runtime": "isolated_skill_executor",
            "runtime_identity": response_identity,
            "runtime_binding": runtime_binding,
            "blockers": [{
                "code": "isolated_executor_runtime_profile_mismatch",
                "items": [str(runtime_profile)],
            }],
            "packages": {
                "requirements": required_packages,
                "status": "executor_unavailable",
            },
            "error_code": "runtime_profile_mismatch",
        }
    if runtime_binding is not None:
        capability_identity = {
            "runtime_profile": runtime_binding["runtime_profile"],
            "socket_identity_sha256": runtime_binding[
                "socket_identity_sha256"
            ],
            "runtime_identity": response_identity,
        }
        runtime_binding["capability_identity_sha256"] = hashlib.sha256(
            json.dumps(
                capability_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    missing_commands = [
        str(item["name"])
        for item in response.get("commands") or []
        if item.get("available") is not True
    ]
    missing_environment = [
        str(item["name"])
        for item in response.get("environment_variables") or []
        if item.get("available") is not True
    ]
    platform_mismatches: list[dict[str, Any]] = []
    for index, item in enumerate(response.get("platform_groups") or []):
        if item.get("satisfied") is True:
            continue
        source = source_groups[index] if index < len(source_groups) else {}
        platform_mismatches.append({
            "current": item.get("current"),
            "allowed": item.get("allowed"),
            **{key: value for key, value in source.items() if value is not None},
        })
    unsatisfied_packages = [
        {
            key: item.get(key)
            for key in (
                "requirement",
                "status",
                "installed_version",
                "unsatisfied_dependencies",
            )
            if item.get(key) is not None
        }
        for item in response.get("requirements") or []
        if item.get("satisfied") is not True
    ]
    blockers: list[dict[str, Any]] = []
    if missing_commands:
        blockers.append({
            "code": "missing_required_commands",
            "items": missing_commands,
        })
    if missing_environment:
        blockers.append({
            "code": "missing_required_environment_variables",
            "items": missing_environment,
        })
    if platform_mismatches:
        blockers.append({
            "code": "unsupported_runtime_platform",
            "items": platform_mismatches,
        })
    if unsatisfied_packages:
        blockers.append({
            "code": "unsatisfied_python_dependencies",
            "items": unsatisfied_packages,
        })
    return {
        "valid": not blockers,
        "checked": True,
        "execution_runtime": "isolated_skill_executor",
        "runtime_identity": response.get("runtime_identity"),
        **(
            {"runtime_binding": runtime_binding}
            if runtime_binding is not None else {}
        ),
        "blockers": blockers,
        "packages": {
            "requirements": required_packages,
            "status": "satisfied" if not unsatisfied_packages else "unsatisfied",
            "results": response.get("requirements") or [],
        },
        "commands": response.get("commands") or [],
        "environment_variables": response.get("environment_variables") or [],
        "platform_groups": response.get("platform_groups") or [],
    }


def preflight_declared_skill_dependencies(skill_dir: str | Path) -> dict[str, Any]:
    """Check one Skill package's explicit Python dependencies in the sidecar."""

    report = scan_declared_skill_dependencies(skill_dir)
    unsupported = [str(item) for item in report.get("unsupported") or []]
    requirements = [
        str(item) for item in report.get("python_packages") or [] if str(item)
    ]
    if unsupported:
        return {
            "valid": False,
            "checked": True,
            "execution_runtime": "isolated_skill_executor",
            "blockers": [{
                "code": "unsupported_declared_runtime_dependencies",
                "items": unsupported,
            }],
            "packages": {
                "requirements": requirements,
                "status": "unsupported",
            },
            "declaration_sources": report.get("sources") or [],
            "warnings": report.get("warnings") or [],
        }
    result = preflight_isolated_skill_runtime(requirements=requirements)
    result["declaration_sources"] = report.get("sources") or []
    result["warnings"] = report.get("warnings") or []
    return result


def preflight_skill_entrypoint_runtime(
    skill_dir: str | Path,
    entrypoint: str,
    *,
    expected_package_sha256: str | None = None,
    expected_script_sha256: str | None = None,
    requirements: list[str] | tuple[str, ...] | None = None,
    commands: list[str] | tuple[str, ...] | None = None,
    environment_variables: list[str] | tuple[str, ...] | None = None,
    platform_groups: (
        list[dict[str, Any] | list[str] | tuple[str, ...]] | None
    ) = None,
) -> dict[str, Any]:
    """Preflight the executor selected from one immutable exact entrypoint.

    The package snapshot is also the profile-selection authority.  Optional
    expected digests let a capability catalog prove that this activation
    check still addresses the same bytes it authorized.
    """

    try:
        from tools.isolated_skill_executor import (
            IsolatedSkillExecutorError,
            snapshot_skill_package,
        )
        from tools.skill_runtime_profile import (
            select_skill_runtime_profile,
        )

        snapshot = snapshot_skill_package(Path(skill_dir))
        selection = select_skill_runtime_profile(snapshot, entrypoint)
    except (ImportError, ValueError) as exc:
        error_code = str(
            getattr(exc, "code", None)
            or "skill_runtime_profile_unavailable"
        )
        return {
            "valid": False,
            "checked": True,
            "execution_runtime": "isolated_skill_executor",
            "blockers": [{
                "code": "isolated_executor_preflight_unavailable",
                "items": [error_code],
            }],
            "packages": {
                "requirements": list(requirements or ()),
                "status": "executor_unavailable",
            },
            "error_code": error_code,
        }

    digest_mismatches: list[str] = []
    if (
        expected_package_sha256 is not None
        and selection.package_sha256 != expected_package_sha256
    ):
        digest_mismatches.append("package_sha256")
    if (
        expected_script_sha256 is not None
        and selection.script_sha256 != expected_script_sha256
    ):
        digest_mismatches.append("script_sha256")
    if digest_mismatches:
        return {
            "valid": False,
            "checked": True,
            "execution_runtime": "isolated_skill_executor",
            "runtime_profile": selection.runtime_profile,
            "package_sha256": selection.package_sha256,
            "script_sha256": selection.script_sha256,
            "blockers": [{
                "code": "skill_runtime_profile_authority_mismatch",
                "items": digest_mismatches,
            }],
            "packages": {
                "requirements": list(requirements or ()),
                "status": "executor_unavailable",
            },
            "error_code": "skill_runtime_profile_authority_mismatch",
        }

    required_packages = list(dict.fromkeys([
        *(str(item) for item in selection.runtime_requirements),
        *(
            str(item).strip()
            for item in requirements or ()
            if str(item).strip()
        ),
    ]))
    required_commands = list(dict.fromkeys([
        *(str(item) for item in selection.runtime_commands),
        *(
            str(item).strip()
            for item in commands or ()
            if str(item).strip()
        ),
    ]))
    result = preflight_isolated_skill_runtime(
        requirements=required_packages,
        commands=required_commands,
        environment_variables=environment_variables,
        platform_groups=platform_groups,
        runtime_profile=selection.runtime_profile,
    )
    result["entrypoint_runtime"] = {
        "entrypoint": selection.entrypoint,
        "runtime_profile": selection.runtime_profile,
        "package_sha256": selection.package_sha256,
        "script_sha256": selection.script_sha256,
        "reachable_sources": list(selection.reachable_sources),
        "required_cwd": selection.required_cwd,
        "runtime_manifest_path": selection.runtime_manifest_path,
        "runtime_manifest_sha256": (
            selection.runtime_manifest_sha256
        ),
    }
    return result


async def ensure_session_runtime(
    user_id: str,
    session_id: str,
    *,
    extra_skill_dirs: list[str | Path] | None = None,
    target_skill_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Ensure a Python environment for a session or one exact session Skill.

    The default remains the historical session-wide dependency aggregation
    used by managed workspace code and MCP runtimes.  ``target_skill_dir`` is
    an additive, internal-facing scope used for a concrete Skill entrypoint;
    it prevents an unrelated Skill in the same session from changing or
    blocking that entrypoint's environment.
    """
    safe_user = _safe_component(user_id, "user_id")
    safe_session = _safe_component(session_id, "session_id")
    reports = _scan_session_reports(
        safe_user,
        safe_session,
        extra_skill_dirs=extra_skill_dirs,
        target_skill_dir=target_skill_dir,
    )
    manifest = aggregate_dependency_reports(reports)
    packages = list(manifest.get("python_packages") or [])
    manifest["policy"] = runtime_policy()
    manifest["user_id"] = safe_user
    manifest["session_id"] = safe_session
    if target_skill_dir is not None:
        manifest["dependency_scope"] = "target_skill"
        manifest["target_skill_dir"] = str(Path(target_skill_dir).resolve())
    else:
        manifest["dependency_scope"] = "session"

    unsafe_requirements = [
        requirement for requirement in packages
        if not _safe_registry_requirement(requirement)
    ]
    if unsafe_requirements:
        manifest["unsupported"] = list(manifest.get("unsupported") or []) + [
            f"unsafe_python_requirement:{requirement}"
            for requirement in unsafe_requirements
        ]
        packages = [
            requirement for requirement in packages
            if requirement not in unsafe_requirements
        ]
        manifest["python_packages"] = packages

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

    cached = _reusable_runtime_status(status_path, python_bin)
    if cached is not None:
        return cached

    flight_key = (safe_user, safe_session, env_hash)
    flight = _join_initialization_flight(flight_key)
    try:
        async with flight.lock:
            # A concurrent caller may have completed (or recently failed) the
            # exact same manifest while this caller waited for the flight.
            cached = _reusable_runtime_status(status_path, python_bin)
            if cached is not None:
                return cached

            env_dir.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            _write_status_file(status_path, {
                "status": "installing",
                "env_hash": env_hash,
                "env_dir": str(env_dir),
                "manifest": manifest,
            })

            try:
                if not python_bin.exists():
                    await _run_command(
                        [sys.executable, "-m", "venv", str(venv_dir)],
                        timeout=120,
                    )
                pip = _venv_pip(venv_dir)
                await _run_command(
                    [
                        str(python_bin), "-m", "pip", "--isolated", "install", "--upgrade",
                        "pip", "setuptools", "wheel",
                    ],
                    timeout=INSTALL_TIMEOUT_SECONDS,
                    log_path=install_log_path,
                )
                install_cmd = [
                    str(pip),
                    "--isolated",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--prefer-binary",
                ]
                if PIP_INDEX_URL:
                    install_cmd.extend(["-i", PIP_INDEX_URL])
                install_cmd.extend(packages)
                await _run_command(
                    install_cmd,
                    timeout=INSTALL_TIMEOUT_SECONDS,
                    log_path=install_log_path,
                )
                freeze = await _run_command(
                    [str(pip), "--isolated", "freeze"], timeout=60
                )
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
                failed_at = time.time()
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
                        "failed_at": failed_at,
                        "retry_after": failed_at + INSTALL_FAILURE_CACHE_SECONDS,
                    },
                )
    finally:
        _leave_initialization_flight(flight_key, flight)


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


def runtime_env_for_subprocess(
    status: dict[str, Any],
    base_env: dict[str, str],
    *,
    user_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, str]:
    env = dict(base_env)
    venv_path = status.get("venv_path")
    if isinstance(venv_path, str) and venv_path:
        bin_dir = str(Path(venv_path) / "bin")
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = venv_path
    manifest = status.get("manifest")
    target_skill_dir = (
        manifest.get("target_skill_dir")
        if isinstance(manifest, dict)
        and manifest.get("dependency_scope") == "target_skill"
        else None
    )
    python_paths = (
        _target_skill_python_paths(target_skill_dir)
        if isinstance(target_skill_dir, str) and target_skill_dir
        else _session_python_paths(user_id, session_id)
    )
    if python_paths:
        existing = env.get("PYTHONPATH")
        if existing:
            python_paths.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
    return env


def _target_skill_python_paths(target_skill_dir: str) -> list[str]:
    target = Path(target_skill_dir).resolve()
    if not target.is_dir() or not (target / "SKILL.md").is_file():
        return []
    paths = [str(target)]
    scripts = (target / "scripts").resolve()
    if scripts.is_dir():
        try:
            scripts.relative_to(target)
        except ValueError:
            return paths
        paths.append(str(scripts))
    return paths


def _session_python_paths(user_id: str | None, session_id: str | None) -> list[str]:
    if not user_id or not session_id:
        return []
    try:
        safe_user = _safe_component(user_id, "user_id")
        safe_session = _safe_component(session_id, "session_id")
    except ValueError:
        return []
    root = (USER_SKILLS_BASE / safe_user / safe_session).resolve()
    if not root.is_dir():
        return []
    paths: list[str] = []
    for skill_dir in sorted(path.parent.resolve() for path in root.rglob("SKILL.md") if path.is_file()):
        paths.append(str(skill_dir))
        scripts = (skill_dir / "scripts").resolve()
        if scripts.is_dir():
            paths.append(str(scripts))
    return paths


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
    target_skill_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    if target_skill_dir is not None:
        target = _validated_target_skill_dir(user_id, session_id, target_skill_dir)
        report = scan_skill_dependencies(target)
        report["skill_dir"] = str(target)
        return [report]

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
    if session_root.is_dir():
        for root in _manifest_roots(session_root, roots):
            key = str(root.resolve())
            if key in seen:
                continue
            seen.add(key)
            report = scan_dependency_manifests(root)
            report["manifest_dir"] = key
            reports.append(report)
    for root in roots:
        key = str(root.resolve())
        if key in seen:
            continue
        seen.add(key)
        report = scan_skill_dependencies(root)
        report["skill_dir"] = key
        reports.append(report)
    return reports


def _manifest_roots(session_root: Path, skill_roots: list[Path]) -> list[Path]:
    skill_root_set = {str(path.resolve()) for path in skill_roots}
    roots: list[Path] = []
    for path in [session_root, *[p for p in session_root.iterdir() if p.is_dir()]]:
        resolved = str(path.resolve())
        if resolved in skill_root_set:
            continue
        if _has_dependency_manifest(path):
            roots.append(path)
    return roots


def _has_dependency_manifest(path: Path) -> bool:
    if not path.is_dir():
        return False
    for child in path.iterdir():
        name = child.name
        if child.is_file() and (
            (name.startswith("requirements") and name.endswith(".txt"))
            or name in {"pyproject.toml", "setup.cfg", "Pipfile", "environment.yml", "environment.yaml"}
        ):
            return True
    return False


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
        "dependency_scope": manifest.get("dependency_scope"),
        "target_skill_dir": manifest.get("target_skill_dir"),
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
        **({"start_new_session": True} if os.name == "posix" else {}),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_subprocess(proc)
        raise RuntimeError(f"Command timed out after {timeout}s: {cmd[0]}")
    except asyncio.CancelledError:
        # The caller's total runtime budget can expire while venv/pip is still
        # running.  Reap it before propagating cancellation so no installer is
        # left mutating the environment after run_skill_python has returned.
        await _terminate_subprocess(proc)
        raise
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
    # Package metadata/build hooks are untrusted. Never forward model/backend
    # endpoints, internal API tokens, cloud credentials, or arbitrary PIP_*
    # configuration from the harness process.
    env = {
        key: value
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR")
        if (value := os.environ.get(key))
    }
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    if PIP_INDEX_URL:
        env["PIP_INDEX_URL"] = PIP_INDEX_URL
    return env


def _safe_cmd(cmd: list[str]) -> str:
    return " ".join(str(part) for part in cmd[:4]) + (" ..." if len(cmd) > 4 else "")


async def _terminate_subprocess(proc: asyncio.subprocess.Process) -> None:
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
        await proc.communicate()
    except (BrokenPipeError, ConnectionResetError, ProcessLookupError):
        try:
            await proc.wait()
        except (ChildProcessError, ProcessLookupError):
            pass


def _safe_registry_requirement(value: Any) -> bool:
    requirement = str(value or "").strip()
    if not requirement or len(requirement) > 512:
        return False
    folded = requirement.casefold()
    if (
        "://" in folded
        or folded.startswith(("git+", "file:", "/", "./", "../", "~"))
        or " @ " in folded
        or "\\" in requirement
    ):
        return False
    return bool(REGISTRY_REQUIREMENT_RE.fullmatch(requirement))


def _validated_target_skill_dir(
    user_id: str,
    session_id: str,
    target_skill_dir: str | Path,
) -> Path:
    target = Path(target_skill_dir).resolve()
    session_root = (USER_SKILLS_BASE / user_id / session_id).resolve()
    try:
        target.relative_to(session_root)
    except ValueError as exc:
        raise ValueError("target_skill_dir must belong to the current session.") from exc
    if not target.is_dir() or not (target / "SKILL.md").is_file():
        raise ValueError("target_skill_dir must contain SKILL.md.")
    return target


def _join_initialization_flight(key: tuple[str, str, str]) -> _InitializationFlight:
    flight = _INITIALIZATION_FLIGHTS.get(key)
    if flight is None:
        flight = _InitializationFlight()
        _INITIALIZATION_FLIGHTS[key] = flight
    flight.callers += 1
    return flight


def _leave_initialization_flight(
    key: tuple[str, str, str],
    flight: _InitializationFlight,
) -> None:
    flight.callers -= 1
    if flight.callers <= 0 and _INITIALIZATION_FLIGHTS.get(key) is flight:
        _INITIALIZATION_FLIGHTS.pop(key, None)


def _reusable_runtime_status(status_path: Path, python_bin: Path) -> dict[str, Any] | None:
    try:
        existing = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if existing.get("status") == "ready" and python_bin.exists():
        return existing
    if existing.get("status") != "install_failed" or INSTALL_FAILURE_CACHE_SECONDS <= 0:
        return None

    now = time.time()
    try:
        retry_after = float(existing.get("retry_after"))
    except (TypeError, ValueError):
        try:
            retry_after = status_path.stat().st_mtime + INSTALL_FAILURE_CACHE_SECONDS
        except OSError:
            return None
    if retry_after <= now:
        return None
    cached = dict(existing)
    cached["negative_cache"] = True
    cached["retry_after_seconds"] = max(0.0, retry_after - now)
    return cached


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
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(status, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


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
