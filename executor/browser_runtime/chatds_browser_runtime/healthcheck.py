"""Static profile validation and optional real-browser smoke checks."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

from . import PROFILE_ID


PROFILE_PATH = Path("/opt/chatds-browser-runtime/profile.json")
INSTALLED_PATH = Path("/opt/chatds-browser-runtime/installed-manifest.json")
NODE_MODULES = "/opt/chatds-browser-runtime/node_modules"
PLAYWRIGHT_BROWSERS = "/opt/chatds-browser-runtime/ms-playwright"
COMMON_REQUIREMENTS_PATH = Path(
    "/opt/chatds-browser-runtime/common-python-requirements.in"
)


class HealthError(RuntimeError):
    """The immutable browser runtime does not match its declaration."""


def _run(command: list[str], *, timeout: float = 10) -> str:
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "NODE_PATH": NODE_MODULES,
        "PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT_BROWSERS,
        "LANG": "C.UTF-8",
    }
    for name in ("HOME", "TMPDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    if os.environ.get("SKILL_EGRESS_PROXY_URL"):
        environment["SKILL_EGRESS_PROXY_URL"] = os.environ[
            "SKILL_EGRESS_PROXY_URL"
        ]
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        env=environment,
    )
    return completed.stdout.strip()


def _major(text: str) -> int:
    match = re.search(r"\b(\d+)\.", text)
    if match is None:
        raise HealthError("browser version output has no major version")
    return int(match.group(1))


def _common_requirement_names(
    path: Path = COMMON_REQUIREMENTS_PATH,
) -> tuple[str, ...]:
    names: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise HealthError("common Python requirement manifest is invalid") from exc
        names.append(requirement.name)
    if not names or len(set(name.lower() for name in names)) != len(names):
        raise HealthError("common Python requirement manifest is empty or duplicated")
    return tuple(names)


def collect_installed() -> dict[str, Any]:
    """Collect exact versions while package metadata is still available."""

    packages: dict[str, str] = {}
    for package in (
        "chromium",
        "chromium-driver",
        "util-linux",
        "weston",
    ):
        packages[package] = _run(
            ["/usr/bin/dpkg-query", "-W", "-f=${Version}", package]
        )
    node_playwright = _run(
        [
            "/usr/local/bin/node",
            "-e",
            "process.stdout.write(require('playwright/package.json').version)",
        ]
    )
    return {
        "profile_id": PROFILE_ID,
        "node": _run(["/usr/local/bin/node", "--version"]).removeprefix("v"),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "node_playwright": node_playwright,
        "python_playwright": importlib.metadata.version("playwright"),
        "selenium": importlib.metadata.version("selenium"),
        "packaging": importlib.metadata.version("packaging"),
        "common_python": {
            name: importlib.metadata.version(name)
            for name in _common_requirement_names()
        },
        "debian_packages": packages,
    }


def validate_static(
    profile_path: Path = PROFILE_PATH,
    installed_path: Path = INSTALLED_PATH,
) -> dict[str, Any]:
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    installed = json.loads(installed_path.read_text(encoding="utf-8"))
    if profile.get("profile_id") != PROFILE_ID or installed.get("profile_id") != PROFILE_ID:
        raise HealthError("profile identity mismatch")
    expected = {
        "node": profile["interpreters"]["node"],
        "python": profile["interpreters"]["python"],
        "node_playwright": profile["libraries"]["node_playwright"],
        "python_playwright": profile["libraries"]["python_playwright"],
        "selenium": profile["libraries"]["selenium"],
        "packaging": profile["libraries"]["packaging"],
    }
    for name, version in expected.items():
        if installed.get(name) != version:
            raise HealthError(f"{name} version does not match the immutable profile")

    if profile.get("common_python_manifest") != str(COMMON_REQUIREMENTS_PATH):
        raise HealthError("common Python manifest path does not match the profile")
    common_python = installed.get("common_python")
    if not isinstance(common_python, dict):
        raise HealthError("installed common Python dependency manifest is missing")
    for name in _common_requirement_names():
        installed_version = common_python.get(name)
        if (
            not isinstance(installed_version, str)
            or not installed_version
            or importlib.metadata.version(name) != installed_version
        ):
            raise HealthError(f"common Python dependency mismatch: {name}")

    packages = installed.get("debian_packages", {})
    if not isinstance(packages, dict) or not all(
        isinstance(packages.get(name), str) and packages[name]
        for name in (
            "chromium",
            "chromium-driver",
            "util-linux",
            "weston",
        )
    ):
        raise HealthError("installed Debian package manifest is incomplete")
    if _major(packages["chromium"]) != _major(packages["chromium-driver"]):
        raise HealthError("Chromium and ChromeDriver majors do not match")

    controller_uid = profile["identities"]["controller_uid"]
    worker_uid = pwd.getpwuid(profile["identities"]["worker_uid"]).pw_uid
    proxy_uid = pwd.getpwuid(profile["identities"]["proxy_uid"]).pw_uid
    if controller_uid == worker_uid or proxy_uid == worker_uid:
        raise HealthError("controller/proxy and worker identities are not separated")
    prlimit = Path("/usr/bin/prlimit")
    prlimit_info = prlimit.stat()
    if (
        not prlimit.is_file()
        or prlimit_info.st_uid != 0
        or prlimit_info.st_mode & 0o022
    ):
        raise HealthError("root-owned prlimit launcher is unavailable")
    for forbidden in ("npm", "npx", "pip", "pip3", "apt", "apt-get"):
        if shutil.which(forbidden):
            raise HealthError(f"runtime installer remains available: {forbidden}")
    expected_browser = _run(
        [
            "/usr/local/bin/node",
            "-e",
            "process.stdout.write(require('playwright').chromium.executablePath())",
        ]
    )
    if Path(expected_browser).resolve() != Path(
        "/usr/local/bin/chatds-chromium-proxy"
    ).resolve():
        raise HealthError("Playwright is not bound to the controlled Chromium wrapper")
    if os.environ.get("EXECUTOR_RUNTIME_PROFILE") == PROFILE_ID:
        for fixed_path in (Path("/workspace"), Path("/tmp"), Path("/dev/shm")):
            if os.access(fixed_path, os.W_OK):
                raise HealthError(
                    f"worker can write fixed cross-lease path: {fixed_path}"
                )
    return installed


def browser_smoke() -> None:
    for script in (
        "/opt/chatds-browser-runtime/smoke/node_playwright.cjs",
        "/opt/chatds-browser-runtime/smoke/node_playwright.mjs",
        "/opt/chatds-browser-runtime/smoke/python_browsers.py",
    ):
        _run(
            ["/usr/local/bin/chatds-browser-runtime-exec", script],
            timeout=90,
        )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-installed-manifest", type=Path)
    parser.add_argument("--browser-smoke", action="store_true")
    parsed = parser.parse_args(arguments)
    try:
        if parsed.write_installed_manifest is not None:
            payload = collect_installed()
            parsed.write_installed_manifest.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            payload = validate_static()
            if parsed.browser_smoke:
                browser_smoke()
            print(
                json.dumps(
                    {
                        "ok": True,
                        "profile_id": payload["profile_id"],
                        "browser_smoke": parsed.browser_smoke,
                    },
                    sort_keys=True,
                )
            )
    except (
        HealthError,
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        KeyError,
    ) as exc:
        print(f"browser runtime unhealthy: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
