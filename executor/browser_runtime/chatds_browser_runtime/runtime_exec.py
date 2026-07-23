"""Launch one exact Skill browser script inside a prebuilt worker."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

from .policy import ProxyPolicy, load_proxy_environment, proxy_environment


WORKER_UID = 65529
NODE = "/usr/local/bin/node"
PYTHON = "/usr/local/bin/python"
BASH = "/bin/bash"
WESTON = "/usr/bin/weston"
NODE_MODULES = "/opt/chatds-browser-runtime/node_modules"
PLAYWRIGHT_BROWSERS = "/opt/chatds-browser-runtime/ms-playwright"
CHROMIUM_WRAPPER = "/usr/local/bin/chatds-chromium-proxy"
_NODE_EXTENSIONS = frozenset({".cjs", ".js", ".mjs"})
_PYTHON_EXTENSIONS = frozenset({".py"})
_SHELL_EXTENSIONS = frozenset({".sh", ".bash"})


class LaunchError(RuntimeError):
    """The requested worker script cannot be launched safely."""


def command_for_script(script: Path, arguments: list[str]) -> list[str]:
    """Return the fixed interpreter command for an exact regular script."""

    try:
        metadata = script.lstat()
    except OSError as exc:
        raise LaunchError("Skill script does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise LaunchError("Skill script must be a regular non-symlink file")
    extension = script.suffix.lower()
    if extension in _NODE_EXTENSIONS:
        interpreter = [NODE]
    elif extension in _PYTHON_EXTENSIONS:
        interpreter = [PYTHON, "-I"]
    elif extension in _SHELL_EXTENSIONS:
        interpreter = [BASH, "--noprofile", "--norc"]
    else:
        raise LaunchError(
            "browser profile supports only .cjs/.js/.mjs, .py, .sh, and .bash scripts"
        )
    return [*interpreter, str(script.resolve()), *arguments]


def worker_environment(policy: ProxyPolicy, *, process_id: int) -> dict[str, str]:
    """Build a small deterministic environment owned by the runtime."""

    home = _private_runtime_directory("HOME")
    temporary = _private_runtime_directory("TMPDIR")
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "XDG_RUNTIME_DIR": str(temporary),
        "MPLCONFIGDIR": str(home / ".config/matplotlib"),
        "NODE_PATH": NODE_MODULES,
        "PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT_BROWSERS,
        "BROWSER_EXECUTABLE": CHROMIUM_WRAPPER,
        "CHROME_BIN": CHROMIUM_WRAPPER,
        "SE_OFFLINE": "true",
        "SE_AVOID_STATS": "true",
        "SE_AVOID_BROWSER_DOWNLOAD": "true",
        "PYTHONUNBUFFERED": "1",
    }
    for name in (
        "CHATDS_WORKSPACE",
        "CHATDS_SKILL_DIR",
        "CHATDS_SKILL_ROOT",
        "SKILL_DIR",
        "CHATDS_OUTPUT_DIR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "BLIS_NUM_THREADS",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    environment.update(proxy_environment(policy))
    return environment


def _private_runtime_directory(name: str) -> Path:
    raw = os.environ.get(name, "")
    path = Path(raw)
    if not raw or not path.is_absolute():
        raise LaunchError(f"runtime-owned {name} directory is required")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise LaunchError(f"runtime-owned {name} directory is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path
        or metadata.st_uid != WORKER_UID
        or metadata.st_gid != WORKER_UID
        or metadata.st_mode & 0o077
    ):
        raise LaunchError(f"runtime-owned {name} directory is not private")
    return path


def _start_weston(
    environment: dict[str, str],
) -> tuple[subprocess.Popen[bytes], dict[str, str], tuple[Path, ...]]:
    """Start one worker-owned compositor on a lease-private Wayland socket."""

    temporary = Path(environment["TMPDIR"])
    socket_name = f"wayland-chatds-{os.getpid()}"
    socket_path = temporary / socket_name
    lock_path = temporary / f"{socket_name}.lock"
    log_path = temporary / f"weston-chatds-{os.getpid()}.log"
    try:
        process = subprocess.Popen(
            [
                WESTON,
                "--backend=headless-backend.so",
                f"--socket={socket_name}",
                "--idle-time=0",
                "--no-config",
                f"--log={log_path}",
                "--width=1440",
                "--height=1000",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchError("could not prepare the private Wayland compositor") from exc

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = ""
            try:
                detail = log_path.read_text(
                    encoding="utf-8", errors="replace"
                )[-2_000:].strip()
            except OSError:
                pass
            suffix = f": {detail}" if detail else ""
            raise LaunchError(
                f"private Weston exited before becoming ready{suffix}"
            )
        try:
            metadata = socket_path.lstat()
        except OSError:
            time.sleep(0.02)
            continue
        if (
            stat.S_ISSOCK(metadata.st_mode)
            and metadata.st_uid == WORKER_UID
            and metadata.st_gid == WORKER_UID
        ):
            break
        _stop_process_group(process)
        raise LaunchError("private Wayland socket has unsafe ownership or type")
    else:
        _stop_process_group(process)
        raise LaunchError("private Weston did not become ready")

    child_environment = dict(environment)
    child_environment.pop("DISPLAY", None)
    child_environment.pop("XAUTHORITY", None)
    child_environment["WAYLAND_DISPLAY"] = socket_name
    child_environment["XDG_SESSION_TYPE"] = "wayland"
    return process, child_environment, (socket_path, lock_path, log_path)


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    """Boundedly terminate a process and every descendant in its session."""

    # start_new_session=True makes the original leader PID the process-group
    # identity. The leader may have already exited while Chromium descendants
    # remain, so never use process.poll() as evidence that the group is empty.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            process.wait()
            return
        time.sleep(0.02)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if not arguments:
        print("usage: chatds-browser-runtime-exec SCRIPT [ARG ...]", file=sys.stderr)
        return 64
    if os.geteuid() != WORKER_UID:
        print("browser scripts must run as the dedicated worker UID", file=sys.stderr)
        return 77

    try:
        command = command_for_script(Path(arguments[0]), arguments[1:])
        policy = load_proxy_environment()
    except (LaunchError, RuntimeError) as exc:
        print(f"chatds browser launch error: {exc}", file=sys.stderr)
        return 78

    try:
        environment = worker_environment(policy, process_id=os.getpid())
        compositor, child_environment, compositor_paths = _start_weston(environment)
        child = subprocess.Popen(
            command,
            env=child_environment,
            close_fds=True,
            start_new_session=True,
        )
        return child.wait()
    except (LaunchError, OSError) as exc:
        print(f"chatds browser launch error: {exc}", file=sys.stderr)
        return 78
    finally:
        if "child" in locals():
            _stop_process_group(child)
        if "compositor" in locals():
            _stop_process_group(compositor)
        for path in locals().get("compositor_paths", ()):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                print(
                    "chatds browser cleanup error: private compositor state "
                    "could not be removed",
                    file=sys.stderr,
                )
                return 78


if __name__ == "__main__":
    raise SystemExit(main())
