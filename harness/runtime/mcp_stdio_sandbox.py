"""Trusted privilege boundary for stdio MCP server subprocesses.

This module is launched with ``python -I`` by the Harness.  It deliberately
receives the eventual child environment as inert encoded data, drops the
Harness process identity and privilege-gaining ability, and only then exposes
that environment to the configured executable.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
import resource
import sys


_SPEC_ENV = "CHATDS_MCP_CHILD_SPEC_B64"
_UNPRIVILEGED_UID = 65534
_UNPRIVILEGED_GID = 65534
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38


def _fail(message: str) -> "NoReturn":
    print(f"stdio MCP sandbox: {message}", file=sys.stderr, flush=True)
    raise SystemExit(126)


def _decode_spec() -> tuple[dict[str, str], Path]:
    encoded = os.environ.pop(_SPEC_ENV, "")
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
        )
    except Exception:
        _fail("invalid child specification")
    env_value = payload.get("env") if isinstance(payload, dict) else None
    home_value = payload.get("home") if isinstance(payload, dict) else None
    if not isinstance(env_value, dict) or not isinstance(home_value, str):
        _fail("malformed child specification")
    child_env: dict[str, str] = {}
    for key, value in env_value.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "=" in key
            or "\x00" in key
            or "\x00" in value
        ):
            _fail("unsafe child environment")
        child_env[key] = value
    home = Path(home_value)
    if home.parent != Path("/tmp") or not home.name.startswith("chatds-mcp-"):
        _fail("unsafe sandbox home")
    return child_env, home


def _prctl(option: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        _fail(f"prctl({option}) failed: errno {err}")


def _drop_privileges() -> None:
    if os.geteuid() == 0:
        os.setgroups([])
        os.setresgid(_UNPRIVILEGED_GID, _UNPRIVILEGED_GID, _UNPRIVILEGED_GID)
        os.setresuid(_UNPRIVILEGED_UID, _UNPRIVILEGED_UID, _UNPRIVILEGED_UID)
    elif os.geteuid() != _UNPRIVILEGED_UID:
        _fail("launcher lacks the expected privilege transition")
    _prctl(_PR_SET_NO_NEW_PRIVS, 1)
    _prctl(_PR_SET_DUMPABLE, 0)
    os.umask(0o077)
    # These are availability boundaries, not a filesystem/network sandbox.
    # They keep one extensible MCP server from exhausting common process-local
    # resources while leaving ordinary persistent stdio servers functional.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    address_space = 2 * 1024 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    file_size = 256 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--" or not argv[2]:
        _fail("missing child command")
    child_env, home = _decode_spec()
    # Do not let the configured child observe the trusted launcher's ambient
    # environment, including the Harness internal API token.
    os.environ.clear()
    _drop_privileges()
    home.mkdir(mode=0o700, parents=False, exist_ok=False)
    for relative in (".cache", ".config", ".local"):
        (home / relative).mkdir(mode=0o700, exist_ok=True)
    command = argv[2]
    child_argv = argv[2:]
    os.execvpe(command, child_argv, child_env)
    return 126


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
