"""Network-isolated execution daemon.

The container is launched with ``network_mode: none`` and communicates with
the harness only through a shared Unix-domain socket. Protocol-v1 supports
session-code and Skill-script requests (including declarative public-function
calls); a legacy source-only calculation form remains for compatibility.
Skill execution receives immutable Skill file bytes plus a disposable
session-workspace snapshot; neither host tree is mounted into the executor.
Protocol-v2 adds authenticated, scope/digest-bound persistent process leases
without changing protocol-v1 one-shot semantics.
"""

from __future__ import annotations

import ast
import base64
import binascii
from collections import OrderedDict
import ctypes
from dataclasses import dataclass, field
import hashlib
import hmac
import importlib.metadata
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import secrets
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement


SOCKET_PATH = Path(os.environ.get(
    "EXECUTOR_SOCKET", "/run/chat-ds-executor/executor.sock"
))

PROTOCOL_VERSION = 1
PROCESS_PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 96 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_STDOUT_BYTES = 80_000
MAX_STDERR_BYTES = 20_000
MAX_CODE_TIMEOUT = 120
MAX_CODE_BYTES = 900_000
MAX_SKILL_TIMEOUT = 300
MAX_ARGS = 64
MAX_ARG_BYTES = 32_768
MAX_ARG_CHARS = 4_096
MAX_FUNCTION_INPUT_BYTES = 128_000
MAX_FUNCTION_ARGS = 64
MAX_FUNCTION_KWARGS = 128
MAX_FUNCTION_JSON_DEPTH = 20
MAX_FUNCTION_SOURCE_BYTES = 2_000_000
MAX_FUNCTION_RESULT_CHARS = 120_000
MAX_FUNCTION_ENVELOPE_BYTES = 260_000
MAX_PATH_BYTES = 2_048
MAX_PATH_DEPTH = 32
MAX_PATH_COMPONENT_BYTES = 255

MAX_SKILL_FILES = 1_024
MAX_SKILL_FILE_BYTES = 8 * 1024 * 1024
MAX_SKILL_TOTAL_BYTES = 24 * 1024 * 1024
MAX_WORKSPACE_FILES = 512
MAX_WORKSPACE_FILE_BYTES = 24 * 1024 * 1024
MAX_WORKSPACE_TOTAL_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_FILES = 512
MAX_OUTPUT_FILE_BYTES = 24 * 1024 * 1024
MAX_OUTPUT_TOTAL_BYTES = 24 * 1024 * 1024
MAX_OUTPUT_ENTRIES = 4_096
MAX_RUNTIME_REQUIREMENTS = 80
MAX_RUNTIME_COMMANDS = 80
MAX_RUNTIME_ENVIRONMENT_VARIABLES = 80
MAX_RUNTIME_PLATFORM_GROUPS = 32
MAX_RUNTIME_PLATFORMS_PER_GROUP = 32
MAX_RUNTIME_DECLARATION_CHARS = 512
MAX_EGRESS_ORIGINS = 128
MAX_EGRESS_RULES = 256
DEFAULT_EGRESS_MAX_REQUESTS_PER_SCOPE = 2_048
DEFAULT_EGRESS_MAX_OUTBOUND_BYTES_PER_SCOPE = 16 * 1024 * 1024
DEFAULT_EGRESS_MAX_RESPONSE_WIRE_BYTES_PER_SCOPE = 512 * 1024 * 1024
MAX_EGRESS_MAX_REQUESTS_PER_SCOPE = 65_536
MAX_EGRESS_MAX_OUTBOUND_BYTES_PER_SCOPE = 1024 * 1024 * 1024
MAX_EGRESS_MAX_RESPONSE_WIRE_BYTES_PER_SCOPE = 16 * 1024 * 1024 * 1024
_REQUIRE_EGRESS_POLICY_V3_RAW = os.environ.get(
    "EXECUTOR_REQUIRE_EGRESS_POLICY_V3",
    "0",
).strip()
if _REQUIRE_EGRESS_POLICY_V3_RAW not in {"0", "1"}:
    raise RuntimeError("invalid_executor_require_egress_policy_v3")
REQUIRE_EGRESS_POLICY_V3 = _REQUIRE_EGRESS_POLICY_V3_RAW == "1"
EGRESS_METHOD_ORDER = (
    "GET",
    "HEAD",
    "OPTIONS",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
)
EGRESS_METHODS = frozenset(EGRESS_METHOD_ORDER)
SESSION_SANDBOX_RUNTIME_PROFILE = "session-sandbox-v1"
# Linux accounts RLIMIT_NPROC against the host real UID, even across the four
# executor PID/mount namespaces. The homogeneous pool therefore needs a
# deployment-owned aggregate ceiling above one slot's cgroup pids_limit (512).
# Only the unified profile may raise the rlimit this far; each container's
# cgroup remains the actual per-slot process/thread hard bound.
MAX_SESSION_SANDBOX_NPROC = 4_096
RUNTIME_BUILD_IDENTITY_SCHEMA = "chatds-runtime-build-identity-v1"
RUNTIME_BUILD_COMPONENTS: tuple[tuple[str, Path], ...] = (
    ("executor.server", Path(__file__).resolve()),
    (
        "browser.profile",
        Path("/opt/chatds-browser-runtime/profile.json"),
    ),
    (
        "browser.installed_manifest",
        Path("/opt/chatds-browser-runtime/installed-manifest.json"),
    ),
    (
        "browser.module.chromium_proxy",
        Path(
            "/usr/local/lib/python3.12/site-packages/"
            "chatds_browser_runtime/chromium_proxy.py"
        ),
    ),
    (
        "browser.module.healthcheck",
        Path(
            "/usr/local/lib/python3.12/site-packages/"
            "chatds_browser_runtime/healthcheck.py"
        ),
    ),
    (
        "browser.module.policy",
        Path(
            "/usr/local/lib/python3.12/site-packages/"
            "chatds_browser_runtime/policy.py"
        ),
    ),
    (
        "browser.module.proxy_bridge",
        Path(
            "/usr/local/lib/python3.12/site-packages/"
            "chatds_browser_runtime/proxy_bridge.py"
        ),
    ),
    (
        "browser.module.runtime_exec",
        Path(
            "/usr/local/lib/python3.12/site-packages/"
            "chatds_browser_runtime/runtime_exec.py"
        ),
    ),
    (
        "browser.launcher.executor_entrypoint",
        Path("/usr/local/bin/chatds-browser-executor-entrypoint"),
    ),
    (
        "browser.launcher.runtime_exec",
        Path("/usr/local/bin/chatds-browser-runtime-exec"),
    ),
    (
        "browser.launcher.health",
        Path("/usr/local/bin/chatds-browser-runtime-health"),
    ),
    (
        "browser.launcher.chromium_proxy",
        Path("/usr/local/bin/chatds-chromium-proxy"),
    ),
    (
        "browser.launcher.egress_bridge",
        Path("/usr/local/bin/chatds-skill-egress-bridge"),
    ),
)

# Protocol-v2 is an additive, stateful process protocol.  Protocol-v1 remains
# unchanged for existing one-shot callers.  A lease still runs inside this
# network-disabled container and can only execute an entrypoint from the exact
# content-addressed Skill snapshot supplied when the lease is opened.
MAX_PROCESS_LEASES = 1
MAX_PROCESS_LEASES_PER_SCOPE = 1
MIN_PROCESS_LEASE_TTL_SECONDS = 1
MAX_PROCESS_LEASE_TTL_SECONDS = 3_600
MAX_PROCESS_RUNTIME_SECONDS = 3_600
MAX_PROCESS_STDIN_CHUNK_BYTES = 64 * 1024
MAX_PROCESS_STDIN_TOTAL_BYTES = 4 * 1024 * 1024
MAX_PROCESS_CALL_BYTES = 4 * 1024
MAX_PROCESS_STREAM_BYTES = 2 * 1024 * 1024
MAX_PROCESS_READ_BYTES = 256 * 1024
MAX_PROCESS_READ_WAIT_MS = 60_000
MAX_PROCESS_OPERATIONS = 1_024
MAX_PROCESS_OPERATION_CACHE_BYTES = 96 * 1024 * 1024
MAX_PROCESS_CLOSE_CACHE_RESERVE_BYTES = (
    ((MAX_OUTPUT_TOTAL_BYTES + 2) // 3) * 4
    + 2 * 1024 * 1024
)
PROCESS_TOMBSTONE_SECONDS = 60
PROCESS_JANITOR_INTERVAL_SECONDS = 1.0
# A prepared artifact batch is a two-phase transaction: the Harness must first
# apply the bounded batch under its workspace CAS and only then acknowledge the
# executor snapshot.  Idle/max-runtime expiry must not destroy that transaction
# while a slow local filesystem apply is still in progress, but the reservation
# also cannot remain immortal after a dead controller.
PROCESS_SYNC_ACK_GRACE_SECONDS = 300
MAX_PROCESS_OWNER_ID_BYTES = 256
TRUSTED_RESOURCE_LAUNCHER = Path("/usr/bin/prlimit")
TRUSTED_BROWSER_RUNTIME_LAUNCHER = Path(
    "/usr/local/bin/chatds-browser-runtime-exec"
)

_ADDRESS_SPACE_LIMIT_RAW = os.environ.get(
    "EXECUTOR_MAX_ADDRESS_SPACE_BYTES",
    str(2 * 1024 * 1024 * 1024),
).strip()
if _ADDRESS_SPACE_LIMIT_RAW == "unlimited":
    if os.environ.get("EXECUTOR_RUNTIME_PROFILE") not in {
        "browser-automation-v1",
        SESSION_SANDBOX_RUNTIME_PROFILE,
    }:
        raise RuntimeError(
            "An unlimited address-space rlimit is valid only for the "
            "browser-automation-v1 profile."
        )
    # Chromium/V8 uses very large sparse virtual mappings. Physical memory is
    # still hard-bounded by the container cgroup; a finite RLIMIT_AS is not a
    # reliable resident-memory control for this runtime.
    MAX_ADDRESS_SPACE_BYTES: int | None = None
else:
    try:
        MAX_ADDRESS_SPACE_BYTES = int(_ADDRESS_SPACE_LIMIT_RAW)
    except ValueError as exc:
        raise RuntimeError(
            "EXECUTOR_MAX_ADDRESS_SPACE_BYTES must be a positive integer or "
            "the exact browser-profile value 'unlimited'."
        ) from exc
    if MAX_ADDRESS_SPACE_BYTES <= 0:
        raise RuntimeError(
            "EXECUTOR_MAX_ADDRESS_SPACE_BYTES must be positive."
        )

SUPPORTED_INTERPRETERS = {
    ".py": "python",
    ".sh": "bash",
    ".bash": "bash",
    ".js": "node",
    ".mjs": "node",
    ".cjs": "node",
}


def _immutable_snapshot_file_mode(path: Path, *, group_only: bool) -> int:
    """Keep exact Skill scripts executable without making data executable."""

    executable = path.suffix.casefold() in SUPPORTED_INTERPRETERS
    if group_only:
        return 0o550 if executable else 0o440
    return 0o555 if executable else 0o444


RESERVED_WORKSPACE_ROOTS = frozenset({
    ".chatds",
    ".git",
    ".pytest_cache",
    "__pycache__",
    "cache",
    "debug",
})

PUBLIC_FUNCTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
RUNTIME_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
RUNTIME_ENVIRONMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
RUNTIME_PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")

BLAS_THREAD_ENV = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "BLIS_NUM_THREADS": "1",
}

# This is the complete key set passed to an isolated Skill subprocess.  The
# capability probe intentionally checks this fixed set rather than the daemon
# process environment, which may contain deployment-only values that are never
# forwarded to untrusted Skill code.
SKILL_RUNTIME_ENVIRONMENT_VARIABLES = frozenset({
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "CHATDS_WORKSPACE",
    "CHATDS_SKILL_DIR",
    "CHATDS_SKILL_ROOT",
    "SKILL_DIR",
    "CHATDS_OUTPUT_DIR",
    *BLAS_THREAD_ENV,
})


# Trusted, fixed launcher for Python CLI entrypoints.  ``python -I`` correctly
# keeps the process working directory, PYTHONPATH, and the launcher's own
# directory off ``sys.path``, but it also omits the selected script directory.
# Standards-compliant Skills commonly split helpers across sibling modules or
# import a package rooted at the Skill directory.  Restore only those two
# exact, already validated snapshot roots before emulating normal ``python
# script.py ...`` semantics; workspace, other Skills, and daemon source paths
# remain undiscoverable.
CLI_RUNNER_SOURCE = r'''
from __future__ import annotations
from pathlib import Path
import runpy
import sys


def main():
    if len(sys.argv) < 3:
        raise SystemExit("trusted Skill CLI launcher requires a root and entrypoint")

    skill_root = Path(sys.argv[1]).resolve(strict=True)
    entrypoint = Path(sys.argv[2]).resolve(strict=True)
    if not skill_root.is_dir() or not entrypoint.is_file():
        raise SystemExit("trusted Skill CLI launcher received an invalid snapshot path")
    try:
        entrypoint.relative_to(skill_root)
    except ValueError as exc:
        raise SystemExit("Skill entrypoint is outside the exact snapshot root") from exc

    user_argv = sys.argv[3:]
    import_roots = [entrypoint.parent, skill_root]
    inherited = [item for item in sys.path if item]
    sys.path[:] = []
    for candidate in [*import_roots, *inherited]:
        rendered = str(candidate)
        if rendered not in sys.path:
            sys.path.append(rendered)

    sys.argv[:] = [str(entrypoint), *user_argv]
    runpy.run_path(str(entrypoint), run_name="__main__")


if __name__ == "__main__":
    main()
'''


# Trusted, fixed runner source. Request values are passed as ordinary argv and
# JSON files; no user-controlled value is interpolated into this program.
FUNCTION_RUNNER_SOURCE = r'''
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
from pathlib import Path
import sys
import traceback


class CappedText(io.TextIOBase):
    def __init__(self, limit):
        self.limit = max(0, int(limit))
        self.parts = []
        self.size = 0
        self.truncated = False

    def writable(self):
        return True

    def write(self, value):
        text = value if isinstance(value, str) else str(value)
        original = len(text)
        remaining = max(0, self.limit - self.size)
        if remaining:
            kept = text[:remaining]
            self.parts.append(kept)
            self.size += len(kept)
        if original > remaining:
            self.truncated = True
        return original

    def flush(self):
        return None

    def value(self):
        result = "".join(self.parts)
        if self.truncated:
            result += "\n... [truncated]"
        return result


def load_module(script):
    module_name = "_chatds_isolated_" + hashlib.sha256(
        str(script).encode("utf-8")
    ).hexdigest()[:20]
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise ImportError("Could not create an import specification for the Skill script.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(script.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        try:
            sys.path.remove(str(script.parent))
        except ValueError:
            pass
    return module, module_name


def load_function(script, name):
    module, module_name = load_module(script)
    function = module.__dict__.get(name)
    if not callable(function):
        raise ValueError(f"Selected public function {name!r} is not callable.")
    try:
        declared = inspect.unwrap(function)
    except (TypeError, ValueError) as exc:
        raise ValueError("Could not validate the function decorator chain.") from exc
    if not inspect.isfunction(declared):
        raise ValueError("Imported or replaced functions are not callable through this interface.")
    if getattr(declared, "__module__", None) != module_name:
        raise ValueError("Imported or replaced functions are not callable through this interface.")
    if getattr(declared, "__name__", None) != name:
        raise ValueError("Reassigned functions are not callable through this interface.")
    if getattr(declared, "__qualname__", None) != name:
        raise ValueError("Nested or dynamically rebound functions are not callable through this interface.")
    return function


def load_instance_method(script, class_name, method_name):
    module, module_name = load_module(script)
    selected_class = module.__dict__.get(class_name)
    if not inspect.isclass(selected_class):
        raise ValueError(f"Selected public class {class_name!r} is not a class.")
    if type.__getattribute__(selected_class, "__module__") != module_name:
        raise ValueError("Imported or replaced classes are not callable through this interface.")
    if type.__getattribute__(selected_class, "__name__") != class_name:
        raise ValueError("Reassigned classes are not callable through this interface.")
    if type.__getattribute__(selected_class, "__qualname__") != class_name:
        raise ValueError("Nested or dynamically rebound classes are not callable through this interface.")
    method = type.__getattribute__(selected_class, "__dict__").get(method_name)
    if not inspect.isfunction(method):
        raise ValueError(
            "Selected method must be a direct plain instance method; inherited, static, "
            "class, property, and replaced descriptors are rejected."
        )
    if inspect.unwrap(method) is not method:
        raise ValueError("Decorated instance methods are not callable through this interface.")
    if getattr(method, "__module__", None) != module_name:
        raise ValueError("Imported or replaced methods are not callable through this interface.")
    if getattr(method, "__name__", None) != method_name:
        raise ValueError("Reassigned methods are not callable through this interface.")
    if getattr(method, "__qualname__", None) != f"{class_name}.{method_name}":
        raise ValueError("Nested or dynamically rebound methods are not callable through this interface.")
    return selected_class, method


def bounded_result(value, limit):
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    parts = []
    size = 0
    try:
        for chunk in encoder.iterencode(value):
            if size + len(chunk) > limit:
                remaining = max(0, limit - size)
                if remaining:
                    parts.append(chunk[:remaining])
                return {
                    "result": None,
                    "result_truncated": True,
                    "result_preview": "".join(parts),
                    "result_json_chars": f">{limit}",
                }
            parts.append(chunk)
            size += len(chunk)
    except (TypeError, ValueError) as exc:
        return {
            "result": None,
            "result_truncated": False,
            "result_type": type(value).__name__,
            "result_serialization_error": f"{type(exc).__name__}: {exc}",
        }
    encoded = "".join(parts)
    return {
        "result": json.loads(encoded),
        "result_truncated": False,
        "result_json_chars": size,
    }


def main():
    script = Path(sys.argv[1])
    mode = sys.argv[2]
    primary_name = sys.argv[3]
    secondary_name = sys.argv[4]
    request_path = Path(sys.argv[5])
    result_path = Path(sys.argv[6])
    result_limit = int(sys.argv[7])
    stdout = CappedText(int(sys.argv[8]))
    stderr = CappedText(int(sys.argv[9]))
    envelope_limit = int(sys.argv[10])
    exit_code = 0
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if mode == "function":
                positional = request["args"]
                keywords = request["kwargs"]
                function = load_function(script, primary_name)
                try:
                    inspect.signature(function).bind(*positional, **keywords)
                except TypeError as exc:
                    raise TypeError(
                        f"Arguments do not match {primary_name}: {exc}"
                    ) from exc
                value = function(*positional, **keywords)
                identity = {"function_name": primary_name}
            elif mode == "instance_method":
                selected_class, method = load_instance_method(
                    script, primary_name, secondary_name
                )
                init_positional = request["constructor_args"]
                init_keywords = request["constructor_kwargs"]
                call_positional = request["method_args"]
                call_keywords = request["method_kwargs"]
                try:
                    inspect.signature(selected_class).bind(
                        *init_positional, **init_keywords
                    )
                except TypeError as exc:
                    raise TypeError(
                        f"Constructor arguments do not match {primary_name}: {exc}"
                    ) from exc
                instance = selected_class(*init_positional, **init_keywords)
                if type(instance) is not selected_class:
                    raise ValueError(
                        "Selected constructor did not return an exact instance of the "
                        "validated public class."
                    )
                bound_method = method.__get__(instance, selected_class)
                try:
                    inspect.signature(bound_method).bind(
                        *call_positional, **call_keywords
                    )
                except TypeError as exc:
                    raise TypeError(
                        f"Arguments do not match {primary_name}.{secondary_name}: {exc}"
                    ) from exc
                value = bound_method(*call_positional, **call_keywords)
                identity = {
                    "class_name": primary_name,
                    "method_name": secondary_name,
                }
            else:
                raise ValueError("Unsupported callable invocation mode.")
            if inspect.isawaitable(value):
                value = asyncio.run(value)
            envelope = {"status": "success", **identity}
            envelope.update(bounded_result(value, result_limit))
    except BaseException as exc:
        exit_code = 1
        envelope = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "exception_type": type(exc).__name__,
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )[-20000:],
        }
        if mode == "function":
            envelope["function_name"] = primary_name
        elif mode == "instance_method":
            envelope["class_name"] = primary_name
            envelope["method_name"] = secondary_name
    envelope["stdout"] = stdout.value()
    envelope["stderr"] = stderr.value()
    envelope["stdout_truncated"] = stdout.truncated
    envelope["stderr_truncated"] = stderr.truncated
    encoded = json.dumps(
        envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > envelope_limit:
        if envelope.get("result") is not None:
            envelope["result"] = None
            envelope["result_truncated"] = True
            envelope["result_preview"] = "Result JSON exceeded the encoded envelope byte limit."
        for field in ("stdout", "stderr", "traceback", "result_preview"):
            if isinstance(envelope.get(field), str):
                envelope[field] = envelope[field][:20000]
        envelope["envelope_truncated"] = True
        encoded = json.dumps(
            envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    if len(encoded) > envelope_limit:
        fallback = {
            "status": "error",
            "error": "Function result envelope exceeded its encoded byte limit.",
            "stdout": "",
            "stderr": "",
            "stdout_truncated": True,
            "stderr_truncated": True,
        }
        if mode == "function":
            fallback["function_name"] = primary_name
        elif mode == "instance_method":
            fallback["class_name"] = primary_name
            fallback["method_name"] = secondary_name
        encoded = json.dumps(fallback, separators=(",", ":")).encode("utf-8")
    with result_path.open("xb") as stream:
        stream.write(encoded)
    return exit_code


raise SystemExit(main())
'''


PERSISTENT_INSTANCE_RUNNER_SOURCE = r'''
from __future__ import annotations
import asyncio
import contextlib
import importlib.util
import inspect
import io
import json
from pathlib import Path
import re
import sys
import traceback


PUBLIC_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")


class BoundedText(io.TextIOBase):
    def __init__(self, limit):
        self.limit = limit
        self.parts = []
        self.size = 0
        self.truncated = False

    def writable(self):
        return True

    def write(self, value):
        rendered = str(value)
        remaining = max(0, self.limit - self.size)
        if remaining:
            self.parts.append(rendered[:remaining])
            self.size += min(len(rendered), remaining)
        if len(rendered) > remaining:
            self.truncated = True
        return len(rendered)

    def getvalue(self):
        return "".join(self.parts)


def emit(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    sys.__stdout__.write(encoded + "\n")
    sys.__stdout__.flush()


def close_event_loop(loop):
    try:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        loop.run_until_complete(loop.shutdown_asyncgens())
    except BaseException:
        pass
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def main():
    if len(sys.argv) != 9:
        raise SystemExit("trusted persistent instance runner received invalid argv")
    skill_root = Path(sys.argv[1]).resolve(strict=True)
    entrypoint = Path(sys.argv[2]).resolve(strict=True)
    constructor_path = Path(sys.argv[3]).resolve(strict=True)
    invocation_mode = sys.argv[4]
    selected_name = sys.argv[5]
    max_input_bytes = int(sys.argv[6])
    max_result_chars = int(sys.argv[7])
    max_capture_chars = int(sys.argv[8])
    try:
        entrypoint.relative_to(skill_root)
    except ValueError as exc:
        raise SystemExit("entrypoint is outside the exact Skill snapshot") from exc
    if (
        invocation_mode not in {"instance", "factory"}
        or not PUBLIC_NAME.fullmatch(selected_name)
        or selected_name.startswith("_")
    ):
        raise SystemExit("invalid public persistent-object identity")

    import_roots = [entrypoint.parent, skill_root]
    inherited = [item for item in sys.path if item]
    sys.path[:] = []
    for candidate in [*import_roots, *inherited]:
        rendered = str(candidate)
        if rendered not in sys.path:
            sys.path.append(rendered)

    request = json.loads(constructor_path.read_text(encoding="utf-8"))
    constructor_args = request.get("args")
    constructor_kwargs = request.get("kwargs")
    if not isinstance(constructor_args, list) or not isinstance(constructor_kwargs, dict):
        raise SystemExit("invalid constructor envelope")

    captured_out = BoundedText(max_capture_chars)
    captured_err = BoundedText(max_capture_chars)
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    try:
        with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
            module_name = "_chatds_persistent_skill"
            spec = importlib.util.spec_from_file_location(module_name, entrypoint)
            if spec is None or spec.loader is None:
                raise RuntimeError("cannot load exact Skill entrypoint")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            if invocation_mode == "instance":
                selected_class = getattr(module, selected_name, None)
                if (
                    not inspect.isclass(selected_class)
                    or selected_class.__module__ != module_name
                    or selected_class.__qualname__ != selected_name
                ):
                    raise TypeError("Imported or replaced classes are not callable")
                instance = selected_class(*constructor_args, **constructor_kwargs)
            else:
                selected_factory = getattr(module, selected_name, None)
                if (
                    not inspect.isfunction(selected_factory)
                    or selected_factory.__module__ != module_name
                    or selected_factory.__qualname__ != selected_name
                ):
                    raise TypeError("Imported, replaced, or decorated factories are not callable")
                instance = selected_factory(*constructor_args, **constructor_kwargs)
                if inspect.isawaitable(instance):
                    instance = event_loop.run_until_complete(instance)
                instance_type = type(instance)
                if (
                    instance_type.__module__ != module_name
                    or instance_type.__qualname__ != instance_type.__name__
                    or not PUBLIC_NAME.fullmatch(instance_type.__name__)
                ):
                    raise TypeError("Factory must return a public top-level object declared by the entrypoint")
            class_name = type(instance).__name__
    except BaseException as exc:
        emit({
            "event": "constructor_error",
            "invocation_mode": invocation_mode,
            "selected_name": selected_name,
            "error_type": type(exc).__name__,
            "error": str(exc)[:max_result_chars],
            "stdout": captured_out.getvalue(),
            "stderr": captured_err.getvalue(),
            "stdout_truncated": captured_out.truncated,
            "stderr_truncated": captured_err.truncated,
        })
        close_event_loop(event_loop)
        return 1

    emit({
        "event": "ready",
        "class_name": class_name,
        "invocation_mode": invocation_mode,
        **({"factory_name": selected_name} if invocation_mode == "factory" else {}),
        "stdout": captured_out.getvalue(),
        "stderr": captured_err.getvalue(),
        "stdout_truncated": captured_out.truncated,
        "stderr_truncated": captured_err.truncated,
    })
    while True:
        raw = sys.stdin.buffer.readline(max_input_bytes + 1)
        if not raw:
            close_event_loop(event_loop)
            return 0
        if len(raw) > max_input_bytes or not raw.endswith(b"\n"):
            emit({"event": "protocol_error", "error": "call envelope exceeded its bound"})
            close_event_loop(event_loop)
            return 2
        try:
            call = json.loads(raw.decode("utf-8"))
            if not isinstance(call, dict) or set(call) != {"call_id", "method", "args", "kwargs"}:
                raise ValueError("invalid call envelope")
            call_id = call["call_id"]
            method_name = call["method"]
            positional = call["args"]
            keywords = call["kwargs"]
            if (
                not isinstance(call_id, str)
                or not isinstance(method_name, str)
                or method_name.startswith("_")
                or not PUBLIC_NAME.fullmatch(method_name)
                or not isinstance(positional, list)
                or not isinstance(keywords, dict)
            ):
                raise ValueError("invalid public method call")
            selected = getattr(instance, method_name, None)
            function = getattr(selected, "__func__", None)
            if (
                not inspect.ismethod(selected)
                or getattr(selected, "__self__", None) is not instance
                or function is None
                or function.__module__ != "_chatds_persistent_skill"
                or function.__qualname__ != f"{type(instance).__qualname__}.{method_name}"
            ):
                raise TypeError("Imported, replaced, decorated, or unbound methods are not callable")
            captured_out = BoundedText(max_capture_chars)
            captured_err = BoundedText(max_capture_chars)
            with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
                result = selected(*positional, **keywords)
                if inspect.isawaitable(result):
                    result = event_loop.run_until_complete(result)
            envelope = {
                "event": "call_result",
                "call_id": call_id,
                "method": method_name,
                "status": "success",
                "result": result,
                "stdout": captured_out.getvalue(),
                "stderr": captured_err.getvalue(),
                "stdout_truncated": captured_out.truncated,
                "stderr_truncated": captured_err.truncated,
            }
            rendered = json.dumps(
                envelope,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            if len(rendered) > max_result_chars:
                raise ValueError("method result exceeded its JSON bound")
            sys.__stdout__.write(rendered + "\n")
            sys.__stdout__.flush()
        except BaseException as exc:
            emit({
                "event": "call_result",
                "call_id": call.get("call_id") if isinstance(call, dict) else None,
                "method": call.get("method") if isinstance(call, dict) else None,
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:max_result_chars],
            })


raise SystemExit(main())
'''


class ProtocolError(ValueError):
    """A stable validation failure safe to return across the socket."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class _ProcessStreamBuffer:
    """A bounded byte ring with absolute offsets for incremental reads."""

    limit: int
    data: bytearray = field(default_factory=bytearray)
    start_offset: int = 0
    end_offset: int = 0
    truncated: bool = False
    eof: bool = False

    def append(self, content: bytes) -> None:
        if not content:
            return
        self.data.extend(content)
        self.end_offset += len(content)
        overflow = len(self.data) - self.limit
        if overflow > 0:
            del self.data[:overflow]
            self.start_offset += overflow
            self.truncated = True

    def read(self, offset: int, limit: int) -> tuple[bytes, int, bool]:
        data_loss = offset < self.start_offset
        effective_offset = max(offset, self.start_offset)
        index = min(len(self.data), effective_offset - self.start_offset)
        content = bytes(self.data[index:index + limit])
        return content, effective_offset + len(content), data_loss


@dataclass
class _ProcessLease:
    handle: str
    scope_digest: str
    skill_sha256: str
    script_sha256: str
    open_op_id: str
    open_fingerprint: str
    entrypoint_relative: str
    entrypoint_bytes: bytes
    argv: list[str]
    cwd_policy: str
    invocation_mode: str
    class_name: str | None
    factory_name: str | None
    runtime_profile: str
    egress_policy: str
    egress_policy_version: int
    egress_bridge: Any | None
    egress_audit_receipt: dict[str, Any] | None
    interpreter: str
    command: list[str]
    workdir: Path
    environment: dict[str, str]
    temp_dir: Path
    skill_root: Path
    workspace: Path
    runtime_root: Path
    workspace_baseline: dict[str, tuple[int, str]]
    created_at: float
    last_activity: float
    idle_ttl_seconds: int
    max_runtime_seconds: int
    absolute_expires_at: float
    state: str = "open"
    process: subprocess.Popen[bytes] | None = None
    process_group_id: int | None = None
    stdin_bytes_written: int = 0
    stdin_closed: bool = False
    stdout: _ProcessStreamBuffer = field(
        default_factory=lambda: _ProcessStreamBuffer(MAX_PROCESS_STREAM_BYTES)
    )
    stderr: _ProcessStreamBuffer = field(
        default_factory=lambda: _ProcessStreamBuffer(MAX_PROCESS_STREAM_BYTES)
    )
    reader_threads: list[threading.Thread] = field(default_factory=list)
    operations: OrderedDict[str, tuple[str, dict[str, Any]]] = field(
        default_factory=OrderedDict
    )
    operation_cache_bytes: int = 0
    pending_sync_token: str | None = None
    pending_sync_state: dict[str, tuple[int, str]] | None = None
    pending_sync_close: bool = False
    pending_sync_ack_deadline: float | None = None
    pending_sync_prepare_op_id: str | None = None
    closed_at: float | None = None
    close_reason: str | None = None
    pending_expiry_reason: str | None = None
    stopping: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    condition: threading.Condition = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)

    @property
    def idle_expires_at(self) -> float:
        return self.last_activity + self.idle_ttl_seconds


_PROCESS_LEASES: dict[str, _ProcessLease] = {}
_PROCESS_OPEN_OPERATIONS: dict[tuple[str, str], tuple[str, str]] = {}
_PROCESS_LEASES_LOCK = threading.RLock()
_PROCESS_HANDLE_SECRET = secrets.token_bytes(32)
DAEMON_DUMPABILITY_HARDENED = False
_EXECUTION_ADMISSION_LOCK = threading.RLock()
_V1_EXECUTION_LOCK = threading.Lock()
_ACTIVE_PROCESS_LEASE_HANDLE: str | None = None
_ACTIVE_V1_EXECUTION = False
_ACTIVE_V1_EXECUTION_QUARANTINED = False
_ACTIVE_V1_TEMP_DIR: Path | None = None
_ORPHANED_V1_EGRESS_BRIDGES: OrderedDict[str, Any] = OrderedDict()
_V1_EGRESS_BRIDGE_CLEANUP_LOCK = threading.Lock()


def _canonical_snapshot_digest(files: dict[str, bytes]) -> str:
    manifest = [
        {
            "path": path,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for path, content in sorted(files.items())
    ]
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_sha256(value: Any, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProtocolError("invalid_authority", f"{field_name} must be a lowercase SHA-256 digest.")
    return value


def _validated_process_owner_scope(value: Any) -> str:
    """Return a digest of runtime-injected owner identity and its secret nonce.

    The owner fields are never accepted as model-facing process arguments.
    They arrive over the executor-only UDS from trusted Harness runtime state.
    The per-scope authority token makes a copied user/session/run tuple
    insufficient to claim an existing handle.
    """

    expected = {"user_id", "session_id", "root_run_id", "authority_token"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ProtocolError(
            "invalid_owner_scope",
            "owner_scope must be the exact runtime-injected owner identity.",
        )
    normalized: dict[str, str] = {}
    for field_name in ("user_id", "session_id", "root_run_id"):
        item = value.get(field_name)
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ProtocolError(
                "invalid_owner_scope",
                f"owner_scope.{field_name} must be a non-empty bounded string.",
            )
        try:
            encoded = item.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ProtocolError(
                "invalid_owner_scope",
                f"owner_scope.{field_name} must be valid UTF-8.",
            ) from exc
        if (
            len(encoded) > MAX_PROCESS_OWNER_ID_BYTES
            or any(ord(character) < 0x20 for character in item)
        ):
            raise ProtocolError(
                "invalid_owner_scope",
                f"owner_scope.{field_name} is outside the bounded identity policy.",
            )
        normalized[field_name] = item
    authority_token = value.get("authority_token")
    if (
        not isinstance(authority_token, str)
        or not 32 <= len(authority_token) <= 128
        or re.fullmatch(r"[A-Za-z0-9_-]+", authority_token) is None
    ):
        raise ProtocolError(
            "invalid_owner_scope",
            "owner_scope.authority_token must be a runtime-generated opaque token.",
        )
    normalized["authority_token"] = authority_token
    encoded_scope = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded_scope).hexdigest()


def _validated_process_op_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ProtocolError("invalid_op_id", "op_id must be a bounded UUID string.")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ProtocolError("invalid_op_id", "op_id must be a UUID string.") from exc


def _validated_process_seconds(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProtocolError(
            "invalid_process_quota",
            f"{field_name} must be an integer between {minimum} and {maximum}.",
        )
    return value


def _process_operation_fingerprint(payload: dict[str, Any]) -> str:
    canonical = {
        key: value
        for key, value in payload.items()
        if key not in {"request_id", "auth_hmac"}
    }
    try:
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolError(
            "invalid_process_request",
            "Process operation must contain bounded valid JSON.",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _process_request_auth_key() -> bytes:
    token = os.environ.get("EXECUTOR_V2_AUTH_TOKEN", "")
    try:
        encoded = token.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ProtocolError(
            "v2_auth_unavailable",
            "Persistent process protocol authentication is not configured safely.",
        ) from exc
    if len(encoded) < 32 or len(encoded) > 4_096:
        raise ProtocolError(
            "v2_auth_unavailable",
            "Persistent process protocol authentication is not configured safely.",
        )
    return encoded


def _process_request_auth_message(payload: dict[str, Any]) -> bytes:
    canonical = {
        key: value
        for key, value in payload.items()
        if key != "auth_hmac"
    }
    try:
        return json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolError(
            "invalid_process_request",
            "Process request must contain bounded valid JSON.",
        ) from exc


def _validate_process_request_auth(payload: dict[str, Any]) -> None:
    provided = payload.get("auth_hmac")
    if (
        not isinstance(provided, str)
        or len(provided) != 64
        or any(character not in "0123456789abcdef" for character in provided)
    ):
        raise ProtocolError(
            "v2_auth_failed",
            "Persistent process request authentication failed.",
        )
    expected = hmac.new(
        _process_request_auth_key(),
        _process_request_auth_message(payload),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise ProtocolError(
            "v2_auth_failed",
            "Persistent process request authentication failed.",
        )


def _new_process_handle(
    *,
    scope_digest: str,
    skill_sha256: str,
    script_sha256: str,
) -> str:
    nonce = secrets.token_urlsafe(24)
    binding = f"{nonce}:{scope_digest}:{skill_sha256}:{script_sha256}".encode("ascii")
    mac = hmac.new(_PROCESS_HANDLE_SECRET, binding, hashlib.sha256).hexdigest()[:32]
    return f"pl2_{nonce}_{mac}"


def _process_error(
    request_id: str | None,
    operation: str | None,
    code: str,
    message: str,
    *,
    handle: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol_version": PROCESS_PROTOCOL_VERSION,
        "kind": "process_lease_result",
        "operation": operation,
        "request_id": request_id,
        "status": "error",
        "error_code": code,
        "error": message,
        "network": "disabled",
    }
    if handle is not None:
        response["lease_handle"] = handle
    response.update(extra)
    return response


def _bound_process_error(
    request_id: str | None,
    operation: str | None,
    code: str,
    message: str,
    *,
    lease: _ProcessLease,
) -> dict[str, Any]:
    """Return an error bound to an already-authorized lease.

    Persistent v3 operations keep their bridge open until close. Ordinary
    lifecycle errors therefore cannot carry a terminal audit, but they must
    still attest the exact lease and egress policy so the Harness can
    distinguish a typed operation failure from a corrupt/downgraded response.
    Errors observed after expiry/close/quarantine are terminal receipts.
    """

    terminal_state = {
        "expired": "closed",
        "closed": "closed",
        "quarantined": "quarantined",
    }.get(lease.state)
    return _process_error(
        request_id,
        operation,
        code,
        message,
        handle=lease.handle,
        scope_digest=lease.scope_digest,
        skill_sha256=lease.skill_sha256,
        script_sha256=lease.script_sha256,
        state=lease.state,
        artifacts=[],
        runtime_profile=lease.runtime_profile,
        network_policy={
            "direct": "disabled",
            "egress": lease.egress_policy,
        },
        egress_policy_version=lease.egress_policy_version,
        **(
            {
                "egress_audit_receipt": dict(
                    lease.egress_audit_receipt
                ),
            }
            if lease.egress_audit_receipt is not None
            else {}
        ),
        **(
            {"terminal_lease_state": terminal_state}
            if terminal_state is not None
            else {}
        ),
    )


def _process_success(
    request_id: str,
    operation: str,
    lease: _ProcessLease,
    **extra: Any,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol_version": PROCESS_PROTOCOL_VERSION,
        "kind": "process_lease_result",
        "operation": operation,
        "request_id": request_id,
        "status": "success",
        "lease_handle": lease.handle,
        "scope_digest": lease.scope_digest,
        "skill_sha256": lease.skill_sha256,
        "script_sha256": lease.script_sha256,
        "state": lease.state,
        "network": "disabled",
        "runtime_profile": lease.runtime_profile,
        "network_policy": {
            "direct": "disabled",
            "egress": lease.egress_policy,
        },
        "egress_policy_version": lease.egress_policy_version,
    }
    if lease.egress_audit_receipt is not None:
        response["egress_audit_receipt"] = dict(
            lease.egress_audit_receipt
        )
    response.update(extra)
    return response


def _terminal_process_error(
    request_id: str,
    operation: str,
    lease: _ProcessLease,
    code: str,
    message: str,
    *,
    terminal_state: str,
    **extra: Any,
) -> dict[str, Any]:
    """Return an authenticated terminal lease receipt that remains cacheable."""

    if terminal_state not in {"closed", "quarantined"}:
        raise ProtocolError(
            "invalid_lease_state",
            "Terminal process errors require a closed or quarantined state.",
        )
    return _process_error(
        request_id,
        operation,
        code,
        message,
        handle=lease.handle,
        scope_digest=lease.scope_digest,
        skill_sha256=lease.skill_sha256,
        script_sha256=lease.script_sha256,
        state=terminal_state,
        terminal_lease_state=terminal_state,
        artifacts=[],
        artifacts_discarded=True,
        runtime_profile=lease.runtime_profile,
        network_policy={
            "direct": "disabled",
            "egress": lease.egress_policy,
        },
        egress_policy_version=lease.egress_policy_version,
        **(
            {
                "egress_audit_receipt": dict(
                    lease.egress_audit_receipt
                ),
            }
            if lease.egress_audit_receipt is not None
            else {}
        ),
        **extra,
    )


def _trusted_resource_launcher() -> str:
    """Return the fixed, controller-verified rlimit launcher path."""

    try:
        item = TRUSTED_RESOURCE_LAUNCHER.lstat()
        resolved = TRUSTED_RESOURCE_LAUNCHER.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProtocolError(
            "resource_launcher_unavailable",
            "The fixed executor resource-limit launcher is unavailable.",
        ) from exc
    if (
        resolved != TRUSTED_RESOURCE_LAUNCHER
        or not stat.S_ISREG(item.st_mode)
        or item.st_uid != 0
        or item.st_mode & 0o022
        or not os.access(TRUSTED_RESOURCE_LAUNCHER, os.X_OK)
    ):
        raise ProtocolError(
            "resource_launcher_unavailable",
            "The fixed executor resource-limit launcher failed its trust policy.",
        )
    return str(TRUSTED_RESOURCE_LAUNCHER)


def _native_worker_popen_kwargs() -> dict[str, Any]:
    """Use subprocess's native credential/session setup, never preexec_fn."""

    options: dict[str, Any] = {"start_new_session": True}
    if (
        os.environ.get("EXECUTOR_WORKER_UID") is None
        and os.environ.get("EXECUTOR_WORKER_GID") is None
    ):
        return options
    worker_uid, worker_gid = _configured_worker_identity(require_explicit=True)
    if os.geteuid() != 0:
        raise ProtocolError(
            "controller_identity_unavailable",
            "Configured worker identity requires a root executor controller.",
        )
    options.update(
        user=worker_uid,
        group=worker_gid,
        extra_groups=[],
    )
    return options


def _resource_limited_command(
    command: list[str],
    *,
    cpu_seconds: int,
    persistent: bool,
) -> list[str]:
    """Wrap one fixed command with native prlimit before dropping identity."""

    universal_runtime = (
        _configured_runtime_profile()
        == SESSION_SANDBOX_RUNTIME_PROFILE
    )
    if persistent or universal_runtime:
        configured_cpu = _trusted_process_limit(
            "EXECUTOR_PROCESS_MAX_CPU_SECONDS",
            default=900,
            minimum=30,
            maximum=MAX_PROCESS_RUNTIME_SECONDS,
        )
        cpu = max(1, min(int(cpu_seconds), configured_cpu))
        max_processes = _trusted_process_limit(
            "EXECUTOR_PROCESS_MAX_NPROC",
            default=64,
            minimum=16,
            # Browser automation legitimately needs hundreds of threads, but
            # only the trusted profile configuration may raise this value.
            # The base lane keeps the 64-process default and each container's
            # cgroup pids_limit remains the independent hard ceiling.
            maximum=(
                MAX_SESSION_SANDBOX_NPROC
                if universal_runtime
                else 512
            ),
        )
        max_files = _trusted_process_limit(
            "EXECUTOR_PROCESS_MAX_NOFILE",
            default=128,
            minimum=64,
            maximum=1_024,
        )
    else:
        cpu = max(1, min(int(cpu_seconds), MAX_SKILL_TIMEOUT + 5))
        max_processes = 32
        max_files = 64
    if MAX_ADDRESS_SPACE_BYTES is None:
        if not (
            universal_runtime
            or (
                persistent
                and _configured_runtime_profile()
                == "browser-automation-v1"
            )
        ):
            raise ProtocolError(
                "invalid_resource_limit",
                "Unlimited sparse address space is restricted to persistent "
                "browser-profile processes.",
            )
        address_space_limit = "unlimited"
    else:
        address_space_limit = str(MAX_ADDRESS_SPACE_BYTES)
    return [
        _trusted_resource_launcher(),
        f"--cpu={cpu}:{cpu}",
        f"--as={address_space_limit}:{address_space_limit}",
        f"--fsize={MAX_OUTPUT_FILE_BYTES}:{MAX_OUTPUT_FILE_BYTES}",
        f"--nofile={max_files}:{max_files}",
        f"--nproc={max_processes}:{max_processes}",
        "--core=0:0",
        "--",
        *command,
    ]


def _trusted_process_limit(
    environment_name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.environ.get(environment_name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _configured_worker_identity(*, require_explicit: bool = False) -> tuple[int, int]:
    raw_uid = os.environ.get("EXECUTOR_WORKER_UID")
    raw_gid = os.environ.get("EXECUTOR_WORKER_GID")
    if (raw_uid is None) != (raw_gid is None):
        raise ProtocolError(
            "worker_identity_unavailable",
            "Executor worker UID and GID must be configured together.",
        )
    if raw_uid is None:
        if require_explicit:
            raise ProtocolError(
                "worker_identity_unavailable",
                "Persistent process protocol requires explicit worker UID/GID isolation.",
            )
        return os.geteuid(), os.getegid()
    try:
        worker_uid = int(raw_uid)
        worker_gid = int(raw_gid)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            "worker_identity_unavailable",
            "Executor worker UID/GID configuration is invalid.",
        ) from exc
    if not (1 <= worker_uid <= 2**31 - 1 and 1 <= worker_gid <= 2**31 - 1):
        raise ProtocolError(
            "worker_identity_unavailable",
            "Executor worker UID/GID is outside the supported numeric policy.",
        )
    return worker_uid, worker_gid


def _process_protocol_enabled() -> bool:
    raw = os.environ.get("EXECUTOR_ALLOWED_REQUEST_KINDS", "")
    if not raw.strip():
        return False
    return "process_lease" in {
        item.strip()
        for item in raw.split(",")
        if item.strip()
    }


def _untrusted_execution_enabled() -> bool:
    raw = os.environ.get("EXECUTOR_ALLOWED_REQUEST_KINDS", "")
    if not raw.strip():
        return True
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    return bool(allowed.intersection({
        "legacy_code",
        "session_code",
        "skill_script",
        "declared_command",
        "process_lease",
    }))


def _validate_worker_controller_security(*, require_root: bool) -> tuple[int, int]:
    _trusted_resource_launcher()
    worker_uid, worker_gid = _configured_worker_identity(require_explicit=True)
    controller_uid = os.geteuid()
    controller_gid = os.getegid()
    if controller_uid == worker_uid or controller_gid == worker_gid:
        raise ProtocolError(
            "worker_identity_unavailable",
            "Executor controller and untrusted worker must use distinct UID/GID identities.",
        )
    if require_root and controller_uid != 0:
        raise ProtocolError(
            "controller_identity_unavailable",
            "Enabled persistent process protocol requires a root controller that drops child identity.",
        )
    if require_root:
        _validate_controller_capabilities()
        if os.environ.get(
            "EXECUTOR_ENFORCE_SHARED_STATE_ISOLATION"
        ) != "1":
            raise ProtocolError(
                "worker_shared_state_unavailable",
                "Executor shared-state isolation is not enabled.",
            )
    return worker_uid, worker_gid


def _validate_process_controller_security(*, require_root: bool = False) -> tuple[int, int]:
    _process_request_auth_key()
    return _validate_worker_controller_security(require_root=require_root)


def _validate_controller_capabilities() -> None:
    """Fail closed when Linux stripped capabilities needed for containment."""

    if not sys.platform.startswith("linux"):
        return
    try:
        status = Path("/proc/self/status").read_text(
            encoding="utf-8",
            errors="strict",
        )
        raw = next(
            line.split(":", 1)[1].strip()
            for line in status.splitlines()
            if line.startswith("CapEff:")
        )
        effective = int(raw, 16)
    except (OSError, UnicodeError, StopIteration, ValueError) as exc:
        raise ProtocolError(
            "controller_capability_unavailable",
            "Executor cannot attest its effective controller capabilities.",
        ) from exc
    required = {
        0: "CHOWN",
        1: "DAC_OVERRIDE",
        3: "FOWNER",
        5: "KILL",
        6: "SETGID",
        7: "SETUID",
    }
    missing = [name for bit, name in required.items() if not effective & (1 << bit)]
    if missing:
        raise ProtocolError(
            "controller_capability_unavailable",
            "Executor controller is missing required containment capabilities.",
        )


def _worker_uid_processes(worker_uid: int) -> set[int]:
    matches: set[int] = set()
    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
    except OSError as exc:
        raise ProtocolError(
            "worker_containment_failed",
            "Executor cannot inspect its PID namespace for worker cleanup.",
        ) from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            if entry.exists():
                raise ProtocolError(
                    "worker_containment_failed",
                    "Executor could not inspect a live PID during worker cleanup.",
                ) from exc
            continue
        uid_line = next(
            (line for line in status.splitlines() if line.startswith("Uid:")),
            None,
        )
        if uid_line is None:
            if entry.exists():
                raise ProtocolError(
                    "worker_containment_failed",
                    "A live PID did not expose a valid UID record during cleanup.",
                )
            continue
        try:
            identities = [int(value) for value in uid_line.split()[1:5]]
        except (TypeError, ValueError) as exc:
            if entry.exists():
                raise ProtocolError(
                    "worker_containment_failed",
                    "A live PID exposed a malformed UID record during cleanup.",
                ) from exc
            continue
        if len(identities) != 4:
            raise ProtocolError(
                "worker_containment_failed",
                "A live PID exposed an incomplete UID record during cleanup.",
            )
        if worker_uid in identities:
            matches.add(pid)
    return matches


def _reap_worker_children() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except (ChildProcessError, OSError):
            return
        if pid <= 0:
            return


def _sweep_configured_worker_uid(*, timeout_seconds: float = 2.0) -> None:
    """Quiesce and kill the dedicated UID, requiring two empty rescans.

    SIGSTOP closes the fork race before SIGKILL.  Requiring two consecutive
    empty scans prevents a just-forked/double-forked descendant from becoming
    the next execution's same-UID peer between cleanup and slot release.
    """

    if os.environ.get("EXECUTOR_WORKER_UID") is None:
        return
    worker_uid, _ = _configured_worker_identity(require_explicit=True)
    deadline = time.monotonic() + max(0.1, min(timeout_seconds, 5.0))
    consecutive_empty_scans = 0
    while True:
        matches = _worker_uid_processes(worker_uid)
        if not matches:
            _reap_worker_children()
            consecutive_empty_scans += 1
            if consecutive_empty_scans >= 2:
                return
            if time.monotonic() >= deadline:
                raise ProtocolError(
                    "worker_containment_failed",
                    "Executor could not confirm two empty worker-identity scans.",
                )
            time.sleep(0.02)
            continue
        consecutive_empty_scans = 0
        stopped: set[int] = set()
        while True:
            new_matches = _worker_uid_processes(worker_uid) - stopped
            if not new_matches:
                break
            for pid in new_matches:
                try:
                    os.kill(pid, signal.SIGSTOP)
                except ProcessLookupError:
                    pass
                except (PermissionError, OSError) as exc:
                    raise ProtocolError(
                        "worker_containment_failed",
                        "Executor controller could not stop a worker-identity process.",
                    ) from exc
            stopped.update(new_matches)
        # Include processes that exited between scans; ProcessLookupError is
        # harmless. Every still-live UID peer is now stopped and cannot fork.
        stopped.update(_worker_uid_processes(worker_uid))
        for pid in stopped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except (PermissionError, OSError) as exc:
                raise ProtocolError(
                    "worker_containment_failed",
                    "Executor controller could not kill a worker-identity process.",
                ) from exc
        _reap_worker_children()
        if time.monotonic() >= deadline:
            remaining = _worker_uid_processes(worker_uid)
            if remaining:
                raise ProtocolError(
                    "worker_containment_failed",
                    "Worker-identity processes survived bounded executor cleanup.",
                )
            _reap_worker_children()
            time.sleep(0.02)
            if _worker_uid_processes(worker_uid):
                raise ProtocolError(
                    "worker_containment_failed",
                    "A worker-identity process appeared during final cleanup confirmation.",
                )
            return
        time.sleep(0.02)


def _configured_execution_temp_root() -> Path:
    raw = os.environ.get("EXECUTOR_EXECUTION_TEMP_ROOT", "/tmp").strip()
    if not raw or "\x00" in raw:
        raise ProtocolError(
            "worker_shared_state_unavailable",
            "Executor execution temp root is invalid.",
        )
    root = Path(raw)
    if not root.is_absolute():
        raise ProtocolError(
            "worker_shared_state_unavailable",
            "Executor execution temp root must be absolute.",
        )
    try:
        root_stat = root.lstat()
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProtocolError(
            "worker_shared_state_unavailable",
            "Executor execution temp root is unavailable.",
        ) from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or resolved != root
    ):
        raise ProtocolError(
            "worker_shared_state_unavailable",
            "Executor execution temp root is not a canonical directory.",
        )
    return root


def _make_execution_temp_dir(prefix: str) -> Path:
    global _ACTIVE_V1_TEMP_DIR
    path = Path(
        tempfile.mkdtemp(
            prefix=prefix,
            dir=str(_configured_execution_temp_root()),
        )
    )
    with _EXECUTION_ADMISSION_LOCK:
        if _ACTIVE_V1_EXECUTION:
            if _ACTIVE_V1_TEMP_DIR is not None:
                _remove_process_tree(path)
                raise ProtocolError(
                    "worker_containment_failed",
                    "A one-shot execution already owns a controller snapshot.",
                )
            _ACTIVE_V1_TEMP_DIR = path
    return path


def _teardown_one_shot_temp_dir(path: Path) -> None:
    """Remove one controller-owned snapshot and prove it no longer exists."""

    global _ACTIVE_V1_EXECUTION_QUARANTINED, _ACTIVE_V1_TEMP_DIR
    if not _remove_process_tree(path):
        with _EXECUTION_ADMISSION_LOCK:
            if _ACTIVE_V1_EXECUTION and _ACTIVE_V1_TEMP_DIR == path:
                _ACTIVE_V1_EXECUTION_QUARANTINED = True
        raise ProtocolError(
            "worker_containment_failed",
            "One-shot worker tree could not be removed safely.",
        )
    with _EXECUTION_ADMISSION_LOCK:
        if _ACTIVE_V1_TEMP_DIR == path:
            _ACTIVE_V1_TEMP_DIR = None


def _worker_shared_state_roots() -> tuple[Path, ...]:
    roots = [
        _configured_execution_temp_root(),
        Path("/tmp"),
        Path("/dev/shm"),
    ]
    if _configured_runtime_profile() in {
        "browser-automation-v1",
        SESSION_SANDBOX_RUNTIME_PROFILE,
    }:
        roots.append(Path("/workspace"))
    return tuple(dict.fromkeys(roots))


def _validate_worker_shared_state_roots(
    *,
    worker_uid: int,
    worker_gid: int,
) -> None:
    """Prove fixed shared roots cannot be mutated by the worker identity."""

    for root in _worker_shared_state_roots():
        try:
            root_stat = root.lstat()
        except OSError as exc:
            raise ProtocolError(
                "worker_shared_state_unavailable",
                "Executor shared-state root is unavailable.",
            ) from exc
        if (
            stat.S_ISLNK(root_stat.st_mode)
            or not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid == worker_uid
            or root_stat.st_mode & 0o002
            or (
                root_stat.st_gid == worker_gid
                and root_stat.st_mode & 0o020
            )
        ):
            raise ProtocolError(
                "worker_shared_state_unavailable",
                "Executor shared-state root is writable by the fixed worker.",
            )


def _worker_owned_shared_entries(
    *,
    worker_uid: int,
    worker_gid: int,
) -> list[Path]:
    """Boundedly audit fixed container paths shared by consecutive leases."""

    _validate_worker_shared_state_roots(
        worker_uid=worker_uid,
        worker_gid=worker_gid,
    )
    owned: list[Path] = []
    inspected = 0
    for root in _worker_shared_state_roots():
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                remaining_entries = MAX_OUTPUT_ENTRIES - inspected
                children: list[os.DirEntry[str]] = []
                with os.scandir(directory) as scanner:
                    for child in scanner:
                        children.append(child)
                        if len(children) > remaining_entries:
                            raise ProtocolError(
                                "worker_shared_state_unavailable",
                                "Executor shared-state audit exceeded its entry bound.",
                            )
            except OSError as exc:
                raise ProtocolError(
                    "worker_shared_state_unavailable",
                    "Executor cannot inspect a shared-state root.",
                ) from exc
            for child in children:
                inspected += 1
                if inspected > MAX_OUTPUT_ENTRIES:
                    raise ProtocolError(
                        "worker_shared_state_unavailable",
                        "Executor shared-state audit exceeded its entry bound.",
                    )
                path = Path(child.path)
                try:
                    item = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ProtocolError(
                        "worker_shared_state_unavailable",
                        "Executor cannot inspect a shared-state entry.",
                    ) from exc
                if item.st_uid == worker_uid:
                    owned.append(path)
                    # Removing the top worker-owned directory also removes its
                    # bounded descendants; do not collect duplicate children.
                    if stat.S_ISDIR(item.st_mode):
                        continue
                elif (
                    stat.S_ISLNK(item.st_mode)
                    or item.st_mode & 0o002
                    or (
                        item.st_gid == worker_gid
                        and item.st_mode & 0o020
                    )
                ):
                    # A fixed worker can mutate a regular file owned by
                    # another identity just as easily as a directory.  A
                    # persistent symlink can also redirect a later lease into
                    # state outside the audited root, so neither form is an
                    # acceptable clean boundary.
                    raise ProtocolError(
                        "worker_shared_state_unavailable",
                        "A shared-state entry is writable or redirectable by the fixed worker.",
                    )
                if stat.S_ISDIR(item.st_mode):
                    stack.append(path)
    return owned


def _purge_configured_worker_shared_state() -> None:
    """Remove fixed-UID residue, then prove all shared roots are clean."""

    if (
        os.environ.get("EXECUTOR_WORKER_UID") is None
        or os.environ.get(
            "EXECUTOR_ENFORCE_SHARED_STATE_ISOLATION"
        ) != "1"
    ):
        return
    worker_uid, worker_gid = _configured_worker_identity(
        require_explicit=True
    )
    for _ in range(2):
        owned = _worker_owned_shared_entries(
            worker_uid=worker_uid,
            worker_gid=worker_gid,
        )
        if not owned:
            # Two clean scans close the same create/delete observation window
            # as process cleanup. No worker process is live at this boundary.
            continue
        for path in sorted(owned, key=lambda item: len(item.parts), reverse=True):
            try:
                item = path.lstat()
                if item.st_uid != worker_uid:
                    raise ProtocolError(
                        "worker_containment_failed",
                        "Shared-state ownership changed during cleanup.",
                    )
                if stat.S_ISDIR(item.st_mode):
                    if not _remove_process_tree(path):
                        raise ProtocolError(
                            "worker_containment_failed",
                            "Worker-owned shared directory could not be removed.",
                        )
                else:
                    path.unlink()
            except FileNotFoundError:
                continue
            except ProtocolError:
                raise
            except OSError as exc:
                raise ProtocolError(
                    "worker_containment_failed",
                    "Worker-owned shared state could not be removed.",
                ) from exc
    if _worker_owned_shared_entries(
        worker_uid=worker_uid,
        worker_gid=worker_gid,
    ):
        raise ProtocolError(
            "worker_containment_failed",
            "Worker-owned shared state survived bounded cleanup.",
        )


def _reserve_process_admission(reservation: str) -> None:
    global _ACTIVE_PROCESS_LEASE_HANDLE
    with _EXECUTION_ADMISSION_LOCK:
        if _ACTIVE_V1_EXECUTION or _ACTIVE_PROCESS_LEASE_HANDLE is not None:
            raise ProtocolError(
                "worker_busy",
                "The dedicated worker identity is already assigned to another execution.",
            )
        _ACTIVE_PROCESS_LEASE_HANDLE = reservation


def _commit_process_admission(reservation: str, handle: str) -> None:
    global _ACTIVE_PROCESS_LEASE_HANDLE
    with _EXECUTION_ADMISSION_LOCK:
        if _ACTIVE_PROCESS_LEASE_HANDLE != reservation:
            raise ProtocolError(
                "worker_admission_lost",
                "The dedicated worker admission reservation was lost.",
            )
        _ACTIVE_PROCESS_LEASE_HANDLE = handle


def _release_process_admission(handle: str) -> None:
    global _ACTIVE_PROCESS_LEASE_HANDLE
    # Sweep before releasing the unique UID so an escaped/session-detached
    # process can never overlap the next execution.
    _sweep_configured_worker_uid()
    _purge_configured_worker_shared_state()
    with _EXECUTION_ADMISSION_LOCK:
        if _ACTIVE_PROCESS_LEASE_HANDLE in {handle, f"opening:{handle}"}:
            _ACTIVE_PROCESS_LEASE_HANDLE = None


def _cancel_process_admission(reservation: str) -> None:
    """Release an open reservation that never started a worker process."""

    global _ACTIVE_PROCESS_LEASE_HANDLE
    with _EXECUTION_ADMISSION_LOCK:
        if _ACTIVE_PROCESS_LEASE_HANDLE == reservation:
            _ACTIVE_PROCESS_LEASE_HANDLE = None


def _requested_v1_egress_policy(payload: dict[str, Any]) -> str:
    """Project a bounded client request into its receipt-only policy label."""

    rules = payload.get("egress_rules", [])
    return (
        "origin_allowlist_proxy"
        if isinstance(rules, list) and bool(rules)
        else "none"
    )


def _skill_invocation_receipt_fields(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Preserve client-built invocation identity when admission fails early."""

    raw = payload.get("invocation")
    if raw is None:
        return {"invocation_mode": "cli"}
    if not isinstance(raw, dict):
        return {"invocation_mode": "cli"}
    mode = raw.get("mode")
    if mode == "function":
        name = raw.get("name")
        return {
            "invocation_mode": "function",
            **({"function_name": name} if isinstance(name, str) else {}),
        }
    if mode == "instance_method":
        class_name = raw.get("class_name")
        method_name = raw.get("method_name")
        return {
            "invocation_mode": "instance_method",
            **(
                {"class_name": class_name}
                if isinstance(class_name, str)
                else {}
            ),
            **(
                {"method_name": method_name}
                if isinstance(method_name, str)
                else {}
            ),
        }
    return {"invocation_mode": "cli"}


def _declared_command_request_sha256(
    payload: dict[str, Any],
) -> str | None:
    """Reproduce the no-shell command identity before worker admission."""

    executable = payload.get("executable")
    argv = payload.get("argv", [])
    if (
        not isinstance(executable, str)
        or not isinstance(argv, list)
        or any(not isinstance(item, str) for item in argv)
    ):
        return None
    try:
        encoded = json.dumps(
            [executable, *argv],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _v1_execution_error(
    payload: dict[str, Any],
    code: str,
    message: str,
) -> dict[str, Any]:
    request_id = (
        payload.get("request_id")
        if isinstance(payload.get("request_id"), str)
        else None
    )
    kind = payload.get("kind")
    if kind == "skill_script":
        return _skill_error(
            request_id,
            code,
            message,
            egress_policy=_requested_v1_egress_policy(payload),
            **_skill_invocation_receipt_fields(payload),
        )
    if kind == "declared_command":
        return _declared_command_error(
            request_id,
            code,
            message,
            executable=(
                payload.get("executable")
                if isinstance(payload.get("executable"), str)
                else None
            ),
            cwd=payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
            command_sha256=_declared_command_request_sha256(payload),
            egress_policy=_requested_v1_egress_policy(payload),
        )
    if kind == "session_code":
        return _session_code_error(request_id, code, message)
    return {
        "status": "error",
        "error_code": code,
        "error": message,
        "network": "disabled",
    }


def _run_v1_serialized(
    payload: dict[str, Any],
    runner: Any,
) -> dict[str, Any]:
    """Admit at most one one-shot child to the dedicated UID slot.

    Waiting on this lock is unsafe: the caller can time out while its server
    handler remains queued, after which the handler could execute side effects
    without a live request fence.  A contending request therefore fails closed
    immediately and must be retried explicitly by the caller.
    """

    global _ACTIVE_V1_EXECUTION, _ACTIVE_V1_EXECUTION_QUARANTINED
    global _ACTIVE_V1_TEMP_DIR
    if not _V1_EXECUTION_LOCK.acquire(blocking=False):
        return _v1_execution_error(
            payload,
            "worker_busy",
            "The dedicated worker identity is already assigned to another execution.",
        )
    try:
        with _EXECUTION_ADMISSION_LOCK:
            if _ACTIVE_V1_EXECUTION or _ACTIVE_PROCESS_LEASE_HANDLE is not None:
                return _v1_execution_error(
                    payload,
                    "worker_busy",
                    "The dedicated worker identity is already assigned to another execution.",
                )
            _ACTIVE_V1_EXECUTION = True
            _ACTIVE_V1_EXECUTION_QUARANTINED = False
            _ACTIVE_V1_TEMP_DIR = None

        result: dict[str, Any] | None = None
        execution_error: BaseException | None = None
        containment_error: ProtocolError | None = None
        try:
            result = runner(payload)
        except BaseException as exc:
            execution_error = exc
        finally:
            if (
                isinstance(execution_error, ProtocolError)
                and execution_error.code == "worker_containment_failed"
            ):
                containment_error = execution_error
            try:
                _sweep_configured_worker_uid()
                if containment_error is None:
                    with _EXECUTION_ADMISSION_LOCK:
                        active_temp_dir = _ACTIVE_V1_TEMP_DIR
                    if active_temp_dir is not None:
                        raise ProtocolError(
                            "worker_containment_failed",
                            "One-shot worker tree survived execution teardown.",
                        )
                    _purge_configured_worker_shared_state()
            except ProtocolError as exc:
                containment_error = exc
            with _EXECUTION_ADMISSION_LOCK:
                orphaned_bridge_count = len(
                    _ORPHANED_V1_EGRESS_BRIDGES
                )
                if (
                    containment_error is None
                    and orphaned_bridge_count == 0
                ):
                    _ACTIVE_V1_EXECUTION = False
                    _ACTIVE_V1_EXECUTION_QUARANTINED = False
                    _ACTIVE_V1_TEMP_DIR = None
                else:
                    # A typed runner result may already preserve post-spawn
                    # returncode/output evidence. Quarantine admission without
                    # replacing that result with a pre-spawn-shaped error.
                    _ACTIVE_V1_EXECUTION = True
                    _ACTIVE_V1_EXECUTION_QUARANTINED = True
        if containment_error is not None:
            return _v1_execution_error(
                payload,
                containment_error.code,
                str(containment_error),
            )
        if execution_error is not None:
            raise execution_error
        if result is None:
            raise RuntimeError("One-shot executor returned no result.")
        return result
    finally:
        _V1_EXECUTION_LOCK.release()


def _prepare_worker_tree(
    temp_root: Path,
    *,
    immutable_roots: tuple[Path, ...] = (),
    writable_roots: tuple[Path, ...] = (),
    root_group_writable: bool = False,
) -> None:
    """Assign only child-visible trees to the fixed worker identity."""

    if os.environ.get("EXECUTOR_WORKER_UID") is None:
        return
    worker_uid, worker_gid = _configured_worker_identity(require_explicit=True)
    controller_uid = os.geteuid()
    os.chown(temp_root, controller_uid, worker_gid, follow_symlinks=False)
    os.chmod(temp_root, 0o730 if root_group_writable else 0o710)

    def prepare(root: Path, *, immutable: bool) -> None:
        for directory, _, files in os.walk(root, topdown=True, followlinks=False):
            directory_path = Path(directory)
            os.chown(
                directory_path,
                controller_uid if immutable else worker_uid,
                worker_gid,
                follow_symlinks=False,
            )
            os.chmod(directory_path, 0o550 if immutable else 0o700)
            for filename in files:
                path = directory_path / filename
                item = path.lstat()
                if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
                    raise ProtocolError(
                        "unsafe_execution_tree",
                        "Executor ownership preparation encountered a non-regular file.",
                    )
                os.chown(
                    path,
                    controller_uid if immutable else worker_uid,
                    worker_gid,
                    follow_symlinks=False,
                )
                os.chmod(
                    path,
                    (
                        _immutable_snapshot_file_mode(
                            path,
                            group_only=True,
                        )
                        if immutable
                        else 0o600
                    ),
                )

    for root in immutable_roots:
        prepare(root, immutable=True)
    for root in writable_roots:
        prepare(root, immutable=False)


def _configured_runtime_profile() -> str:
    profile = os.environ.get("EXECUTOR_RUNTIME_PROFILE", "base-v1")
    if profile not in {
        "base-v1",
        "browser-automation-v1",
        SESSION_SANDBOX_RUNTIME_PROFILE,
    }:
        raise ProtocolError(
            "runtime_profile_unavailable",
            "Executor runtime profile is not a supported fixed profile.",
        )
    return profile


def _runtime_build_component_record(
    name: str,
    path: Path,
) -> dict[str, Any]:
    """Content-address one fixed image component without trusting path metadata."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except FileNotFoundError:
        # Source-tree tests intentionally lack most image-only paths.  Their
        # absence is still part of the canonical identity instead of being
        # replaced by a random value or silently omitted.
        return {"component": name, "status": "missing"}
    except OSError:
        return {"component": name, "status": "unavailable"}
    try:
        item = os.fstat(descriptor)
        if not stat.S_ISREG(item.st_mode):
            return {"component": name, "status": "not_regular"}
        digest = hashlib.sha256()
        size_bytes = 0
        while True:
            chunk = os.read(descriptor, 256 * 1024)
            if not chunk:
                break
            size_bytes += len(chunk)
            digest.update(chunk)
    except OSError:
        return {"component": name, "status": "unavailable"}
    finally:
        os.close(descriptor)
    return {
        "component": name,
        "status": "present",
        "size_bytes": size_bytes,
        "sha256": digest.hexdigest(),
    }


def _runtime_build_sha256() -> str:
    """Hash the immutable executor/browser control plane as one build identity."""

    records = [
        _runtime_build_component_record(name, path)
        for name, path in sorted(RUNTIME_BUILD_COMPONENTS)
    ]
    encoded = json.dumps(
        {
            "schema": RUNTIME_BUILD_IDENTITY_SCHEMA,
            "components": records,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_runtime_environment_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or "\x00" in value:
        return None
    try:
        if len(value.encode("utf-8", errors="strict")) > 4_096:
            return None
    except UnicodeError:
        return None
    return value


def _profile_process_environment() -> tuple[str, str, dict[str, str]]:
    profile = _configured_runtime_profile()
    if profile not in {
        "browser-automation-v1",
        SESSION_SANDBOX_RUNTIME_PROFILE,
    }:
        return profile, "none", {}
    forwarded: dict[str, str] = {}
    for name in (
        "NODE_PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "BROWSER_EXECUTABLE",
        "CHROME_BIN",
    ):
        value = _bounded_runtime_environment_value(name)
        if value is not None:
            forwarded[name] = value
    for name in (
        "SE_OFFLINE",
        "SE_AVOID_STATS",
        "SE_AVOID_BROWSER_DOWNLOAD",
    ):
        value = _bounded_runtime_environment_value(name)
        if value is not None:
            forwarded[name] = value
    return profile, "none", forwarded


def _canonical_egress_origin(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8_192:
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress contains an invalid origin.",
        )
    try:
        parsed = urlsplit(value)
        parsed_host = parsed.hostname
        parsed_port = parsed.port
        port = (
            parsed_port
            if parsed_port is not None
            else (443 if parsed.scheme.casefold() == "https" else 80)
        )
    except ValueError as exc:
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress contains an invalid origin.",
        ) from exc
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed_host
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65_535
    ):
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress contains an invalid origin.",
        )
    raw_host = parsed_host.rstrip(".").casefold()
    if (
        not raw_host
        or "%" in raw_host
        or any(char in raw_host for char in "*?[]")
        or any(
            ord(char) < 0x20 or ord(char) == 0x7F
            for char in raw_host
        )
    ):
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress contains an invalid host.",
        )
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        try:
            host = raw_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ProtocolError(
                "invalid_egress_policy",
                "Session sandbox egress contains an invalid host.",
            ) from exc
        if (
            len(host) > 253
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or any(
                    char
                    not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                    for char in label
                )
                for label in host.split(".")
            )
        ):
            raise ProtocolError(
                "invalid_egress_policy",
                "Session sandbox egress contains an invalid host.",
            )
    else:
        host = (
            f"[{address.compressed}]"
            if address.version == 6
            else address.compressed
        )
    canonical = f"{scheme}://{host}:{port}"
    if canonical != value:
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress origins must be canonical.",
        )
    return canonical


def _validated_egress_origins(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_EGRESS_ORIGINS:
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress exceeds its bounded origin policy.",
        )
    result = tuple(_canonical_egress_origin(item) for item in value)
    if len(set(result)) != len(result):
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress origins must be unique.",
        )
    return result


_INVALID_EGRESS_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-F]{2})")
_INVALID_EGRESS_ENCODED_PATH = re.compile(
    r"%(?:2e|2f|5c|25|23|3f|0[0-9a-f]|1[0-9a-f]|7f)",
    re.IGNORECASE,
)


def _canonical_egress_url_prefix(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 8_192
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            for character in value
        )
    ):
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress contains an invalid URL prefix.",
        )
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed_port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress contains an invalid URL prefix.",
        ) from exc
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress contains an invalid URL prefix.",
        )
    port = (
        parsed_port
        if parsed_port is not None
        else (443 if scheme == "https" else 80)
    )
    rendered_host = (
        f"[{hostname}]"
        if ":" in hostname and not hostname.startswith("[")
        else hostname
    )
    origin = _canonical_egress_origin(
        f"{scheme}://{rendered_host}:{port}"
    )
    path = parsed.path or "/"
    if (
        not path.startswith("/")
        or "\\" in path
        or "//" in path
        or "{" in path
        or "}" in path
        or _INVALID_EGRESS_PERCENT_ESCAPE.search(path)
        or _INVALID_EGRESS_ENCODED_PATH.search(path)
        or any(
            re.fullmatch(r"\.{1,2}(?:;.*)?", component) is not None
            for component in path.split("/")
        )
    ):
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress contains an invalid URL prefix.",
        )
    query = parsed.query
    if (
        "{" in query
        or "}" in query
        or ";" in query
        or _INVALID_EGRESS_PERCENT_ESCAPE.search(query)
        or re.search(
            r"%(?:25|23|0[0-9A-F]|1[0-9A-F]|7F)",
            query,
            re.IGNORECASE,
        )
    ):
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress contains an invalid URL prefix.",
        )
    canonical = urlunsplit((
        scheme,
        urlsplit(origin).netloc,
        path,
        query,
        "",
    ))
    if canonical != value:
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress URL prefixes must be canonical.",
        )
    return canonical


def _validated_exact_egress_policy(
    payload: dict[str, Any],
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    raw_rules = payload.get("egress_rules", [])
    if (
        not isinstance(raw_rules, list)
        or len(raw_rules) > MAX_EGRESS_RULES
    ):
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox egress exceeds its bounded exact-rule policy.",
        )
    rules: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for rule in raw_rules:
        if (
            not isinstance(rule, dict)
            or set(rule) != {"methods", "url_prefix"}
            or not isinstance(rule.get("methods"), list)
        ):
            raise ProtocolError(
                "invalid_egress_policy",
                "Session sandbox egress contains a malformed exact rule.",
            )
        prefix = _canonical_egress_url_prefix(rule.get("url_prefix"))
        methods = rule["methods"]
        if (
            not methods
            or len(methods) > len(EGRESS_METHOD_ORDER)
            or any(method not in EGRESS_METHODS for method in methods)
            or len(set(methods)) != len(methods)
        ):
            raise ProtocolError(
                "invalid_egress_policy",
                "Session sandbox egress contains an invalid method set.",
            )
        canonical_methods = tuple(
            method for method in EGRESS_METHOD_ORDER if method in set(methods)
        )
        coordinate = (prefix, canonical_methods)
        if list(canonical_methods) != methods or coordinate in seen:
            raise ProtocolError(
                "invalid_egress_policy",
                "Session sandbox egress rules must be canonical and unique.",
            )
        seen.add(coordinate)
        rules.append({
            "methods": list(canonical_methods),
            "url_prefix": prefix,
        })
    if raw_rules and payload.get("egress_policy_version") not in {2, 3}:
        raise ProtocolError(
            "invalid_egress_policy",
            "Exact sandbox egress requires policy version 2 or 3.",
        )
    if not raw_rules and payload.get("egress_policy_version") not in {
        None, 2,
    }:
        raise ProtocolError(
            "invalid_egress_policy",
            "Unsupported sandbox egress policy version.",
        )
    origins = tuple(dict.fromkeys(
        _canonical_egress_origin(
            f"{urlsplit(rule['url_prefix']).scheme}://"
            f"{urlsplit(rule['url_prefix']).netloc}"
        )
        for rule in rules
    ))
    asserted_origins = _validated_egress_origins(
        payload.get("egress_origins", [])
    )
    if asserted_origins != origins:
        raise ProtocolError(
            "invalid_egress_policy",
            "Origin-only sandbox egress is forbidden; origins must equal the "
            "exact method/prefix projection.",
        )
    private_origins = _validated_egress_origins(
        payload.get("private_origins", [])
    )
    if any(origin not in set(origins) for origin in private_origins):
        raise ProtocolError(
            "invalid_egress_policy",
            "Private sandbox origins must be derived from exact URL rules.",
        )
    return tuple(rules), origins, private_origins


def _validated_egress_budget_binding(
    payload: dict[str, Any],
    *,
    has_rules: bool,
) -> tuple[int, str | None, str | None]:
    """Validate the additive v3 proxy budget/audit binding."""

    version = payload.get("egress_policy_version")
    has_scope_field = "budget_scope_sha256" in payload
    has_call_field = "call_id_sha256" in payload
    if version == 3:
        if not has_rules or not has_scope_field or not has_call_field:
            raise ProtocolError(
                "invalid_egress_policy",
                "Egress policy v3 requires exact rules and both runtime-owned "
                "budget bindings.",
            )
        scope = payload.get("budget_scope_sha256")
        call = payload.get("call_id_sha256")
        if (
            not isinstance(scope, str)
            or not isinstance(call, str)
            or re.fullmatch(r"[0-9a-f]{64}", scope) is None
            or re.fullmatch(r"[0-9a-f]{64}", call) is None
        ):
            raise ProtocolError(
                "invalid_egress_policy",
                "Egress policy v3 budget bindings must be lowercase SHA-256 "
                "digests.",
            )
        return 3, scope, call
    if has_scope_field or has_call_field:
        raise ProtocolError(
            "invalid_egress_policy",
            "Legacy egress policies cannot carry v3 budget bindings.",
        )
    if (
        has_rules
        and REQUIRE_EGRESS_POLICY_V3
    ):
        raise ProtocolError(
            "egress_policy_upgrade_required",
            "This deployment requires aggregate-budgeted egress policy v3.",
        )
    return 2, None, None


def _configured_egress_limits() -> dict[str, int]:
    """Return deployment-owned v3 aggregate limits within code hard bounds."""

    def configured(
        name: str,
        *,
        default: int,
        maximum: int,
    ) -> int:
        raw = os.environ.get(name, str(default)).strip()
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid_{name.lower()}") from exc
        if not 1 <= value <= maximum:
            raise RuntimeError(f"invalid_{name.lower()}")
        return value

    return {
        "max_outbound_bytes": configured(
            "EXECUTOR_EGRESS_MAX_OUTBOUND_BYTES_PER_SCOPE",
            default=DEFAULT_EGRESS_MAX_OUTBOUND_BYTES_PER_SCOPE,
            maximum=MAX_EGRESS_MAX_OUTBOUND_BYTES_PER_SCOPE,
        ),
        "max_requests": configured(
            "EXECUTOR_EGRESS_MAX_REQUESTS_PER_SCOPE",
            default=DEFAULT_EGRESS_MAX_REQUESTS_PER_SCOPE,
            maximum=MAX_EGRESS_MAX_REQUESTS_PER_SCOPE,
        ),
        "max_response_wire_bytes": configured(
            "EXECUTOR_EGRESS_MAX_RESPONSE_WIRE_BYTES_PER_SCOPE",
            default=DEFAULT_EGRESS_MAX_RESPONSE_WIRE_BYTES_PER_SCOPE,
            maximum=MAX_EGRESS_MAX_RESPONSE_WIRE_BYTES_PER_SCOPE,
        ),
    }


@dataclass
class _EgressBridgeHandle:
    server: Any
    thread: threading.Thread
    environment: dict[str, str]
    policy_version: int
    audit_receipt: dict[str, Any] | None = None
    closed: bool = False

    def close(self) -> dict[str, Any] | None:
        if self.closed:
            return (
                None
                if self.audit_receipt is None
                else json.loads(json.dumps(self.audit_receipt))
            )
        terminal_receipt: dict[str, Any] | None = None
        shutdown_and_seal = getattr(
            self.server,
            "shutdown_and_seal",
            None,
        )
        try:
            if callable(shutdown_and_seal):
                receipt = shutdown_and_seal()
                terminal_receipt = (
                    dict(receipt)
                    if isinstance(receipt, dict)
                    else None
                )
            else:
                self.server.shutdown()
                self.server.server_close()
        except Exception as exc:
            # Do not mark the handle closed: active handlers may finish after
            # the bounded wait, and a later cleanup attempt must be able to
            # retry sealing rather than returning a stale partial receipt.
            raise ProtocolError(
                "egress_bridge_cleanup_failed",
                "Session sandbox egress bridge did not drain and seal cleanly.",
            ) from exc
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise ProtocolError(
                "egress_bridge_cleanup_failed",
                "Session sandbox egress bridge did not stop cleanly.",
            )
        if terminal_receipt is None:
            receipt_builder = getattr(
                self.server,
                "audit_receipt",
                None,
            )
            receipt = (
                receipt_builder()
                if callable(receipt_builder)
                else None
            )
            terminal_receipt = (
                dict(receipt)
                if isinstance(receipt, dict)
                else None
            )
        if self.policy_version == 3 and terminal_receipt is None:
            raise ProtocolError(
                "egress_audit_unavailable",
                "Bounded egress ended without its required audit receipt.",
            )
        self.audit_receipt = (
            None
            if terminal_receipt is None
            else json.loads(json.dumps(terminal_receipt))
        )
        self.closed = True
        return (
            None
            if self.audit_receipt is None
            else json.loads(json.dumps(self.audit_receipt))
        )


def _seal_egress_bridge(
    bridge: Any,
) -> dict[str, Any] | None:
    """Seal one bridge and normalize every implementation failure."""

    try:
        receipt = bridge.close()
    except ProtocolError:
        raise
    except Exception as exc:
        raise ProtocolError(
            "egress_bridge_cleanup_failed",
            "Session sandbox egress bridge did not drain and seal cleanly.",
        ) from exc
    if receipt is not None and not isinstance(receipt, dict):
        raise ProtocolError(
            "egress_audit_unavailable",
            "Session sandbox egress bridge returned an invalid audit receipt.",
        )
    return receipt


def _register_orphaned_v1_egress_bridge(bridge: Any) -> str:
    """Transfer an unsealed one-shot bridge to controller-owned quarantine."""

    global _ACTIVE_V1_EXECUTION, _ACTIVE_V1_EXECUTION_QUARANTINED
    with _EXECUTION_ADMISSION_LOCK:
        for token, retained in _ORPHANED_V1_EGRESS_BRIDGES.items():
            if retained is bridge:
                return token
        token = uuid.uuid4().hex
        _ORPHANED_V1_EGRESS_BRIDGES[token] = bridge
        # A bridge can retain accepted sockets and incomplete audit state even
        # after the worker process/tree is gone. Keep the unified execution
        # admission closed until a controller cleanup pass seals it.
        _ACTIVE_V1_EXECUTION = True
        _ACTIVE_V1_EXECUTION_QUARANTINED = True
        return token


def _v1_orphaned_egress_bridge_count() -> int:
    with _EXECUTION_ADMISSION_LOCK:
        return len(_ORPHANED_V1_EGRESS_BRIDGES)


def _seal_or_quarantine_one_shot_egress_bridge(
    bridge: Any,
) -> tuple[dict[str, Any] | None, ProtocolError | None]:
    """Bounded seal attempts, then transfer ownership without losing reachability."""

    last_error: ProtocolError | None = None
    for _ in range(2):
        try:
            return _seal_egress_bridge(bridge), None
        except ProtocolError as exc:
            last_error = exc
    _register_orphaned_v1_egress_bridge(bridge)
    return None, last_error


def _retry_orphaned_v1_egress_bridges(
) -> tuple[int, list[ProtocolError]]:
    """Retry every quarantined bridge independently and retain failed entries."""

    sealed = 0
    failures: list[ProtocolError] = []
    with _V1_EGRESS_BRIDGE_CLEANUP_LOCK:
        with _EXECUTION_ADMISSION_LOCK:
            retained = list(_ORPHANED_V1_EGRESS_BRIDGES.items())
        for token, bridge in retained:
            try:
                _seal_egress_bridge(bridge)
            except ProtocolError as exc:
                failures.append(exc)
                continue
            with _EXECUTION_ADMISSION_LOCK:
                if _ORPHANED_V1_EGRESS_BRIDGES.get(token) is bridge:
                    _ORPHANED_V1_EGRESS_BRIDGES.pop(token, None)
                    sealed += 1
    return sealed, failures


def _start_egress_bridge(
    origins: tuple[str, ...],
    *,
    egress_rules: tuple[dict[str, Any], ...] = (),
    private_origins: tuple[str, ...] = (),
    policy_version: int = 2,
    budget_scope_sha256: str | None = None,
    call_id_sha256: str | None = None,
    runtime_root: Path,
) -> _EgressBridgeHandle | None:
    """Create one lease-scoped loopback bridge from trusted policy state."""

    if (
        policy_version not in {2, 3}
        or (
            policy_version == 3
            and (
                not isinstance(budget_scope_sha256, str)
                or not isinstance(call_id_sha256, str)
            )
        )
        or (
            policy_version == 2
            and (
                budget_scope_sha256 is not None
                or call_id_sha256 is not None
            )
        )
    ):
        raise ProtocolError(
            "invalid_egress_policy",
            "Session sandbox bridge received an inconsistent policy binding.",
        )
    try:
        from chatds_browser_runtime.proxy_bridge import (
            EXPECTED_BRIDGE_GID,
            EXPECTED_PROXY_UID,
            PROXY_CA_CERTIFICATE_PATH,
            PROXY_LEAF_SPKI_PATH,
            PROXY_SOCKET_PATH,
            LoopbackProxyBridge,
            ProxySocketAuthority,
            ProxyTrustAuthority,
        )
    except ImportError:
        try:
            from browser_runtime.chatds_browser_runtime.proxy_bridge import (
                EXPECTED_BRIDGE_GID,
                EXPECTED_PROXY_UID,
                PROXY_CA_CERTIFICATE_PATH,
                PROXY_LEAF_SPKI_PATH,
                PROXY_SOCKET_PATH,
                LoopbackProxyBridge,
                ProxySocketAuthority,
                ProxyTrustAuthority,
            )
        except ImportError as exc:
            raise ProtocolError(
                "egress_bridge_unavailable",
                "The fixed session sandbox egress bridge is unavailable.",
            ) from exc
    authority = ProxySocketAuthority(
        PROXY_SOCKET_PATH,
        expected_uid=EXPECTED_PROXY_UID,
        expected_gid=EXPECTED_BRIDGE_GID,
    )
    try:
        authority.validate()
        worker_uid, worker_gid = _configured_worker_identity()
        trust_environment = ProxyTrustAuthority(
            PROXY_CA_CERTIFICATE_PATH,
            PROXY_LEAF_SPKI_PATH,
            expected_uid=EXPECTED_PROXY_UID,
            expected_gid=EXPECTED_BRIDGE_GID,
        ).materialize(
            runtime_root,
            worker_uid=worker_uid,
            worker_gid=worker_gid,
        )
        bridge = LoopbackProxyBridge(
            authority,
            ("127.0.0.1", 0),
            origin_allowlist=origins,
            egress_rules=egress_rules,
            private_origins=private_origins,
            trust_generation=trust_environment[
                "SKILL_EGRESS_TRUST_GENERATION"
            ],
            **(
                {
                    "budget_scope_sha256": budget_scope_sha256,
                    "call_id_sha256": call_id_sha256,
                    "limits": _configured_egress_limits(),
                }
                if policy_version == 3
                else {}
            ),
        )
    except Exception as exc:
        raise ProtocolError(
            "egress_bridge_unavailable",
            "The fixed session sandbox egress bridge failed closed.",
        ) from exc
    thread = threading.Thread(
        target=bridge.serve_forever,
        kwargs={"poll_interval": 0.1},
        daemon=True,
        name="session-sandbox-egress",
    )
    thread.start()
    port = int(bridge.server_address[1])
    proxy = f"http://127.0.0.1:{port}"
    environment = {
        "SKILL_EGRESS_PROXY_URL": proxy,
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "ALL_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "all_proxy": proxy,
        # Selenium clients need their private local driver. No non-loopback
        # destination can bypass Docker's network_mode:none boundary.
        "NO_PROXY": "localhost,127.0.0.1,[::1]",
        "no_proxy": "localhost,127.0.0.1,[::1]",
        "NODE_USE_ENV_PROXY": "1",
        **trust_environment,
    }
    return _EgressBridgeHandle(
        server=bridge,
        thread=thread,
        environment=environment,
        policy_version=policy_version,
    )


def _request_kind_allowed(kind: str) -> bool:
    raw = os.environ.get("EXECUTOR_ALLOWED_REQUEST_KINDS", "")
    if not raw.strip():
        return kind != "process_lease"
    allowed = {
        item.strip()
        for item in raw.split(",")
        if item.strip()
    }
    return kind in allowed


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _communicate_capped(
    proc: subprocess.Popen[bytes],
    *,
    timeout: int,
) -> tuple[bytes, bytes, bool, bool, bool]:
    """Continuously drain both streams while retaining only bounded prefixes."""

    values = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    caps = {"stdout": MAX_STDOUT_BYTES, "stderr": MAX_STDERR_BYTES}

    def drain(name: str, stream: Any) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = max(0, caps[name] - len(values[name]))
                if remaining:
                    values[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[name] = True
        except (OSError, ValueError):
            return

    threads: list[threading.Thread] = []
    for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        if stream is None:
            continue
        thread = threading.Thread(target=drain, args=(name, stream), daemon=True)
        thread.start()
        threads.append(thread)

    try:
        proc.wait(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)

    # A Skill may fork children which outlive its entrypoint. Every child is
    # placed in a fresh session by native Popen options; reap that group
    # after the leader exits so no background code survives one request.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    _sweep_configured_worker_uid()

    for thread in threads:
        thread.join(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    return (
        bytes(values["stdout"]),
        bytes(values["stderr"]),
        truncated["stdout"],
        truncated["stderr"],
        timed_out,
    )


def _run_code(payload: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("code", "")
    if not isinstance(code, str):
        return {
            "status": "error",
            "error": "Code must be a string.",
            "network": "disabled",
        }
    try:
        code_size = len(code.encode("utf-8", errors="strict"))
    except UnicodeError:
        return {
            "status": "error",
            "error": "Code must be valid UTF-8.",
            "network": "disabled",
        }
    if code_size > MAX_CODE_BYTES:
        return {
            "status": "error",
            "error": f"Code exceeds the {MAX_CODE_BYTES}-byte limit.",
            "network": "disabled",
        }
    try:
        requested_timeout = int(payload.get("timeout", 30))
    except (TypeError, ValueError):
        requested_timeout = 30
    timeout = max(1, min(requested_timeout, MAX_CODE_TIMEOUT))
    if not code.strip():
        return {"status": "error", "error": "No code provided."}

    started = time.monotonic()
    temp_dir = _make_execution_temp_dir("exec_")
    script = temp_dir / "script.py"
    script.write_text(code, encoding="utf-8")
    os.chmod(script, 0o444)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(temp_dir),
        "TMPDIR": str(temp_dir),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        **BLAS_THREAD_ENV,
    }
    _prepare_worker_tree(temp_dir, root_group_writable=True)

    try:
        proc = subprocess.Popen(
            _resource_limited_command(
                [sys.executable, "-I", "-B", str(script)],
                cpu_seconds=timeout + 5,
                persistent=False,
            ),
            cwd=temp_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            **_native_worker_popen_kwargs(),
        )
        stdout, stderr, stdout_truncated, stderr_truncated, timed_out = (
            _communicate_capped(proc, timeout=timeout)
        )
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        result: dict[str, Any] = {
            "status": "success",
            "output": stdout_text,
            "exit_code": proc.returncode,
            "duration_seconds": round(time.monotonic() - started, 2),
            "network": "disabled",
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        if timed_out:
            result.update(status="timeout", error=f"Script timed out after {timeout}s")
        elif proc.returncode != 0:
            result.update(
                status="error",
                error=stderr_text or f"Script exited with code {proc.returncode}",
            )
            if stderr_text:
                result["output"] = stdout_text + "\n--- stderr ---\n" + stderr_text
        return result
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    finally:
        _teardown_one_shot_temp_dir(temp_dir)


def _safe_relative_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError("invalid_path", f"{field} must be a non-empty relative path.")
    if "\x00" in value or "\\" in value:
        raise ProtocolError("invalid_path", f"{field} contains an unsafe path character.")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ProtocolError("invalid_path", f"{field} is not valid UTF-8.") from exc
    components = value.split("/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or len(encoded) > MAX_PATH_BYTES
        or len(components) > MAX_PATH_DEPTH
        or any(part in {"", ".", ".."} for part in components)
        or any(len(part.encode("utf-8")) > MAX_PATH_COMPONENT_BYTES for part in components)
    ):
        raise ProtocolError("invalid_path", f"{field} is outside the bounded relative-path policy.")
    return "/".join(components)


def _decode_snapshot(
    value: Any,
    *,
    field: str,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> dict[str, bytes]:
    if not isinstance(value, list):
        raise ProtocolError("invalid_snapshot", f"{field} must be an array of file snapshots.")
    if len(value) > max_files:
        raise ProtocolError("snapshot_limit_exceeded", f"{field} exceeds the {max_files}-file limit.")
    decoded: dict[str, bytes] = {}
    total = 0
    required_fields = {"path", "content_b64", "size_bytes", "sha256"}
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != required_fields:
            raise ProtocolError(
                "invalid_snapshot",
                f"{field}[{index}] must contain exactly path/content_b64/size_bytes/sha256.",
            )
        relative = _safe_relative_path(item.get("path"), field=f"{field}[{index}].path")
        if relative in decoded:
            raise ProtocolError("duplicate_snapshot_path", f"Duplicate snapshot path: {relative}")
        size = item.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ProtocolError("invalid_snapshot", f"{field}[{index}].size_bytes is invalid.")
        if size > max_file_bytes:
            raise ProtocolError("snapshot_limit_exceeded", f"Snapshot file is too large: {relative}")
        digest = item.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ProtocolError("invalid_snapshot", f"{field}[{index}].sha256 is invalid.")
        content = item.get("content_b64")
        if not isinstance(content, str):
            raise ProtocolError("invalid_snapshot", f"{field}[{index}].content_b64 is invalid.")
        try:
            raw = base64.b64decode(content.encode("ascii"), validate=True)
        except (UnicodeError, binascii.Error, ValueError) as exc:
            raise ProtocolError("invalid_snapshot", f"Invalid base64 for snapshot file: {relative}") from exc
        if len(raw) != size:
            raise ProtocolError("snapshot_integrity_error", f"Size mismatch for snapshot file: {relative}")
        actual_digest = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(actual_digest, digest):
            raise ProtocolError("snapshot_integrity_error", f"SHA-256 mismatch for snapshot file: {relative}")
        total += size
        if total > max_total_bytes:
            raise ProtocolError("snapshot_limit_exceeded", f"{field} exceeds its aggregate byte limit.")
        decoded[relative] = raw
    return decoded


def _materialize_snapshot(root: Path, files: dict[str, bytes], *, immutable: bool) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    for relative, content in files.items():
        destination = root.joinpath(*relative.split("/"))
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(content)
        os.chmod(
            destination,
            (
                _immutable_snapshot_file_mode(
                    destination,
                    group_only=False,
                )
                if immutable
                else 0o600
            ),
        )
    if immutable:
        directories = [root]
        directories.extend(path for path in root.rglob("*") if path.is_dir())
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            os.chmod(directory, 0o555)


def _validated_args(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ARGS:
        raise ProtocolError("invalid_args", f"argv must be an array with at most {MAX_ARGS} strings.")
    result: list[str] = []
    total = 0
    for index, item in enumerate(value):
        if not isinstance(item, str) or "\x00" in item or len(item) > MAX_ARG_CHARS:
            raise ProtocolError("invalid_args", f"argv[{index}] is not a bounded NUL-free string.")
        try:
            total += len(item.encode("utf-8", errors="strict"))
        except UnicodeError as exc:
            raise ProtocolError("invalid_args", f"argv[{index}] is not valid UTF-8.") from exc
        if total > MAX_ARG_BYTES:
            raise ProtocolError("invalid_args", "argv exceeds its aggregate byte limit.")
        result.append(item)
    return result


def _validated_timeout(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SKILL_TIMEOUT:
        raise ProtocolError(
            "invalid_timeout",
            f"timeout must be an integer between 1 and {MAX_SKILL_TIMEOUT} seconds.",
        )
    return value


def _validated_request_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ProtocolError("invalid_request_id", "request_id must be a bounded UUID string.")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ProtocolError("invalid_request_id", "request_id must be a UUID string.") from exc


def _validated_runtime_strings(
    value: Any,
    *,
    field: str,
    limit: int,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ProtocolError(
            "invalid_runtime_capability_request",
            f"{field} must be an array with at most {limit} strings.",
        )
    result: list[str] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, str)
            or not item
            or "\x00" in item
            or "\r" in item
            or "\n" in item
            or len(item) > MAX_RUNTIME_DECLARATION_CHARS
        ):
            raise ProtocolError(
                "invalid_runtime_capability_request",
                f"{field}[{index}] is not a bounded declaration string.",
            )
        if pattern is not None and pattern.fullmatch(item) is None:
            raise ProtocolError(
                "invalid_runtime_capability_request",
                f"{field}[{index}] has unsupported syntax.",
            )
        result.append(item)
    if len(set(result)) != len(result):
        raise ProtocolError(
            "invalid_runtime_capability_request",
            f"{field} must not contain duplicate declarations.",
        )
    return result


def _runtime_platform_name() -> str:
    current = sys.platform.casefold()
    if current.startswith("linux"):
        return "linux"
    if current == "darwin":
        return "macos"
    if current.startswith(("win", "cygwin", "msys")):
        return "windows"
    return current.replace("_", "-")


def _installed_requirement_leaf(requirement: Requirement) -> dict[str, Any]:
    if requirement.url:
        return {
            "status": "unsupported_direct_reference",
            "satisfied": False,
        }
    try:
        distribution = importlib.metadata.distribution(requirement.name)
    except importlib.metadata.PackageNotFoundError:
        return {
            "status": "missing",
            "satisfied": False,
        }
    installed_version = distribution.version
    if requirement.specifier and installed_version not in requirement.specifier:
        return {
            "status": "version_conflict",
            "satisfied": False,
            "installed_version": installed_version,
        }
    return {
        "status": "satisfied",
        "satisfied": True,
        "installed_version": installed_version,
        "distribution": distribution,
    }


def _installed_requirement_status(requirement: Requirement) -> dict[str, Any]:
    """Evaluate one parsed PEP 508 requirement against this exact image."""

    marker_environment = default_environment()
    marker_environment["extra"] = ""
    if requirement.marker is not None and not requirement.marker.evaluate(marker_environment):
        return {
            "status": "marker_not_applicable",
            "satisfied": True,
        }
    leaf = _installed_requirement_leaf(requirement)
    if leaf.get("satisfied") is not True:
        return leaf
    distribution = leaf.pop("distribution")
    installed_version = str(leaf.get("installed_version") or "")

    # Extras are capabilities, not merely alternate labels. Verify every
    # direct dependency activated by the selected extra(s), as well as the
    # distribution's ordinary direct requirements, against the same image.
    unsatisfied_dependencies: list[dict[str, str]] = []
    dependency_records = distribution.requires or []
    if len(dependency_records) > 256:
        return {
            "status": "dependency_metadata_limit_exceeded",
            "satisfied": False,
            "installed_version": installed_version,
        }
    selected_extras = sorted(requirement.extras)
    environments = []
    for extra in ["", *selected_extras]:
        environment = default_environment()
        environment["extra"] = extra
        environments.append(environment)
    for raw_dependency in dependency_records:
        try:
            dependency = Requirement(raw_dependency)
        except InvalidRequirement:
            return {
                "status": "invalid_distribution_metadata",
                "satisfied": False,
                "installed_version": installed_version,
            }
        if dependency.marker is not None and not any(
            dependency.marker.evaluate(environment)
            for environment in environments
        ):
            continue
        dependency_status = _installed_requirement_leaf(
            Requirement(str(dependency).split(";", 1)[0].strip())
        )
        dependency_status.pop("distribution", None)
        if dependency_status.get("satisfied") is True:
            continue
        if len(unsatisfied_dependencies) < 20:
            unsatisfied_dependencies.append({
                "requirement": str(dependency)[:MAX_RUNTIME_DECLARATION_CHARS],
                "status": str(dependency_status.get("status") or "unsatisfied"),
            })
    if unsatisfied_dependencies:
        return {
            "status": "dependency_unsatisfied",
            "satisfied": False,
            "installed_version": installed_version,
            "unsatisfied_dependencies": unsatisfied_dependencies,
        }
    return {
        "status": "satisfied",
        "satisfied": True,
        "installed_version": installed_version,
        **({"extras_checked": selected_extras} if selected_extras else {}),
    }


def _runtime_capability_error(
    request_id: str | None,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "runtime_capabilities_result",
        "request_id": request_id,
        "status": "error",
        "error_code": code,
        "error": message,
        "network": "disabled",
        "valid": False,
    }


def _run_runtime_capabilities(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate declarations without importing or executing Skill code."""

    request_id: str | None = None
    try:
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise ProtocolError(
                "unsupported_protocol",
                f"protocol_version must be {PROTOCOL_VERSION}.",
            )
        request_id = _validated_request_id(payload.get("request_id"))
        with _EXECUTION_ADMISSION_LOCK:
            one_shot_quarantined = _ACTIVE_V1_EXECUTION_QUARANTINED
            orphaned_bridge_count = len(
                _ORPHANED_V1_EGRESS_BRIDGES
            )
        if one_shot_quarantined or orphaned_bridge_count:
            raise ProtocolError(
                "worker_containment_failed",
                "Executor one-shot admission remains quarantined pending "
                "controller cleanup.",
            )
        requirements = _validated_runtime_strings(
            payload.get("requirements", []),
            field="requirements",
            limit=MAX_RUNTIME_REQUIREMENTS,
        )
        commands = _validated_runtime_strings(
            payload.get("commands", []),
            field="commands",
            limit=MAX_RUNTIME_COMMANDS,
            pattern=RUNTIME_COMMAND_RE,
        )
        environment_variables = _validated_runtime_strings(
            payload.get("environment_variables", []),
            field="environment_variables",
            limit=MAX_RUNTIME_ENVIRONMENT_VARIABLES,
            pattern=RUNTIME_ENVIRONMENT_RE,
        )
        raw_platform_groups = payload.get("platform_groups", [])
        if (
            not isinstance(raw_platform_groups, list)
            or len(raw_platform_groups) > MAX_RUNTIME_PLATFORM_GROUPS
        ):
            raise ProtocolError(
                "invalid_runtime_capability_request",
                "platform_groups is not a bounded array.",
            )
        platform_groups = [
            _validated_runtime_strings(
                group,
                field=f"platform_groups[{index}]",
                limit=MAX_RUNTIME_PLATFORMS_PER_GROUP,
                pattern=RUNTIME_PLATFORM_RE,
            )
            for index, group in enumerate(raw_platform_groups)
        ]

        requirement_results: list[dict[str, Any]] = []
        for raw_requirement in requirements:
            try:
                parsed_requirement = Requirement(raw_requirement)
            except InvalidRequirement:
                result: dict[str, Any] = {
                    "status": "invalid_requirement",
                    "satisfied": False,
                }
            else:
                result = _installed_requirement_status(parsed_requirement)
            requirement_results.append({
                "requirement": raw_requirement,
                **result,
            })

        command_results = [
            {
                "name": name,
                "available": shutil.which(
                    name,
                    path="/usr/local/bin:/usr/bin:/bin",
                ) is not None,
            }
            for name in commands
        ]
        runtime_profile, egress_policy, profile_environment = _profile_process_environment()
        available_environment = set(SKILL_RUNTIME_ENVIRONMENT_VARIABLES)
        available_environment.update(profile_environment)
        if runtime_profile in {
            "browser-automation-v1",
            SESSION_SANDBOX_RUNTIME_PROFILE,
        }:
            # The trusted launcher creates these values per lease after it
            # starts the private non-root Wayland compositor.  DISPLAY is
            # deliberately not advertised: this profile has no shared X11
            # server and must not claim one is available.
            available_environment.update({"WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"})
        environment_results = [
            {
                "name": name,
                "available": name in available_environment,
            }
            for name in environment_variables
        ]
        current_platform = _runtime_platform_name()
        platform_results = [
            {
                "allowed": allowed,
                "current": current_platform,
                "satisfied": not allowed or current_platform in allowed,
            }
            for allowed in platform_groups
        ]
        valid = (
            all(item.get("satisfied") is True for item in requirement_results)
            and all(item["available"] for item in command_results)
            and all(item["available"] for item in environment_results)
            and all(item["satisfied"] for item in platform_results)
        )
        worker_uid, worker_gid = _configured_worker_identity()
        worker_configured = (
            os.environ.get("EXECUTOR_WORKER_UID") is not None
            and os.environ.get("EXECUTOR_WORKER_GID") is not None
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "runtime_capabilities_result",
            "request_id": request_id,
            "status": "success",
            "valid": valid,
            "runtime_identity": {
                "execution_runtime": "isolated_skill_executor",
                "python_implementation": sys.implementation.name,
                "python_version": ".".join(str(part) for part in sys.version_info[:3]),
                "platform": current_platform,
                "network": "disabled",
                "runtime_build_sha256": _runtime_build_sha256(),
                "runtime_profile": runtime_profile,
                "network_policy": {
                    "direct": "disabled",
                    "egress": egress_policy,
                },
                "display_backend": (
                    "wayland-headless"
                    if runtime_profile in {
                        "browser-automation-v1",
                        SESSION_SANDBOX_RUNTIME_PROFILE,
                    }
                    else "none"
                ),
                "headed_browser": runtime_profile in {
                    "browser-automation-v1",
                    SESSION_SANDBOX_RUNTIME_PROFILE,
                },
                "x11": False,
                "dependency_install": "disabled",
                "execution_identity": {
                    "controller_uid": os.geteuid(),
                    "controller_gid": os.getegid(),
                    "worker_uid": worker_uid,
                    "worker_gid": worker_gid,
                    "uid_isolated": (
                        worker_configured
                        and worker_uid != os.geteuid()
                    ),
                    "resource_launcher": "prlimit",
                    "shared_state_isolated": (
                        os.environ.get(
                            "EXECUTOR_ENFORCE_SHARED_STATE_ISOLATION"
                        ) == "1"
                    ),
                },
            },
            "requirements": requirement_results,
            "commands": command_results,
            "environment_variables": environment_results,
            "platform_groups": platform_results,
        }
    except ProtocolError as exc:
        return _runtime_capability_error(request_id, exc.code, str(exc))
    except Exception as exc:
        return _runtime_capability_error(
            request_id,
            "runtime_capability_internal_error",
            f"Runtime capability evaluation failed safely ({type(exc).__name__}).",
        )


def _validate_json_value(value: Any, *, field: str, depth: int = 0) -> None:
    if depth > MAX_FUNCTION_JSON_DEPTH:
        raise ProtocolError(
            "invalid_function_call",
            f"{field} exceeds JSON nesting depth {MAX_FUNCTION_JSON_DEPTH}.",
        )
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, field=field, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError(
                    "invalid_function_call", f"{field} object keys must be strings."
                )
            _validate_json_value(item, field=field, depth=depth + 1)
        return
    raise ProtocolError(
        "invalid_function_call",
        f"{field} contains a non-JSON value of type {type(value).__name__}.",
    )


def _validated_invocation(
    value: Any,
    *,
    entrypoint_relative: str,
    entrypoint_bytes: bytes,
    argv: list[str],
) -> tuple[str, str | None, str | None, str | None, bytes | None]:
    """Validate the declarative invocation without importing Skill code."""

    if value is None:
        return "cli", None, None, None, None
    if not isinstance(value, dict):
        raise ProtocolError("invalid_invocation", "invocation must be an object.")
    mode = value.get("mode")
    if mode == "cli":
        if set(value) != {"mode"}:
            raise ProtocolError(
                "invalid_invocation", "CLI invocation must contain exactly mode."
            )
        return "cli", None, None, None, None
    function_keys = {"mode", "name", "args", "kwargs"}
    instance_keys = {
        "mode",
        "class_name",
        "method_name",
        "constructor_args",
        "constructor_kwargs",
        "method_args",
        "method_kwargs",
    }
    if mode == "function" and set(value) == function_keys:
        callable_kind = "function"
    elif mode == "instance_method" and set(value) == instance_keys:
        callable_kind = "instance_method"
    else:
        raise ProtocolError(
            "invalid_invocation",
            "Callable invocation has an invalid mode or field set.",
        )
    if PurePosixPath(entrypoint_relative).suffix != ".py":
        raise ProtocolError(
            "invalid_function_call", "Callable invocation requires a Python entrypoint."
        )
    if argv:
        raise ProtocolError(
            "invalid_function_call", "Callable invocation cannot include CLI argv."
        )
    function_name: str | None = None
    class_name: str | None = None
    method_name: str | None = None
    if callable_kind == "function":
        function_name = value.get("name")
        if (
            not isinstance(function_name, str)
            or function_name.startswith("_")
            or not PUBLIC_FUNCTION_RE.fullmatch(function_name)
        ):
            raise ProtocolError(
                "invalid_function_call",
                "Function name must be one public, non-dotted Python identifier.",
            )
        positional = value.get("args")
        keywords = value.get("kwargs")
        if not isinstance(positional, list) or len(positional) > MAX_FUNCTION_ARGS:
            raise ProtocolError(
                "invalid_function_call",
                f"Function args must be an array with at most {MAX_FUNCTION_ARGS} items.",
            )
        if not isinstance(keywords, dict) or len(keywords) > MAX_FUNCTION_KWARGS:
            raise ProtocolError(
                "invalid_function_call",
                f"Function kwargs must be an object with at most {MAX_FUNCTION_KWARGS} items.",
            )
        argument_payload = {"args": positional, "kwargs": keywords}
    else:
        class_name = value.get("class_name")
        method_name = value.get("method_name")
        for field, name in (("class_name", class_name), ("method_name", method_name)):
            if (
                not isinstance(name, str)
                or name.startswith("_")
                or not PUBLIC_FUNCTION_RE.fullmatch(name)
            ):
                raise ProtocolError(
                    "invalid_function_call",
                    f"{field} must be one public, non-dotted Python identifier.",
                )
        init_positional = value.get("constructor_args")
        init_keywords = value.get("constructor_kwargs")
        call_positional = value.get("method_args")
        call_keywords = value.get("method_kwargs")
        if (
            not isinstance(init_positional, list)
            or not isinstance(call_positional, list)
            or len(init_positional) + len(call_positional) > MAX_FUNCTION_ARGS
        ):
            raise ProtocolError(
                "invalid_function_call",
                "Combined constructor and method args must be arrays with at most "
                f"{MAX_FUNCTION_ARGS} items.",
            )
        if (
            not isinstance(init_keywords, dict)
            or not isinstance(call_keywords, dict)
            or len(init_keywords) + len(call_keywords) > MAX_FUNCTION_KWARGS
        ):
            raise ProtocolError(
                "invalid_function_call",
                "Combined constructor and method kwargs must be objects with at "
                f"most {MAX_FUNCTION_KWARGS} keys.",
            )
        argument_payload = {
            "constructor_args": init_positional,
            "constructor_kwargs": init_keywords,
            "method_args": call_positional,
            "method_kwargs": call_keywords,
        }
    for field, argument_value in argument_payload.items():
        if isinstance(argument_value, dict):
            for key in argument_value:
                if (
                    not isinstance(key, str)
                    or key.startswith("_")
                    or not PUBLIC_FUNCTION_RE.fullmatch(key)
                ):
                    raise ProtocolError(
                        "invalid_function_call",
                        f"Invalid public keyword argument in {field}: {key!r}.",
                    )
        _validate_json_value(argument_value, field=field)
    try:
        request_bytes = json.dumps(
            argument_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolError(
            "invalid_function_call", "Callable arguments must be finite valid JSON."
        ) from exc
    if len(request_bytes) > MAX_FUNCTION_INPUT_BYTES:
        raise ProtocolError(
            "invalid_function_call",
            f"Callable argument JSON exceeds {MAX_FUNCTION_INPUT_BYTES} bytes.",
        )
    if len(entrypoint_bytes) > MAX_FUNCTION_SOURCE_BYTES:
        raise ProtocolError(
            "invalid_function_call",
            f"Python entrypoint exceeds {MAX_FUNCTION_SOURCE_BYTES} bytes for function inspection.",
        )
    try:
        tree = ast.parse(entrypoint_bytes.decode("utf-8"), filename=entrypoint_relative)
    except (UnicodeError, SyntaxError) as exc:
        raise ProtocolError(
            "invalid_function_call", "Python entrypoint cannot be inspected safely."
        ) from exc
    if callable_kind == "function":
        declared = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")
        }
        if function_name not in declared:
            raise ProtocolError(
                "invalid_function_call",
                f"Public top-level function {function_name!r} is not declared by the selected entrypoint.",
            )
        return "function", function_name, None, None, request_bytes

    matching_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == class_name
    ]
    if len(matching_classes) != 1:
        raise ProtocolError(
            "invalid_function_call",
            f"Public top-level class {class_name!r} is not uniquely declared by the selected entrypoint.",
        )
    class_node = matching_classes[0]
    matching_methods = [
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
        and not node.decorator_list
        and bool([*node.args.posonlyargs, *node.args.args])
    ]
    if len(matching_methods) != 1:
        raise ProtocolError(
            "invalid_function_call",
            "Direct public plain instance method "
            f"{class_name}.{method_name} is not uniquely declared by the "
            "selected entrypoint.",
        )
    return "instance_method", None, class_name, method_name, request_bytes


def _read_function_envelope(path: Path) -> dict[str, Any]:
    try:
        content, stable_stat = _read_regular_file(path, display_path="function result envelope")
    except ProtocolError:
        raise
    except Exception as exc:
        raise ProtocolError(
            "invalid_function_result", "Function runner did not produce a safe result envelope."
        ) from exc
    if stable_stat.st_size > MAX_FUNCTION_ENVELOPE_BYTES:
        raise ProtocolError(
            "invalid_function_result", "Function result envelope exceeded its byte limit."
        )
    try:
        envelope = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(
            "invalid_function_result", "Function runner produced invalid result JSON."
        ) from exc
    if not isinstance(envelope, dict) or envelope.get("status") not in {"success", "error"}:
        raise ProtocolError(
            "invalid_function_result", "Function runner result envelope has an invalid shape."
        )
    return envelope


def _interpreter_command(
    entrypoint: Path,
    *,
    skill_root: Path,
    runtime_root: Path,
    runtime_profile: str = "base-v1",
) -> tuple[str, list[str]]:
    kind = SUPPORTED_INTERPRETERS.get(entrypoint.suffix)
    if kind == "python":
        runner_path = runtime_root / "cli_runner.py"
        with runner_path.open("xb") as stream:
            stream.write(CLI_RUNNER_SOURCE.encode("utf-8"))
        os.chmod(runner_path, 0o400)
        arguments = [str(skill_root), str(entrypoint)]
        if runtime_profile in {
            "browser-automation-v1",
            SESSION_SANDBOX_RUNTIME_PROFILE,
        }:
            return kind, _browser_runtime_command(runner_path, arguments)
        return kind, [
            sys.executable,
            "-I",
            "-B",
            str(runner_path),
            *arguments,
        ]
    if kind == "bash":
        if runtime_profile in {
            "browser-automation-v1",
            SESSION_SANDBOX_RUNTIME_PROFILE,
        }:
            return kind, _browser_runtime_command(entrypoint, [])
        executable = Path("/bin/bash")
        if not executable.is_file():
            raise ProtocolError("interpreter_unavailable", "The fixed bash interpreter is unavailable.")
        return kind, [str(executable), "--noprofile", "--norc", str(entrypoint)]
    if kind == "node":
        if runtime_profile in {
            "browser-automation-v1",
            SESSION_SANDBOX_RUNTIME_PROFILE,
        }:
            return kind, _browser_runtime_command(entrypoint, [])
        executable = Path("/usr/bin/node")
        if not executable.is_file():
            raise ProtocolError("interpreter_unavailable", "The fixed Node.js interpreter is unavailable.")
        return kind, [str(executable), str(entrypoint)]
    raise ProtocolError(
        "unsupported_script_type",
        "entrypoint must end in .py, .sh, .bash, .js, .mjs, or .cjs.",
    )


def _browser_runtime_command(
    script: Path,
    arguments: list[str],
) -> list[str]:
    """Wrap every browser-profile entrypoint in its private display launcher."""

    try:
        item = TRUSTED_BROWSER_RUNTIME_LAUNCHER.lstat()
        resolved = TRUSTED_BROWSER_RUNTIME_LAUNCHER.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProtocolError(
            "browser_runtime_unavailable",
            "The fixed browser runtime launcher is unavailable.",
        ) from exc
    if (
        resolved != TRUSTED_BROWSER_RUNTIME_LAUNCHER
        or not stat.S_ISREG(item.st_mode)
        or item.st_uid != 0
        or item.st_mode & 0o022
        or not os.access(TRUSTED_BROWSER_RUNTIME_LAUNCHER, os.X_OK)
    ):
        raise ProtocolError(
            "browser_runtime_unavailable",
            "The fixed browser runtime launcher failed its trust policy.",
        )
    return [
        str(TRUSTED_BROWSER_RUNTIME_LAUNCHER),
        str(script),
        *arguments,
    ]


def _require_safe_output_fd_api() -> None:
    """Fail closed unless this executor can traverse output by descriptor."""

    if (
        not getattr(os, "O_NOFOLLOW", 0)
        or not getattr(os, "O_DIRECTORY", 0)
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or os.scandir not in os.supports_fd
    ):
        raise ProtocolError(
            "unsafe_workspace_output",
            "This executor cannot safely traverse workspace output by descriptor.",
        )


def _output_object_identity(item: os.stat_result) -> tuple[int, int, int]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
    )


def _stable_output_file_identity(
    item: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _stable_output_directory_identity(
    item: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _open_output_directory_at(
    parent_fd: int,
    name: str,
    *,
    display_path: str,
    expected: os.stat_result | None = None,
) -> tuple[int, os.stat_result]:
    """Open one directory component without ever following a link."""

    try:
        before = expected or os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ProtocolError(
            "unsafe_workspace_output",
            f"Cannot safely inspect output directory: {display_path}",
        ) from exc
    if not stat.S_ISDIR(before.st_mode):
        raise ProtocolError(
            "unsafe_workspace_output",
            f"Workspace output directory is unsafe: {display_path}",
        )
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ProtocolError(
            "unsafe_workspace_output",
            f"Cannot safely open output directory: {display_path}",
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _output_object_identity(before)
            != _output_object_identity(opened)
        ):
            raise ProtocolError(
                "workspace_output_race",
                f"Workspace output directory changed during collection: {display_path}",
            )
        try:
            rebound = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ProtocolError(
                "workspace_output_race",
                f"Workspace output directory changed during collection: {display_path}",
            ) from exc
        if _output_object_identity(rebound) != _output_object_identity(opened):
            raise ProtocolError(
                "workspace_output_race",
                f"Workspace output directory changed during collection: {display_path}",
            )
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _open_anchored_output_directory(
    path: Path,
    *,
    display_path: str,
) -> tuple[
    list[int],
    list[tuple[int, str, int, tuple[int, int, int]]],
]:
    """Open every absolute path component and retain the complete fd chain."""

    _require_safe_output_fd_api()
    if not path.is_absolute() or path.anchor != "/" or ".." in path.parts:
        raise ProtocolError(
            "unsafe_workspace_output",
            f"Workspace output root is not a canonical absolute directory: {display_path}",
        )
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        root_fd = os.open("/", flags)
    except OSError as exc:
        raise ProtocolError(
            "unsafe_workspace_output",
            f"Cannot safely anchor output directory: {display_path}",
        ) from exc
    descriptors = [root_fd]
    links: list[tuple[int, str, int, tuple[int, int, int]]] = []
    try:
        for component in path.parts[1:]:
            child_fd, opened = _open_output_directory_at(
                descriptors[-1],
                component,
                display_path=display_path,
            )
            links.append((
                descriptors[-1],
                component,
                child_fd,
                _output_object_identity(opened),
            ))
            descriptors.append(child_fd)
        return descriptors, links
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _validate_anchored_output_directory(
    links: list[tuple[int, str, int, tuple[int, int, int]]],
    *,
    display_path: str,
) -> None:
    """Prove no component in the retained root-to-leaf chain was rebound."""

    for parent_fd, name, child_fd, expected in links:
        try:
            linked = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            opened = os.fstat(child_fd)
        except OSError as exc:
            raise ProtocolError(
                "workspace_output_race",
                f"Workspace output path changed during collection: {display_path}",
            ) from exc
        if (
            _output_object_identity(linked) != expected
            or _output_object_identity(opened) != expected
        ):
            raise ProtocolError(
                "workspace_output_race",
                f"Workspace output path changed during collection: {display_path}",
            )


def _read_regular_file_at(
    parent_fd: int,
    name: str,
    *,
    display_path: str,
    expected: os.stat_result | None = None,
) -> tuple[bytes, os.stat_result]:
    """Read an unchanged regular file through its already-anchored parent."""

    try:
        linked_before = expected or os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ProtocolError(
            "unsafe_workspace_output",
            f"Cannot safely inspect output: {display_path}",
        ) from exc
    if (
        not stat.S_ISREG(linked_before.st_mode)
        or linked_before.st_nlink != 1
    ):
        raise ProtocolError(
            "unsafe_workspace_output",
            f"Workspace output is not an independent regular file: {display_path}",
        )
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ProtocolError(
            "unsafe_workspace_output",
            f"Cannot safely open output: {display_path}",
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _output_object_identity(linked_before)
            != _output_object_identity(before)
        ):
            raise ProtocolError(
                "workspace_output_race",
                f"Workspace output changed during collection: {display_path}",
            )
        if before.st_size > MAX_OUTPUT_FILE_BYTES:
            raise ProtocolError(
                "output_limit_exceeded",
                f"Workspace output is too large: {display_path}",
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            linked_after = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ProtocolError(
                "workspace_output_race",
                f"Workspace output changed during collection: {display_path}",
            ) from exc
        if (
            len(content) != before.st_size
            or _stable_output_file_identity(before)
            != _stable_output_file_identity(after)
            or _stable_output_file_identity(before)
            != _stable_output_file_identity(linked_after)
        ):
            raise ProtocolError(
                "workspace_output_race",
                f"Workspace output changed during collection: {display_path}",
            )
        return content, after
    finally:
        os.close(descriptor)


def _read_regular_file(
    path: Path,
    *,
    display_path: str,
) -> tuple[bytes, os.stat_result]:
    descriptors, links = _open_anchored_output_directory(
        path.parent,
        display_path=display_path,
    )
    try:
        result = _read_regular_file_at(
            descriptors[-1],
            path.name,
            display_path=display_path,
        )
        _validate_anchored_output_directory(
            links,
            display_path=display_path,
        )
        return result
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _collect_workspace_artifacts_with_state(
    workspace: Path,
    initial: dict[str, tuple[int, str]],
) -> tuple[list[dict[str, Any]], int, dict[str, tuple[int, str]]]:
    artifacts: list[dict[str, Any]] = []
    observed: set[str] = set()
    current: dict[str, tuple[int, str]] = {}
    output_total = 0
    entries = 0
    descriptors, links = _open_anchored_output_directory(
        workspace,
        display_path="workspace",
    )

    def collect_directory(directory_fd: int, prefix: str, depth: int) -> None:
        nonlocal entries, output_total
        try:
            directory_before = os.fstat(directory_fd)
            remaining_entries = MAX_OUTPUT_ENTRIES - entries
            children: list[str] = []
            with os.scandir(directory_fd) as scanner:
                for child in scanner:
                    children.append(child.name)
                    if len(children) > remaining_entries:
                        raise ProtocolError(
                            "output_limit_exceeded",
                            "Workspace output has too many filesystem entries.",
                        )
            children.sort()
        except OSError as exc:
            raise ProtocolError(
                "unsafe_workspace_output",
                "Cannot safely scan the workspace output.",
            ) from exc
        if not stat.S_ISDIR(directory_before.st_mode):
            raise ProtocolError(
                "unsafe_workspace_output",
                "Workspace output traversal lost its directory anchor.",
            )
        for child_name in children:
            entries += 1
            if entries > MAX_OUTPUT_ENTRIES:
                raise ProtocolError("output_limit_exceeded", "Workspace output has too many filesystem entries.")
            relative = f"{prefix}/{child_name}" if prefix else child_name
            safe_relative = _safe_relative_path(relative, field="workspace output path")
            if PurePosixPath(safe_relative).parts[0] in RESERVED_WORKSPACE_ROOTS:
                raise ProtocolError(
                    "unsafe_workspace_output",
                    f"Writes to reserved workspace paths are forbidden: {safe_relative}",
                )
            try:
                item_stat = os.stat(
                    child_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise ProtocolError("unsafe_workspace_output", f"Cannot inspect output: {safe_relative}") from exc
            mode = item_stat.st_mode
            if stat.S_ISLNK(mode):
                raise ProtocolError("unsafe_workspace_output", f"Symlink output is forbidden: {safe_relative}")
            if stat.S_ISDIR(mode):
                if depth + 1 >= MAX_PATH_DEPTH:
                    raise ProtocolError("output_limit_exceeded", f"Workspace output is too deeply nested: {safe_relative}")
                child_fd, child_opened = _open_output_directory_at(
                    directory_fd,
                    child_name,
                    display_path=safe_relative,
                    expected=item_stat,
                )
                try:
                    collect_directory(
                        child_fd,
                        safe_relative,
                        depth + 1,
                    )
                    try:
                        linked_after = os.stat(
                            child_name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except OSError as exc:
                        raise ProtocolError(
                            "workspace_output_race",
                            f"Workspace output directory changed during collection: {safe_relative}",
                        ) from exc
                    if (
                        _output_object_identity(linked_after)
                        != _output_object_identity(child_opened)
                    ):
                        raise ProtocolError(
                            "workspace_output_race",
                            f"Workspace output directory changed during collection: {safe_relative}",
                        )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(mode) or item_stat.st_nlink != 1:
                raise ProtocolError(
                    "unsafe_workspace_output",
                    f"Only independent regular-file outputs are allowed: {safe_relative}",
                )
            observed.add(safe_relative)
            content, stable_stat = _read_regular_file_at(
                directory_fd,
                child_name,
                display_path=safe_relative,
                expected=item_stat,
            )
            digest = hashlib.sha256(content).hexdigest()
            current[safe_relative] = (stable_stat.st_size, digest)
            previous = initial.get(safe_relative)
            if previous == (stable_stat.st_size, digest):
                continue
            output_total += stable_stat.st_size
            if len(artifacts) >= MAX_OUTPUT_FILES or output_total > MAX_OUTPUT_TOTAL_BYTES:
                raise ProtocolError("output_limit_exceeded", "Changed workspace files exceed output limits.")
            artifacts.append({
                "path": safe_relative,
                "change": "modified" if previous is not None else "created",
                "content_b64": base64.b64encode(content).decode("ascii"),
                "size_bytes": stable_stat.st_size,
                "sha256": digest,
            })
        try:
            directory_after = os.fstat(directory_fd)
        except OSError as exc:
            raise ProtocolError(
                "workspace_output_race",
                "Workspace output directory changed during collection.",
            ) from exc
        if (
            _stable_output_directory_identity(directory_before)
            != _stable_output_directory_identity(directory_after)
        ):
            raise ProtocolError(
                "workspace_output_race",
                "Workspace output directory changed during collection.",
            )

    try:
        collect_directory(descriptors[-1], "", 0)
        _validate_anchored_output_directory(
            links,
            display_path="workspace",
        )
        artifacts.sort(key=lambda item: item["path"])
        return artifacts, len(set(initial) - observed), current
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _collect_workspace_artifacts(
    workspace: Path,
    initial: dict[str, tuple[int, str]],
) -> tuple[list[dict[str, Any]], int]:
    artifacts, deleted_count, _ = _collect_workspace_artifacts_with_state(
        workspace,
        initial,
    )
    return artifacts, deleted_count


def _skill_error(
    request_id: str | None,
    code: str,
    message: str,
    *,
    egress_policy: str = "none",
    **extra: Any,
) -> dict[str, Any]:
    runtime_profile = _configured_runtime_profile()
    if egress_policy not in {"none", "origin_allowlist_proxy"}:
        raise ProtocolError(
            "invalid_egress_policy",
            "Executor error receipt contains an invalid egress policy.",
        )
    response: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "skill_script_result",
        "request_id": request_id,
        "status": "error",
        "error_code": code,
        "error": message,
        "network": "disabled",
        "runtime_profile": runtime_profile,
        "network_policy": {
            "direct": "disabled",
            "egress": egress_policy,
        },
        "artifacts": [],
    }
    response.update(extra)
    return response


def _declared_command_error(
    request_id: str | None,
    code: str,
    message: str,
    *,
    executable: str | None = None,
    cwd: str | None = None,
    command_sha256: str | None = None,
    egress_policy: str = "none",
    **extra: Any,
) -> dict[str, Any]:
    runtime_profile = _configured_runtime_profile()
    if egress_policy not in {"none", "origin_allowlist_proxy"}:
        raise ProtocolError(
            "invalid_egress_policy",
            "Executor error receipt contains an invalid egress policy.",
        )
    response: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "declared_command_result",
        "request_id": request_id,
        "status": "error",
        "error_code": code,
        "error": message,
        "executable": executable,
        "cwd": cwd,
        "command_sha256": command_sha256,
        "shell": False,
        "network": "disabled",
        "runtime_profile": runtime_profile,
        "network_policy": {
            "direct": "disabled",
            "egress": egress_policy,
        },
        "artifacts": [],
    }
    response.update(extra)
    return response


def _run_declared_command(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute a trusted compiled executable plus literal argv, never a shell."""
    started = time.monotonic()
    request_id: str | None = None
    executable: str | None = None
    cwd_policy: str | None = None
    command_sha256: str | None = None
    temp_dir: Path | None = None
    runtime_profile: str | None = None
    egress_origins: tuple[str, ...] = ()
    egress_rules: tuple[dict[str, Any], ...] = ()
    private_origins: tuple[str, ...] = ()
    egress_policy = "none"
    egress_policy_version = 2
    budget_scope_sha256: str | None = None
    call_id_sha256: str | None = None
    egress_audit_receipt: dict[str, Any] | None = None
    egress_bridge: _EgressBridgeHandle | None = None
    proc: subprocess.Popen[bytes] | None = None
    try:
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise ProtocolError(
                "unsupported_protocol", f"protocol_version must be {PROTOCOL_VERSION}."
            )
        runtime_profile = _configured_runtime_profile()
        request_id = _validated_request_id(payload.get("request_id"))
        raw_executable = payload.get("executable")
        if not isinstance(raw_executable, str) or RUNTIME_COMMAND_RE.fullmatch(raw_executable) is None:
            raise ProtocolError(
                "invalid_executable",
                "executable must be one PATH command name without a slash.",
            )
        executable = raw_executable
        argv = _validated_args(payload.get("argv", []))
        timeout = _validated_timeout(payload.get("timeout", 120))
        (
            egress_rules,
            egress_origins,
            private_origins,
        ) = _validated_exact_egress_policy(
            payload
        )
        (
            egress_policy_version,
            budget_scope_sha256,
            call_id_sha256,
        ) = _validated_egress_budget_binding(
            payload,
            has_rules=bool(egress_rules),
        )
        egress_policy = (
            "origin_allowlist_proxy" if egress_origins else "none"
        )
        raw_cwd = payload.get("cwd", "workspace")
        if raw_cwd not in {"workspace", "skill"}:
            raise ProtocolError("invalid_cwd", "cwd must be exactly 'workspace' or 'skill'.")
        cwd_policy = raw_cwd
        command_sha256 = hashlib.sha256(json.dumps(
            [executable, *argv],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

        skill_files = _decode_snapshot(
            payload.get("skill_files"),
            field="skill_files",
            max_files=MAX_SKILL_FILES,
            max_file_bytes=MAX_SKILL_FILE_BYTES,
            max_total_bytes=MAX_SKILL_TOTAL_BYTES,
        )
        if "SKILL.md" not in skill_files:
            raise ProtocolError(
                "invalid_skill_snapshot", "The Skill snapshot must contain root SKILL.md."
            )
        workspace_files = _decode_snapshot(
            payload.get("workspace_files", []),
            field="workspace_files",
            max_files=MAX_WORKSPACE_FILES,
            max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
            max_total_bytes=MAX_WORKSPACE_TOTAL_BYTES,
        )

        temp_dir = _make_execution_temp_dir("declared_command_")
        skill_root = temp_dir / "skill"
        workspace = temp_dir / "workspace"
        runtime_root = temp_dir / "runtime"
        _materialize_snapshot(skill_root, skill_files, immutable=True)
        _materialize_snapshot(workspace, workspace_files, immutable=False)
        runtime_root.mkdir(mode=0o700)
        (runtime_root / "home").mkdir(mode=0o700)
        (runtime_root / "tmp").mkdir(mode=0o700)
        output_dir = workspace / "output_result"
        if output_dir.exists() and not output_dir.is_dir():
            raise ProtocolError(
                "invalid_workspace_snapshot", "workspace/output_result must be a directory."
            )
        output_dir.mkdir(mode=0o700, exist_ok=True)
        initial = {
            relative: (len(content), hashlib.sha256(content).hexdigest())
            for relative, content in workspace_files.items()
        }
        fixed_path = "/usr/local/bin:/usr/bin:/bin"
        resolved_executable = shutil.which(executable, path=fixed_path)
        if not resolved_executable:
            raise ProtocolError(
                "command_unavailable",
                "The declared executable is unavailable in the isolated runtime image.",
            )
        env = {
            "PATH": fixed_path,
            "HOME": str(runtime_root / "home"),
            "TMPDIR": str(runtime_root / "tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "CHATDS_WORKSPACE": str(workspace),
            "CHATDS_SKILL_DIR": str(skill_root),
            "CHATDS_SKILL_ROOT": str(skill_root),
            "SKILL_DIR": str(skill_root),
            "CHATDS_OUTPUT_DIR": str(output_dir),
            **BLAS_THREAD_ENV,
        }
        _prepare_worker_tree(
            temp_dir,
            immutable_roots=(skill_root,),
            writable_roots=(workspace, runtime_root),
        )
        egress_bridge = (
            _start_egress_bridge(
                egress_origins,
                egress_rules=egress_rules,
                private_origins=private_origins,
                policy_version=egress_policy_version,
                budget_scope_sha256=budget_scope_sha256,
                call_id_sha256=call_id_sha256,
                runtime_root=runtime_root,
            )
            if egress_origins
            else None
        )
        if egress_origins and egress_bridge is None:
            raise ProtocolError(
                "egress_bridge_unavailable",
                "The declared-command egress bridge failed closed.",
            )
        if egress_bridge is not None:
            env.update(egress_bridge.environment)
        try:
            proc = subprocess.Popen(
                _resource_limited_command(
                    [resolved_executable, *argv],
                    cpu_seconds=timeout + 5,
                    persistent=False,
                ),
                shell=False,
                cwd=(
                    workspace
                    if cwd_policy == "workspace"
                    else skill_root
                ),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                **_native_worker_popen_kwargs(),
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ProtocolError(
                "command_spawn_failed",
                "Could not start the declared executable "
                f"({type(exc).__name__}).",
            ) from exc
        stdout, stderr, stdout_truncated, stderr_truncated, timed_out = (
            _communicate_capped(proc, timeout=timeout)
        )
        bridge_cleanup_error: ProtocolError | None = None
        if egress_bridge is not None:
            (
                egress_audit_receipt,
                bridge_cleanup_error,
            ) = _seal_or_quarantine_one_shot_egress_bridge(
                egress_bridge
            )
            egress_bridge = None
        if bridge_cleanup_error is None:
            artifacts, deleted_count = _collect_workspace_artifacts(
                workspace,
                initial,
            )
        else:
            # A failed terminal seal makes post-execution filesystem output
            # untrusted. Preserve process evidence, but never publish artifacts
            # or pretend this was a pre-spawn validation failure.
            artifacts, deleted_count = [], 0
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        execution_status = (
            "timeout"
            if timed_out
            else ("success" if proc.returncode == 0 else "error")
        )
        status = (
            "error"
            if bridge_cleanup_error is not None
            else execution_status
        )
        response: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "declared_command_result",
            "request_id": request_id,
            "status": status,
            "returncode": proc.returncode,
            "executable": executable,
            "cwd": cwd_policy,
            "command_sha256": command_sha256,
            "shell": False,
            "network": "disabled",
            "runtime_profile": runtime_profile,
            "network_policy": {
                "direct": "disabled",
                "egress": (
                    "origin_allowlist_proxy"
                    if egress_origins
                    else "none"
                ),
            },
            "egress_policy_version": egress_policy_version,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "artifacts": artifacts,
            "deleted_workspace_files_ignored": deleted_count,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        if bridge_cleanup_error is not None:
            response.update(
                error_code=bridge_cleanup_error.code,
                error=str(bridge_cleanup_error),
                cleanup_phase="egress_bridge_seal",
                execution_completed=True,
                execution_status=execution_status,
                artifacts_discarded=True,
            )
        elif timed_out:
            response.update(
                error_code="command_timeout",
                error=f"Declared command timed out after {timeout}s.",
            )
        elif proc.returncode != 0:
            response.update(
                error_code="command_exit_nonzero",
                error=stderr_text or f"Declared command exited with return code {proc.returncode}.",
            )
        if egress_audit_receipt is not None:
            response["egress_audit_receipt"] = egress_audit_receipt
        return response
    except ProtocolError as exc:
        if egress_bridge is not None:
            (
                egress_audit_receipt,
                cleanup_error,
            ) = _seal_or_quarantine_one_shot_egress_bridge(
                egress_bridge
            )
            egress_bridge = None
            if cleanup_error is not None:
                exc = cleanup_error
        response = _declared_command_error(
            request_id,
            exc.code,
            str(exc),
            executable=executable,
            cwd=cwd_policy,
            command_sha256=command_sha256,
            egress_policy=egress_policy,
            duration_seconds=round(time.monotonic() - started, 3),
        )
        response["egress_policy_version"] = egress_policy_version
        if egress_audit_receipt is not None:
            response["egress_audit_receipt"] = egress_audit_receipt
        return response
    except Exception as exc:
        cleanup_error: ProtocolError | None = None
        if egress_bridge is not None:
            (
                egress_audit_receipt,
                cleanup_error,
            ) = _seal_or_quarantine_one_shot_egress_bridge(
                egress_bridge
            )
            egress_bridge = None
        response = _declared_command_error(
            request_id,
            (
                cleanup_error.code
                if cleanup_error is not None
                else "executor_internal_error"
            ),
            (
                str(cleanup_error)
                if cleanup_error is not None
                else "The declared-command executor failed safely "
                f"({type(exc).__name__})."
            ),
            executable=executable,
            cwd=cwd_policy,
            command_sha256=command_sha256,
            egress_policy=egress_policy,
            duration_seconds=round(time.monotonic() - started, 3),
        )
        response["egress_policy_version"] = egress_policy_version
        if egress_audit_receipt is not None:
            response["egress_audit_receipt"] = egress_audit_receipt
        return response
    finally:
        try:
            if egress_bridge is not None:
                _seal_or_quarantine_one_shot_egress_bridge(
                    egress_bridge
                )
        finally:
            if temp_dir is not None:
                _teardown_one_shot_temp_dir(temp_dir)


def _run_skill_script(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    request_id: str | None = None
    temp_dir: Path | None = None
    invocation_mode = "cli"
    function_name: str | None = None
    class_name: str | None = None
    method_name: str | None = None
    egress_bridge: _EgressBridgeHandle | None = None
    egress_origins: tuple[str, ...] = ()
    egress_rules: tuple[dict[str, Any], ...] = ()
    private_origins: tuple[str, ...] = ()
    egress_policy = "none"
    egress_policy_version = 2
    budget_scope_sha256: str | None = None
    call_id_sha256: str | None = None
    egress_audit_receipt: dict[str, Any] | None = None
    proc: subprocess.Popen[bytes] | None = None
    try:
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise ProtocolError("unsupported_protocol", f"protocol_version must be {PROTOCOL_VERSION}.")
        request_id = _validated_request_id(payload.get("request_id"))
        invocation_receipt = _skill_invocation_receipt_fields(payload)
        invocation_mode = str(
            invocation_receipt.get("invocation_mode") or "cli"
        )
        function_name = (
            invocation_receipt.get("function_name")
            if isinstance(invocation_receipt.get("function_name"), str)
            else None
        )
        class_name = (
            invocation_receipt.get("class_name")
            if isinstance(invocation_receipt.get("class_name"), str)
            else None
        )
        method_name = (
            invocation_receipt.get("method_name")
            if isinstance(invocation_receipt.get("method_name"), str)
            else None
        )
        entrypoint_relative = _safe_relative_path(payload.get("entrypoint"), field="entrypoint")
        argv = _validated_args(payload.get("argv", []))
        timeout = _validated_timeout(payload.get("timeout", 120))
        (
            egress_rules,
            egress_origins,
            private_origins,
        ) = _validated_exact_egress_policy(
            payload
        )
        (
            egress_policy_version,
            budget_scope_sha256,
            call_id_sha256,
        ) = _validated_egress_budget_binding(
            payload,
            has_rules=bool(egress_rules),
        )
        egress_policy = (
            "origin_allowlist_proxy" if egress_origins else "none"
        )
        cwd_policy = payload.get("cwd", "workspace")
        if cwd_policy not in {"workspace", "script", "skill"}:
            raise ProtocolError(
                "invalid_cwd", "cwd must be exactly 'workspace', 'script', or 'skill'."
            )

        skill_files = _decode_snapshot(
            payload.get("skill_files"),
            field="skill_files",
            max_files=MAX_SKILL_FILES,
            max_file_bytes=MAX_SKILL_FILE_BYTES,
            max_total_bytes=MAX_SKILL_TOTAL_BYTES,
        )
        if "SKILL.md" not in skill_files:
            raise ProtocolError("invalid_skill_snapshot", "The Skill snapshot must contain root SKILL.md.")
        if entrypoint_relative not in skill_files:
            raise ProtocolError("missing_entrypoint", "entrypoint is not present in the exact Skill snapshot.")
        raw_invocation = payload.get("invocation")
        if isinstance(raw_invocation, dict) and raw_invocation.get("mode") == "function":
            invocation_mode = "function"
            requested_name = raw_invocation.get("name")
            function_name = requested_name if isinstance(requested_name, str) else None
        elif isinstance(raw_invocation, dict) and raw_invocation.get("mode") == "instance_method":
            invocation_mode = "instance_method"
            requested_class = raw_invocation.get("class_name")
            requested_method = raw_invocation.get("method_name")
            class_name = requested_class if isinstance(requested_class, str) else None
            method_name = requested_method if isinstance(requested_method, str) else None
        (
            invocation_mode,
            function_name,
            class_name,
            method_name,
            function_request,
        ) = _validated_invocation(
            raw_invocation,
            entrypoint_relative=entrypoint_relative,
            entrypoint_bytes=skill_files[entrypoint_relative],
            argv=argv,
        )
        workspace_files = _decode_snapshot(
            payload.get("workspace_files", []),
            field="workspace_files",
            max_files=MAX_WORKSPACE_FILES,
            max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
            max_total_bytes=MAX_WORKSPACE_TOTAL_BYTES,
        )

        temp_dir = _make_execution_temp_dir("skill_exec_")
        skill_root = temp_dir / "skill"
        workspace = temp_dir / "workspace"
        runtime_root = temp_dir / "runtime"
        _materialize_snapshot(skill_root, skill_files, immutable=True)
        _materialize_snapshot(workspace, workspace_files, immutable=False)
        runtime_root.mkdir(mode=0o700)
        (runtime_root / "home").mkdir(mode=0o700)
        (runtime_root / "tmp").mkdir(mode=0o700)
        output_dir = workspace / "output_result"
        if output_dir.exists() and not output_dir.is_dir():
            raise ProtocolError("invalid_workspace_snapshot", "workspace/output_result must be a directory.")
        output_dir.mkdir(mode=0o700, exist_ok=True)

        initial = {
            relative: (len(content), hashlib.sha256(content).hexdigest())
            for relative, content in workspace_files.items()
        }
        entrypoint = skill_root.joinpath(*entrypoint_relative.split("/"))
        (
            runtime_profile,
            _profile_egress_policy,
            profile_environment,
        ) = _profile_process_environment()
        function_result_path: Path | None = None
        if invocation_mode in {"function", "instance_method"}:
            interpreter = "python"
            runner_path = runtime_root / "function_runner.py"
            request_path = runtime_root / "function_request.json"
            function_result_path = runtime_root / "function_result.json"
            with runner_path.open("xb") as stream:
                stream.write(FUNCTION_RUNNER_SOURCE.encode("utf-8"))
            with request_path.open("xb") as stream:
                stream.write(function_request or b"")
            os.chmod(runner_path, 0o400)
            os.chmod(request_path, 0o400)
            runner_arguments = [
                str(entrypoint),
                invocation_mode,
                str(function_name or class_name or ""),
                str(method_name or ""),
                str(request_path),
                str(function_result_path),
                str(MAX_FUNCTION_RESULT_CHARS),
                str(MAX_STDOUT_BYTES),
                str(MAX_STDERR_BYTES),
                str(MAX_FUNCTION_ENVELOPE_BYTES),
            ]
            command = (
                _browser_runtime_command(runner_path, runner_arguments)
                if runtime_profile in {
                    "browser-automation-v1",
                    SESSION_SANDBOX_RUNTIME_PROFILE,
                }
                else [
                    sys.executable,
                    "-I",
                    "-B",
                    str(runner_path),
                    *runner_arguments,
                ]
            )
        else:
            interpreter, command = _interpreter_command(
                entrypoint,
                skill_root=skill_root,
                runtime_root=runtime_root,
                runtime_profile=runtime_profile,
            )
        if cwd_policy == "workspace":
            workdir = workspace
        elif cwd_policy == "skill":
            workdir = skill_root
        else:
            workdir = entrypoint.parent
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(runtime_root / "home"),
            "TMPDIR": str(runtime_root / "tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "CHATDS_WORKSPACE": str(workspace),
            "CHATDS_SKILL_DIR": str(skill_root),
            "CHATDS_SKILL_ROOT": str(skill_root),
            "SKILL_DIR": str(skill_root),
            "CHATDS_OUTPUT_DIR": str(output_dir),
            **BLAS_THREAD_ENV,
        }
        env.update(profile_environment)
        _prepare_worker_tree(
            temp_dir,
            immutable_roots=(skill_root,),
            writable_roots=(workspace, runtime_root),
        )
        if egress_origins or runtime_profile in {
            "browser-automation-v1",
            SESSION_SANDBOX_RUNTIME_PROFILE,
        }:
            egress_bridge = _start_egress_bridge(
                egress_origins,
                egress_rules=egress_rules,
                private_origins=private_origins,
                policy_version=egress_policy_version,
                budget_scope_sha256=budget_scope_sha256,
                call_id_sha256=call_id_sha256,
                runtime_root=runtime_root,
            )
        if egress_bridge is not None:
            env.update(egress_bridge.environment)

        try:
            proc = subprocess.Popen(
                _resource_limited_command(
                    [*command, *argv] if invocation_mode == "cli" else command,
                    cpu_seconds=timeout + 5,
                    persistent=False,
                ),
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                **_native_worker_popen_kwargs(),
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ProtocolError(
                "interpreter_spawn_failed",
                f"Could not start the fixed interpreter ({type(exc).__name__}).",
            ) from exc

        stdout, stderr, stdout_truncated, stderr_truncated, timed_out = (
            _communicate_capped(proc, timeout=timeout)
        )
        bridge_cleanup_error: ProtocolError | None = None
        if egress_bridge is not None:
            (
                egress_audit_receipt,
                bridge_cleanup_error,
            ) = _seal_or_quarantine_one_shot_egress_bridge(
                egress_bridge
            )
            egress_bridge = None
        if bridge_cleanup_error is None:
            artifacts, deleted_count = _collect_workspace_artifacts(
                workspace,
                initial,
            )
        else:
            # Preserve the completed child evidence, but fail closed around
            # outputs when the terminal egress receipt cannot be sealed.
            artifacts, deleted_count = [], 0
        envelope: dict[str, Any] | None = None
        if (
            bridge_cleanup_error is None
            and invocation_mode in {"function", "instance_method"}
            and not timed_out
        ):
            if function_result_path is None or not function_result_path.is_file():
                raise ProtocolError(
                    "invalid_function_result",
                    "Function runner exited without a bounded result envelope.",
                )
            envelope = _read_function_envelope(function_result_path)
            if invocation_mode == "function" and envelope.get("function_name") != function_name:
                raise ProtocolError(
                    "invalid_function_result",
                    "Function result identity does not match the validated request.",
                )
            if invocation_mode == "instance_method" and (
                envelope.get("class_name") != class_name
                or envelope.get("method_name") != method_name
            ):
                raise ProtocolError(
                    "invalid_function_result",
                    "Instance-method result identity does not match the validated request.",
                )
        execution_status = "timeout" if timed_out else (
            str(envelope.get("status")) if envelope is not None
            else ("success" if proc.returncode == 0 else "error")
        )
        status = (
            "error"
            if bridge_cleanup_error is not None
            else execution_status
        )
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if envelope is not None:
            captured_stdout = envelope.pop("stdout", "")
            captured_stderr = envelope.pop("stderr", "")
            if isinstance(captured_stdout, str):
                combined = captured_stdout + ("\n" + stdout_text if stdout_text else "")
                stdout_truncated = stdout_truncated or bool(envelope.pop("stdout_truncated", False)) or len(combined) > MAX_STDOUT_BYTES
                stdout_text = combined[:MAX_STDOUT_BYTES]
            if isinstance(captured_stderr, str):
                combined = captured_stderr + ("\n" + stderr_text if stderr_text else "")
                stderr_truncated = stderr_truncated or bool(envelope.pop("stderr_truncated", False)) or len(combined) > MAX_STDERR_BYTES
                stderr_text = combined[:MAX_STDERR_BYTES]
        response: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "skill_script_result",
            "request_id": request_id,
            "status": status,
            "returncode": proc.returncode,
            "interpreter": interpreter,
            "interpreter_policy": "extension_allowlist",
            "invocation_mode": invocation_mode,
            "cwd": cwd_policy,
            "network": "disabled",
            "runtime_profile": runtime_profile,
            "network_policy": {
                "direct": "disabled",
                "egress": egress_policy,
            },
            "egress_policy_version": egress_policy_version,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "artifacts": artifacts,
            "deleted_workspace_files_ignored": deleted_count,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        if function_name is not None:
            response["function_name"] = function_name
        if class_name is not None and method_name is not None:
            response["class_name"] = class_name
            response["method_name"] = method_name
        if envelope is not None:
            response.update(envelope)
        if bridge_cleanup_error is not None:
            response.update(
                error_code=bridge_cleanup_error.code,
                error=str(bridge_cleanup_error),
                cleanup_phase="egress_bridge_seal",
                execution_completed=True,
                execution_status=execution_status,
                artifacts_discarded=True,
            )
        elif timed_out:
            response.update(
                error_code=(
                    "function_timeout"
                    if invocation_mode == "function"
                    else (
                        "instance_method_timeout"
                        if invocation_mode == "instance_method"
                        else "script_timeout"
                    )
                ),
                error=(
                    f"Skill function timed out after {timeout}s."
                    if invocation_mode == "function"
                    else (
                        f"Skill instance method timed out after {timeout}s."
                        if invocation_mode == "instance_method"
                        else f"Skill script timed out after {timeout}s."
                    )
                ),
                timeout_scope=(
                    "isolated_function"
                    if invocation_mode == "function"
                    else (
                        "isolated_instance_method"
                        if invocation_mode == "instance_method"
                        else "isolated_script"
                    )
                ),
            )
        elif invocation_mode == "function" and status == "error":
            response.setdefault("error_code", "function_exception")
            response.setdefault("error", "Skill function invocation failed.")
        elif invocation_mode == "instance_method" and status == "error":
            response.setdefault("error_code", "instance_method_exception")
            response.setdefault("error", "Skill instance-method invocation failed.")
        elif proc.returncode != 0:
            response.update(
                error_code="script_exit_nonzero",
                error=f"Skill script exited with return code {proc.returncode}.",
            )
        if egress_audit_receipt is not None:
            response["egress_audit_receipt"] = egress_audit_receipt
        return response
    except ProtocolError as exc:
        if egress_bridge is not None:
            (
                egress_audit_receipt,
                cleanup_error,
            ) = _seal_or_quarantine_one_shot_egress_bridge(
                egress_bridge
            )
            egress_bridge = None
            if cleanup_error is not None:
                exc = cleanup_error
        response = _skill_error(
            request_id,
            exc.code,
            str(exc),
            egress_policy=egress_policy,
            invocation_mode=invocation_mode,
            **({"function_name": function_name} if function_name else {}),
            **({"class_name": class_name} if class_name else {}),
            **({"method_name": method_name} if method_name else {}),
            duration_seconds=round(time.monotonic() - started, 3),
        )
        response["egress_policy_version"] = egress_policy_version
        if egress_audit_receipt is not None:
            response["egress_audit_receipt"] = egress_audit_receipt
        return response
    except Exception as exc:
        cleanup_error: ProtocolError | None = None
        if egress_bridge is not None:
            (
                egress_audit_receipt,
                cleanup_error,
            ) = _seal_or_quarantine_one_shot_egress_bridge(
                egress_bridge
            )
            egress_bridge = None
        response = _skill_error(
            request_id,
            (
                cleanup_error.code
                if cleanup_error is not None
                else "executor_internal_error"
            ),
            (
                str(cleanup_error)
                if cleanup_error is not None
                else "The isolated Skill executor failed safely "
                f"({type(exc).__name__})."
            ),
            egress_policy=egress_policy,
            invocation_mode=invocation_mode,
            **({"function_name": function_name} if function_name else {}),
            **({"class_name": class_name} if class_name else {}),
            **({"method_name": method_name} if method_name else {}),
            duration_seconds=round(time.monotonic() - started, 3),
        )
        response["egress_policy_version"] = egress_policy_version
        if egress_audit_receipt is not None:
            response["egress_audit_receipt"] = egress_audit_receipt
        return response
    finally:
        try:
            if egress_bridge is not None:
                _seal_or_quarantine_one_shot_egress_bridge(
                    egress_bridge
                )
        finally:
            if temp_dir is not None:
                _teardown_one_shot_temp_dir(temp_dir)


def _session_code_error(
    request_id: str | None,
    code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "session_code_result",
        "request_id": request_id,
        "status": "error",
        "error_code": code,
        "error": message,
        "network": "disabled",
        "artifacts": [],
    }
    response.update(extra)
    return response


def _run_session_code(payload: dict[str, Any]) -> dict[str, Any]:
    """Run model-authored Python against disposable session snapshots only."""

    started = time.monotonic()
    request_id: str | None = None
    temp_dir: Path | None = None
    try:
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise ProtocolError("unsupported_protocol", f"protocol_version must be {PROTOCOL_VERSION}.")
        request_id = _validated_request_id(payload.get("request_id"))
        code = payload.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ProtocolError("invalid_code", "code must be a non-empty string.")
        try:
            code_bytes = code.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ProtocolError("invalid_code", "code must be valid UTF-8.") from exc
        if len(code_bytes) > MAX_CODE_BYTES:
            raise ProtocolError(
                "code_limit_exceeded", f"code exceeds the {MAX_CODE_BYTES}-byte limit."
            )
        timeout = _validated_timeout(payload.get("timeout", 30))
        if timeout > MAX_CODE_TIMEOUT:
            raise ProtocolError(
                "invalid_timeout", f"session code timeout cannot exceed {MAX_CODE_TIMEOUT} seconds."
            )
        skill_files = _decode_snapshot(
            payload.get("skill_files", []),
            field="skill_files",
            max_files=MAX_SKILL_FILES,
            max_file_bytes=MAX_SKILL_FILE_BYTES,
            max_total_bytes=MAX_SKILL_TOTAL_BYTES,
        )
        workspace_files = _decode_snapshot(
            payload.get("workspace_files", []),
            field="workspace_files",
            max_files=MAX_WORKSPACE_FILES,
            max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
            max_total_bytes=MAX_WORKSPACE_TOTAL_BYTES,
        )
        result_files = _decode_snapshot(
            payload.get("result_files", []),
            field="result_files",
            max_files=MAX_WORKSPACE_FILES,
            max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
            max_total_bytes=MAX_WORKSPACE_TOTAL_BYTES,
        )

        temp_dir = _make_execution_temp_dir("session_code_")
        skills_root = temp_dir / "skills"
        results_root = temp_dir / "results"
        workspace = temp_dir / "workspace"
        runtime_root = temp_dir / "runtime"
        code_root = temp_dir / "code"
        _materialize_snapshot(skills_root, skill_files, immutable=True)
        _materialize_snapshot(results_root, result_files, immutable=True)
        _materialize_snapshot(workspace, workspace_files, immutable=False)
        runtime_root.mkdir(mode=0o700)
        (runtime_root / "home").mkdir(mode=0o700)
        (runtime_root / "tmp").mkdir(mode=0o700)
        code_root.mkdir(mode=0o700)
        script = code_root / "script.py"
        with script.open("xb") as stream:
            stream.write(code_bytes)
        os.chmod(script, 0o400)

        output_dir = workspace / "output_result"
        if output_dir.exists() and not output_dir.is_dir():
            raise ProtocolError("invalid_workspace_snapshot", "workspace/output_result must be a directory.")
        output_dir.mkdir(mode=0o700, exist_ok=True)
        initial = {
            relative: (len(content), hashlib.sha256(content).hexdigest())
            for relative, content in workspace_files.items()
        }
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(runtime_root / "home"),
            "TMPDIR": str(runtime_root / "tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "CHATDS_WORKSPACE": str(workspace),
            "CHATDS_SKILLS_ROOT": str(skills_root),
            "CHATDS_RESULTS_ROOT": str(results_root),
            "CHATDS_OUTPUT_DIR": str(output_dir),
            **BLAS_THREAD_ENV,
        }
        _prepare_worker_tree(
            temp_dir,
            immutable_roots=(skills_root, results_root),
            writable_roots=(workspace, runtime_root, code_root),
        )
        try:
            proc = subprocess.Popen(
                _resource_limited_command(
                    [sys.executable, "-I", "-B", str(script)],
                    cpu_seconds=timeout + 5,
                    persistent=False,
                ),
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                **_native_worker_popen_kwargs(),
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ProtocolError(
                "interpreter_spawn_failed",
                f"Could not start the fixed Python interpreter ({type(exc).__name__}).",
            ) from exc

        stdout, stderr, stdout_truncated, stderr_truncated, timed_out = (
            _communicate_capped(proc, timeout=timeout)
        )
        artifacts, deleted_count = _collect_workspace_artifacts(workspace, initial)
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        status = "timeout" if timed_out else ("success" if proc.returncode == 0 else "error")
        response: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "session_code_result",
            "request_id": request_id,
            "status": status,
            "returncode": proc.returncode,
            "interpreter": "python",
            "interpreter_policy": "fixed_python",
            "network": "disabled",
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "artifacts": artifacts,
            "deleted_workspace_files_ignored": deleted_count,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        if timed_out:
            response.update(
                error_code="script_timeout",
                error=f"Session code timed out after {timeout}s.",
            )
        elif proc.returncode != 0:
            response.update(
                error_code="script_exit_nonzero",
                error=stderr_text or f"Session code exited with return code {proc.returncode}.",
            )
        return response
    except ProtocolError as exc:
        return _session_code_error(
            request_id,
            exc.code,
            str(exc),
            duration_seconds=round(time.monotonic() - started, 3),
        )
    except Exception as exc:
        return _session_code_error(
            request_id,
            "executor_internal_error",
            f"The isolated session-code executor failed safely ({type(exc).__name__}).",
            duration_seconds=round(time.monotonic() - started, 3),
        )
    finally:
        if temp_dir is not None:
            _teardown_one_shot_temp_dir(temp_dir)


def _drain_process_stream(
    lease: _ProcessLease,
    stream_name: str,
    stream: Any,
) -> None:
    buffer = lease.stdout if stream_name == "stdout" else lease.stderr
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            while not lease.stopping:
                if lease.lock.acquire(timeout=0.1):
                    try:
                        buffer.append(chunk)
                        lease.condition.notify_all()
                    finally:
                        lease.lock.release()
                    break
            if lease.stopping:
                break
    except (OSError, ValueError):
        pass
    finally:
        if lease.stopping:
            # The stopper owns the lease lock and joins this thread before
            # releasing it. These simple flags are safe under the GIL and
            # avoid a lock/join cycle.
            buffer.eof = True
        else:
            with lease.condition:
                buffer.eof = True
                _refresh_process_state_locked(lease)
                lease.condition.notify_all()


def _refresh_process_state_locked(lease: _ProcessLease) -> None:
    process = lease.process
    if (
        lease.state == "running"
        and process is not None
        and process.poll() is not None
        and lease.stdout.eof
        and lease.stderr.eof
    ):
        lease.state = "exited"


def _signal_process_group(
    process: subprocess.Popen[bytes],
    selected_signal: int,
    *,
    process_group_id: int | None = None,
) -> bool:
    group_id = process.pid if process_group_id is None else process_group_id
    if group_id <= 0:
        return False
    try:
        os.killpg(group_id, selected_signal)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _seal_process_egress_bridge_locked(lease: _ProcessLease) -> None:
    """Seal a lease bridge exactly once, retaining it on retryable failure."""

    if lease.egress_bridge is None:
        return
    receipt = _seal_egress_bridge(lease.egress_bridge)
    lease.egress_audit_receipt = receipt
    lease.egress_bridge = None


def _stop_process_lease_locked(lease: _ProcessLease) -> None:
    process = lease.process
    if process is None:
        _seal_process_egress_bridge_locked(lease)
        return
    lease.stopping = True
    if process.stdin is not None:
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
    lease.stdin_closed = True
    if process.poll() is None:
        _signal_process_group(
            process,
            signal.SIGTERM,
            process_group_id=lease.process_group_id,
        )
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            _signal_process_group(
                process,
                signal.SIGKILL,
                process_group_id=lease.process_group_id,
            )
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _kill_process_group(process)
    # The leader may already have exited while descendants inherited its
    # process group. Always kill the original group ID before releasing the
    # lease; relying on process.poll() would leak those descendants.
    try:
        os.killpg(lease.process_group_id or process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    for thread in lease.reader_threads:
        thread.join(timeout=1)
    _refresh_process_state_locked(lease)
    _seal_process_egress_bridge_locked(lease)


def _remove_process_tree(root: Path) -> bool:
    """Remove an owned snapshot after restoring only real directory modes."""

    if not root.exists():
        return True
    try:
        for directory, _, _ in os.walk(root, topdown=True, followlinks=False):
            try:
                os.chmod(directory, 0o700, follow_symlinks=False)
            except (NotImplementedError, OSError):
                pass
        shutil.rmtree(root)
    except OSError:
        # Cleanup is retried by the janitor/tombstone path. Never follow a
        # Skill-created link merely to make deletion succeed.
        return not root.exists()
    return not root.exists()


def _discard_pending_process_sync_locked(lease: _ProcessLease) -> None:
    """Abort one prepared batch and invalidate its now-stale replay receipt."""

    prepare_op_id = lease.pending_sync_prepare_op_id
    if prepare_op_id is not None:
        cached = lease.operations.pop(prepare_op_id, None)
        if cached is not None:
            _, cached_response = cached
            encoded = json.dumps(
                cached_response,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            lease.operation_cache_bytes = max(
                0,
                lease.operation_cache_bytes - len(encoded),
            )
    lease.pending_sync_token = None
    lease.pending_sync_state = None
    lease.pending_sync_close = False
    lease.pending_sync_ack_deadline = None
    lease.pending_sync_prepare_op_id = None


def _quarantine_process_cleanup_locked(
    lease: _ProcessLease,
    *,
    error_code: str,
    retry_reason: str,
) -> None:
    """Retain the lease and admission until every cleanup proof succeeds."""

    lease.state = "quarantined"
    lease.close_reason = error_code
    lease.pending_expiry_reason = retry_reason
    lease.closed_at = None
    lease.condition.notify_all()


def _expire_process_lease_locked(
    lease: _ProcessLease,
    *,
    now: float,
    reason: str,
) -> None:
    if lease.state in {"closed", "expired"}:
        return
    if lease.pending_sync_token is not None:
        # Once the bounded ACK grace expires, the prepared filesystem
        # transaction no longer exists.  Its exact-op replay must therefore
        # not return an otherwise valid-looking stale token/artifact batch.
        _discard_pending_process_sync_locked(lease)
    try:
        _stop_process_lease_locked(lease)
        _sweep_configured_worker_uid()
        if not _remove_process_tree(lease.temp_dir):
            raise ProtocolError(
                "worker_containment_failed",
                "Expired worker tree could not be removed safely.",
            )
        _release_process_admission(lease.handle)
    except ProtocolError as exc:
        # Keep the unique worker slot permanently reserved if containment
        # cannot be proven. This is safer than overlapping another same-UID
        # execution; shutdown/operator intervention can then recover it.
        _quarantine_process_cleanup_locked(
            lease,
            error_code=exc.code,
            retry_reason=reason,
        )
    else:
        lease.state = "expired"
        lease.close_reason = reason
        lease.pending_expiry_reason = None
        lease.closed_at = now
    lease.condition.notify_all()


def _retry_quarantined_process_lease_locked(
    lease: _ProcessLease,
    *,
    now: float,
) -> bool:
    if lease.state != "quarantined":
        return False
    try:
        _stop_process_lease_locked(lease)
        _sweep_configured_worker_uid()
        if not _remove_process_tree(lease.temp_dir):
            raise ProtocolError(
                "worker_containment_failed",
                "Quarantined worker tree could not be removed safely.",
            )
        _release_process_admission(lease.handle)
    except ProtocolError as exc:
        lease.close_reason = exc.code
        lease.closed_at = None
        lease.condition.notify_all()
        return False
    lease.state = "expired"
    lease.close_reason = lease.pending_expiry_reason or "worker_containment_recovered"
    lease.pending_expiry_reason = None
    lease.closed_at = now
    lease.condition.notify_all()
    return True


def _cleanup_expired_process_leases(*, now: float | None = None) -> int:
    current = time.monotonic() if now is None else now
    cleaned = 0
    with _PROCESS_LEASES_LOCK:
        for handle, lease in list(_PROCESS_LEASES.items()):
            with lease.lock:
                if lease.state == "quarantined":
                    if _retry_quarantined_process_lease_locked(
                        lease,
                        now=current,
                    ):
                        cleaned += 1
                    else:
                        continue
                if lease.state not in {"closed", "expired"}:
                    if (
                        lease.pending_sync_token is not None
                        and lease.pending_sync_ack_deadline is not None
                        and current < lease.pending_sync_ack_deadline
                    ):
                        # A local artifact apply may legitimately outlive the
                        # process idle/max-runtime deadline.  The immutable
                        # prepared batch remains recoverable until its own
                        # bounded ACK grace expires.
                        continue
                    if lease.pending_sync_token is not None:
                        _expire_process_lease_locked(
                            lease,
                            now=current,
                            reason=(
                                "close_ack_timeout"
                                if lease.pending_sync_close
                                else "sync_ack_timeout"
                            ),
                        )
                        if lease.state == "expired":
                            cleaned += 1
                    elif current >= lease.absolute_expires_at:
                        _expire_process_lease_locked(
                            lease,
                            now=current,
                            reason="max_runtime_expired",
                        )
                        if lease.state == "expired":
                            cleaned += 1
                    elif current >= lease.idle_expires_at:
                        _expire_process_lease_locked(
                            lease,
                            now=current,
                            reason="idle_ttl_expired",
                        )
                        if lease.state == "expired":
                            cleaned += 1
                if (
                    lease.state in {"closed", "expired"}
                    and lease.closed_at is not None
                ):
                    if lease.temp_dir.exists():
                        _remove_process_tree(lease.temp_dir)
                    if (
                        not lease.temp_dir.exists()
                        and current - lease.closed_at >= PROCESS_TOMBSTONE_SECONDS
                    ):
                        _PROCESS_LEASES.pop(handle, None)
                        _PROCESS_OPEN_OPERATIONS.pop(
                            (lease.scope_digest, lease.open_op_id),
                            None,
                        )
    return cleaned


def _reap_process_lease_locked(
    lease: _ProcessLease,
    *,
    now: float,
    reason: str,
) -> bool:
    """Best-effort one lease without releasing admission on any failed proof."""

    if lease.pending_sync_token is not None:
        _discard_pending_process_sync_locked(lease)
    try:
        _stop_process_lease_locked(lease)
        _sweep_configured_worker_uid()
        if not _remove_process_tree(lease.temp_dir):
            raise ProtocolError(
                "worker_containment_failed",
                "Worker tree could not be removed safely during reap.",
            )
        _release_process_admission(lease.handle)
    except ProtocolError as exc:
        _quarantine_process_cleanup_locked(
            lease,
            error_code=exc.code,
            retry_reason=reason,
        )
        return False
    lease.state = "closed"
    lease.close_reason = reason
    lease.pending_expiry_reason = None
    lease.closed_at = now
    lease.condition.notify_all()
    return True


def _shutdown_all_process_leases() -> None:
    """Best-effort daemon/test cleanup; no process survives server shutdown."""

    global _ACTIVE_V1_EXECUTION
    global _ACTIVE_V1_EXECUTION_QUARANTINED, _ACTIVE_V1_TEMP_DIR
    with _PROCESS_LEASES_LOCK:
        for handle, lease in list(_PROCESS_LEASES.items()):
            with lease.lock:
                if not _reap_process_lease_locked(
                    lease,
                    now=time.monotonic(),
                    reason="executor_shutdown",
                ):
                    # Keep the exact bridge, lease, and admission reachable so
                    # a later shutdown/janitor/controller pass can retry.
                    continue
                _PROCESS_LEASES.pop(handle, None)
                _PROCESS_OPEN_OPERATIONS.pop(
                    (lease.scope_digest, lease.open_op_id),
                    None,
                )
    _retry_orphaned_v1_egress_bridges()
    with _EXECUTION_ADMISSION_LOCK:
        v1_temp_dir = _ACTIVE_V1_TEMP_DIR
    v1_tree_removed = (
        v1_temp_dir is None or _remove_process_tree(v1_temp_dir)
    )
    with _EXECUTION_ADMISSION_LOCK:
        if (
            v1_tree_removed
            and not _ORPHANED_V1_EGRESS_BRIDGES
        ):
            _ACTIVE_V1_EXECUTION = False
            _ACTIVE_V1_EXECUTION_QUARANTINED = False
            _ACTIVE_V1_TEMP_DIR = None
        else:
            _ACTIVE_V1_EXECUTION = True
            _ACTIVE_V1_EXECUTION_QUARANTINED = True


def _controller_reap_process_leases() -> int:
    """Authenticated startup cleanup for a replacement Harness process.

    Process capabilities intentionally live only in Harness memory. If that
    process crashes, a new Harness instance must be able to prove that the
    fixed worker UID is empty before accepting traffic instead of waiting for
    an orphan lease's idle timeout.
    """

    global _ACTIVE_PROCESS_LEASE_HANDLE, _ACTIVE_V1_EXECUTION
    global _ACTIVE_V1_EXECUTION_QUARANTINED, _ACTIVE_V1_TEMP_DIR
    with _EXECUTION_ADMISSION_LOCK:
        if (
            _ACTIVE_V1_EXECUTION
            and not _ACTIVE_V1_EXECUTION_QUARANTINED
        ):
            raise ProtocolError(
                "worker_busy",
                "A one-shot worker is active; controller startup reap was refused.",
            )
        if (
            not _ACTIVE_V1_EXECUTION
            and (
                _ACTIVE_V1_EXECUTION_QUARANTINED
                or _ACTIVE_V1_TEMP_DIR is not None
            )
        ):
            raise ProtocolError(
                "worker_containment_failed",
                "One-shot executor quarantine state is inconsistent.",
            )
        reaping_v1 = bool(
            _ACTIVE_V1_EXECUTION
            and _ACTIVE_V1_EXECUTION_QUARANTINED
        )
        v1_temp_dir = _ACTIVE_V1_TEMP_DIR
        orphaned_bridge_count = len(
            _ORPHANED_V1_EGRESS_BRIDGES
        )
        active_handle = _ACTIVE_PROCESS_LEASE_HANDLE
        if (
            orphaned_bridge_count
            and not reaping_v1
        ):
            raise ProtocolError(
                "worker_containment_failed",
                "Orphaned one-shot egress bridges are not bound to a "
                "quarantined admission.",
            )
    reaped_leases = 0
    failures: list[ProtocolError] = []
    with _PROCESS_LEASES_LOCK:
        leases = list(_PROCESS_LEASES.items())
        known_handles = {lease.handle for _, lease in leases}
        if (
            active_handle is not None
            and active_handle not in known_handles
        ):
            raise ProtocolError(
                "worker_busy",
                "A lease-open reservation is active; controller startup reap was refused.",
            )
        for handle, lease in leases:
            with lease.lock:
                if not _reap_process_lease_locked(
                    lease,
                    now=time.monotonic(),
                    reason="controller_startup_reap",
                ):
                    failures.append(ProtocolError(
                        lease.close_reason
                        or "worker_containment_failed",
                        "Controller startup reap could not seal and contain "
                        "one process lease.",
                    ))
                    continue
                _PROCESS_LEASES.pop(handle, None)
                _PROCESS_OPEN_OPERATIONS.pop(
                    (lease.scope_digest, lease.open_op_id),
                    None,
                )
                reaped_leases += 1
    _, bridge_failures = _retry_orphaned_v1_egress_bridges()
    failures.extend(bridge_failures)
    if v1_temp_dir is not None and not _remove_process_tree(v1_temp_dir):
        failures.append(ProtocolError(
            "worker_containment_failed",
            "Controller reap could not remove a quarantined one-shot tree.",
        ))
    elif reaping_v1 and not bridge_failures:
        with _EXECUTION_ADMISSION_LOCK:
            if not _ORPHANED_V1_EGRESS_BRIDGES:
                _ACTIVE_V1_EXECUTION = False
                _ACTIVE_V1_EXECUTION_QUARANTINED = False
                _ACTIVE_V1_TEMP_DIR = None
    if failures:
        first = failures[0]
        raise ProtocolError(
            first.code,
            "Controller startup reap completed all independent cleanup but "
            f"{len(failures)} quarantined worker allocation(s) remain.",
        )
    # A second bounded sweep after releasing all tree references proves that
    # no child appeared during cleanup.
    _sweep_configured_worker_uid()
    _purge_configured_worker_shared_state()
    return reaped_leases + int(reaping_v1)


def _process_lease_janitor(stop_event: threading.Event) -> None:
    while not stop_event.wait(PROCESS_JANITOR_INTERVAL_SECONDS):
        try:
            _cleanup_expired_process_leases()
        except Exception:
            # One unexpected cleanup defect must not permanently kill expiry
            # enforcement. Lease-specific expected failures are quarantined by
            # the cleanup path and retried on the next pass.
            continue


def _cached_process_response(
    lease: _ProcessLease,
    *,
    op_id: str,
    fingerprint: str,
    request_id: str,
) -> dict[str, Any] | None:
    cached = lease.operations.get(op_id)
    if cached is None:
        return None
    cached_fingerprint, cached_response = cached
    if not hmac.compare_digest(cached_fingerprint, fingerprint):
        raise ProtocolError(
            "op_id_conflict",
            "op_id was already used for a different process operation.",
        )
    response = json.loads(json.dumps(cached_response))
    response["request_id"] = request_id
    response["idempotent_replay"] = True
    return response


def _cache_process_response(
    lease: _ProcessLease,
    *,
    op_id: str,
    fingerprint: str,
    response: dict[str, Any],
) -> None:
    if len(lease.operations) >= MAX_PROCESS_OPERATIONS:
        raise ProtocolError(
            "operation_limit_exceeded",
            "Process lease exhausted its bounded operation budget.",
        )
    encoded = json.dumps(
        response,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if lease.operation_cache_bytes + len(encoded) > MAX_PROCESS_OPERATION_CACHE_BYTES:
        raise ProtocolError(
            "operation_cache_limit_exceeded",
            "Process lease exhausted its bounded idempotency-cache byte budget.",
        )
    lease.operations[op_id] = (
        fingerprint,
        json.loads(encoded),
    )
    lease.operation_cache_bytes += len(encoded)


def _process_cache_reservation(payload: dict[str, Any], operation: str) -> int:
    if operation in {"sync", "close"}:
        return MAX_PROCESS_CLOSE_CACHE_RESERVE_BYTES
    if operation == "read":
        requested = payload.get("max_bytes", MAX_PROCESS_READ_BYTES)
        if isinstance(requested, bool) or not isinstance(requested, int):
            requested = MAX_PROCESS_READ_BYTES
        bounded = max(1, min(requested, MAX_PROCESS_READ_BYTES))
        return ((bounded * 2 + 2) // 3) * 4 + 128 * 1024
    return 64 * 1024


def _validated_process_binding(
    payload: dict[str, Any],
    lease: _ProcessLease,
) -> None:
    scope_digest = _validated_process_owner_scope(payload.get("owner_scope"))
    skill_sha256 = _validated_sha256(
        payload.get("skill_sha256"),
        field_name="skill_sha256",
    )
    script_sha256 = _validated_sha256(
        payload.get("script_sha256"),
        field_name="script_sha256",
    )
    if not (
        hmac.compare_digest(scope_digest, lease.scope_digest)
        and hmac.compare_digest(skill_sha256, lease.skill_sha256)
        and hmac.compare_digest(script_sha256, lease.script_sha256)
    ):
        raise ProtocolError(
            "lease_scope_mismatch",
            "Lease owner scope or content authority does not match the opened lease.",
        )


def _validated_persistent_invocation(
    value: Any,
    *,
    entrypoint_relative: str,
    entrypoint_bytes: bytes,
    argv: list[str],
) -> tuple[str, str | None, str | None, bytes | None]:
    if value is None or value == {"mode": "cli"}:
        return "cli", None, None, None
    instance_fields = {
        "mode",
        "class_name",
        "constructor_args",
        "constructor_kwargs",
    }
    factory_fields = {
        "mode",
        "factory_name",
        "factory_args",
        "factory_kwargs",
    }
    if (
        not isinstance(value, dict)
        or (
            not (set(value) == instance_fields and value.get("mode") == "instance")
            and not (set(value) == factory_fields and value.get("mode") == "factory")
        )
    ):
        raise ProtocolError(
            "invalid_invocation",
            "Persistent invocation must be exact CLI, public-class, or public-factory JSON.",
        )
    if PurePosixPath(entrypoint_relative).suffix != ".py" or argv:
        raise ProtocolError(
            "invalid_function_call",
            "Persistent object invocation requires a Python entrypoint and no CLI argv.",
        )
    invocation_mode = str(value.get("mode"))
    identity_field = "class_name" if invocation_mode == "instance" else "factory_name"
    selected_name = value.get(identity_field)
    if (
        not isinstance(selected_name, str)
        or selected_name.startswith("_")
        or PUBLIC_FUNCTION_RE.fullmatch(selected_name) is None
    ):
        raise ProtocolError(
            "invalid_function_call",
            f"{identity_field} must be one public, non-dotted Python identifier.",
        )
    positional_field = "constructor_args" if invocation_mode == "instance" else "factory_args"
    keywords_field = "constructor_kwargs" if invocation_mode == "instance" else "factory_kwargs"
    positional = value.get(positional_field)
    keywords = value.get(keywords_field)
    if not isinstance(positional, list) or len(positional) > MAX_FUNCTION_ARGS:
        raise ProtocolError(
            "invalid_function_call",
            f"{positional_field} must contain at most {MAX_FUNCTION_ARGS} JSON values.",
        )
    if not isinstance(keywords, dict) or len(keywords) > MAX_FUNCTION_KWARGS:
        raise ProtocolError(
            "invalid_function_call",
            f"{keywords_field} must contain at most {MAX_FUNCTION_KWARGS} JSON values.",
        )
    for key in keywords:
        if (
            not isinstance(key, str)
            or key.startswith("_")
            or PUBLIC_FUNCTION_RE.fullmatch(key) is None
        ):
            raise ProtocolError(
                "invalid_function_call",
                f"{keywords_field} keys must be public Python identifiers.",
            )
    _validate_json_value(positional, field=positional_field)
    _validate_json_value(keywords, field=keywords_field)
    try:
        source = entrypoint_bytes.decode("utf-8")
        tree = ast.parse(source, filename=entrypoint_relative)
    except (UnicodeError, SyntaxError) as exc:
        raise ProtocolError(
            "invalid_function_call",
            "Persistent instance entrypoint must be valid UTF-8 Python source.",
        ) from exc
    if invocation_mode == "instance":
        matching_declarations = [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == selected_name
            and not node.decorator_list
        ]
    else:
        matching_declarations = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == selected_name
            and not node.decorator_list
        ]
    if len(matching_declarations) != 1:
        raise ProtocolError(
            "invalid_function_call",
            f"Public top-level {invocation_mode} {selected_name!r} is not uniquely declared by the selected entrypoint.",
        )
    try:
        constructor_request = json.dumps(
            {"args": positional, "kwargs": keywords},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolError(
            "invalid_function_call",
            "Persistent object arguments must be finite valid JSON.",
        ) from exc
    if len(constructor_request) > MAX_FUNCTION_INPUT_BYTES:
        raise ProtocolError(
            "invalid_function_call",
            "Persistent object argument JSON exceeds its byte limit.",
        )
    return (
        invocation_mode,
        selected_name if invocation_mode == "instance" else None,
        selected_name if invocation_mode == "factory" else None,
        constructor_request,
    )


def _validated_persistent_method_call(
    payload: dict[str, Any],
    lease: _ProcessLease,
    *,
    call_id: str,
) -> bytes:
    if lease.invocation_mode not in {"instance", "factory"}:
        raise ProtocolError(
            "invalid_invocation",
            "Structured method calls require a persistent public-object lease.",
        )
    method_name = payload.get("method_name")
    if (
        not isinstance(method_name, str)
        or method_name.startswith("_")
        or PUBLIC_FUNCTION_RE.fullmatch(method_name) is None
    ):
        raise ProtocolError(
            "invalid_function_call",
            "method_name must be one public, non-dotted Python identifier.",
        )
    positional = payload.get("method_args", [])
    keywords = payload.get("method_kwargs", {})
    if not isinstance(positional, list) or len(positional) > MAX_FUNCTION_ARGS:
        raise ProtocolError(
            "invalid_function_call",
            f"method_args must contain at most {MAX_FUNCTION_ARGS} JSON values.",
        )
    if not isinstance(keywords, dict) or len(keywords) > MAX_FUNCTION_KWARGS:
        raise ProtocolError(
            "invalid_function_call",
            f"method_kwargs must contain at most {MAX_FUNCTION_KWARGS} JSON values.",
        )
    for key in keywords:
        if (
            not isinstance(key, str)
            or key.startswith("_")
            or PUBLIC_FUNCTION_RE.fullmatch(key) is None
        ):
            raise ProtocolError(
                "invalid_function_call",
                "method_kwargs keys must be public Python identifiers.",
            )
    _validate_json_value(positional, field="method_args")
    _validate_json_value(keywords, field="method_kwargs")
    try:
        source = lease.entrypoint_bytes.decode("utf-8")
        tree = ast.parse(source, filename=lease.entrypoint_relative)
    except (UnicodeError, SyntaxError) as exc:
        raise ProtocolError(
            "invalid_function_call",
            "Persistent instance entrypoint is no longer valid Python source.",
        ) from exc
    if lease.invocation_mode == "instance":
        matching_classes = [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == lease.class_name
            and not node.decorator_list
        ]
        if len(matching_classes) != 1:
            raise ProtocolError(
                "invalid_function_call",
                "Persistent instance class identity is not uniquely declared.",
            )
    else:
        matching_factories = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == lease.factory_name
            and not node.decorator_list
        ]
        if len(matching_factories) != 1:
            raise ProtocolError(
                "invalid_function_call",
                "Persistent factory identity is not uniquely declared.",
            )
        matching_classes = [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and not node.decorator_list
            and any(
                isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                and method.name == method_name
                and not method.decorator_list
                and bool([*method.args.posonlyargs, *method.args.args])
                for method in node.body
            )
        ]
        if len(matching_classes) != 1:
            raise ProtocolError(
                "invalid_function_call",
                "A factory method must map to one unique public top-level class declaration.",
            )
    selected_class = matching_classes[0]
    matching_methods = [
        node
        for node in selected_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
        and not node.decorator_list
        and bool([*node.args.posonlyargs, *node.args.args])
    ]
    if len(matching_methods) != 1:
        raise ProtocolError(
            "invalid_function_call",
            f"Public plain method {selected_class.name}.{method_name} is not uniquely declared.",
        )
    try:
        encoded = json.dumps(
            {
                "call_id": call_id,
                "method": method_name,
                "args": positional,
                "kwargs": keywords,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolError(
            "invalid_function_call",
            "Method arguments must be finite valid JSON.",
        ) from exc
    if len(encoded) > MAX_PROCESS_CALL_BYTES:
        raise ProtocolError(
            "invalid_function_call",
            "Method call JSON exceeds the atomic process-call byte limit.",
        )
    return encoded


def _open_process_lease(
    payload: dict[str, Any],
    *,
    request_id: str,
    op_id: str,
    scope_digest: str,
    fingerprint: str,
) -> dict[str, Any]:
    temp_dir: Path | None = None
    admission_reservation: str | None = None
    admission_handle: str | None = None
    lease_published = False
    egress_bridge: _EgressBridgeHandle | None = None
    egress_policy_version = 2
    budget_scope_sha256: str | None = None
    call_id_sha256: str | None = None
    with _PROCESS_LEASES_LOCK:
        previous = _PROCESS_OPEN_OPERATIONS.get((scope_digest, op_id))
        if previous is not None:
            previous_fingerprint, previous_handle = previous
            if not hmac.compare_digest(previous_fingerprint, fingerprint):
                raise ProtocolError(
                    "op_id_conflict",
                    "op_id was already used for a different lease-open operation.",
                )
            previous_lease = _PROCESS_LEASES.get(previous_handle)
            if previous_lease is None:
                raise ProtocolError(
                    "lease_lost",
                    "The idempotent lease-open result is no longer retained.",
                )
            return _process_success(
                request_id,
                "open",
                previous_lease,
                idempotent_replay=True,
                entrypoint=previous_lease.entrypoint_relative,
                interpreter=previous_lease.interpreter,
                idle_ttl_seconds=previous_lease.idle_ttl_seconds,
                max_runtime_seconds=previous_lease.max_runtime_seconds,
                invocation_mode=previous_lease.invocation_mode,
                **(
                    {"class_name": previous_lease.class_name}
                    if previous_lease.class_name is not None
                    else {}
                ),
                **(
                    {"factory_name": previous_lease.factory_name}
                    if previous_lease.factory_name is not None
                    else {}
                ),
            )

        active = [
            lease
            for lease in _PROCESS_LEASES.values()
            if lease.state not in {"closed", "expired"}
        ]
        max_leases = _trusted_process_limit(
            "EXECUTOR_MAX_PROCESS_LEASES",
            default=MAX_PROCESS_LEASES,
            minimum=1,
            maximum=MAX_PROCESS_LEASES,
        )
        max_per_scope = _trusted_process_limit(
            "EXECUTOR_MAX_PROCESS_LEASES_PER_SCOPE",
            default=MAX_PROCESS_LEASES_PER_SCOPE,
            minimum=1,
            maximum=MAX_PROCESS_LEASES_PER_SCOPE,
        )
        if len(active) >= max_leases:
            raise ProtocolError(
                "lease_quota_exceeded",
                "Executor has reached its bounded active process-lease quota.",
            )
        if sum(lease.scope_digest == scope_digest for lease in active) >= max_per_scope:
            raise ProtocolError(
                "lease_quota_exceeded",
                "Owner scope has reached its bounded active process-lease quota.",
            )

        entrypoint_relative = _safe_relative_path(
            payload.get("entrypoint"),
            field="entrypoint",
        )
        argv = _validated_args(payload.get("argv", []))
        (
            egress_rules,
            egress_origins,
            private_origins,
        ) = _validated_exact_egress_policy(
            payload
        )
        (
            egress_policy_version,
            budget_scope_sha256,
            call_id_sha256,
        ) = _validated_egress_budget_binding(
            payload,
            has_rules=bool(egress_rules),
        )
        cwd_policy = payload.get("cwd", "workspace")
        if cwd_policy not in {"workspace", "script", "skill"}:
            raise ProtocolError(
                "invalid_cwd",
                "cwd must be exactly 'workspace', 'script', or 'skill'.",
            )
        idle_ttl_seconds = _validated_process_seconds(
            payload.get("idle_ttl_seconds", 300),
            field_name="idle_ttl_seconds",
            minimum=MIN_PROCESS_LEASE_TTL_SECONDS,
            maximum=MAX_PROCESS_LEASE_TTL_SECONDS,
        )
        max_runtime_seconds = _validated_process_seconds(
            payload.get("max_runtime_seconds", MAX_PROCESS_RUNTIME_SECONDS),
            field_name="max_runtime_seconds",
            minimum=MIN_PROCESS_LEASE_TTL_SECONDS,
            maximum=MAX_PROCESS_RUNTIME_SECONDS,
        )
        skill_files = _decode_snapshot(
            payload.get("skill_files"),
            field="skill_files",
            max_files=MAX_SKILL_FILES,
            max_file_bytes=MAX_SKILL_FILE_BYTES,
            max_total_bytes=MAX_SKILL_TOTAL_BYTES,
        )
        if "SKILL.md" not in skill_files:
            raise ProtocolError(
                "invalid_skill_snapshot",
                "The Skill snapshot must contain root SKILL.md.",
            )
        if entrypoint_relative not in skill_files:
            raise ProtocolError(
                "missing_entrypoint",
                "entrypoint is not present in the exact Skill snapshot.",
            )
        workspace_files = _decode_snapshot(
            payload.get("workspace_files", []),
            field="workspace_files",
            max_files=MAX_WORKSPACE_FILES,
            max_file_bytes=MAX_WORKSPACE_FILE_BYTES,
            max_total_bytes=MAX_WORKSPACE_TOTAL_BYTES,
        )
        skill_sha256 = _canonical_snapshot_digest(skill_files)
        script_sha256 = hashlib.sha256(skill_files[entrypoint_relative]).hexdigest()
        expected_skill_sha256 = _validated_sha256(
            payload.get("skill_sha256"),
            field_name="skill_sha256",
        )
        expected_script_sha256 = _validated_sha256(
            payload.get("script_sha256"),
            field_name="script_sha256",
        )
        if not (
            hmac.compare_digest(skill_sha256, expected_skill_sha256)
            and hmac.compare_digest(script_sha256, expected_script_sha256)
        ):
            raise ProtocolError(
                "authority_digest_mismatch",
                "Lease content does not match the authorized Skill/script digests.",
            )
        (
            invocation_mode,
            class_name,
            factory_name,
            constructor_request,
        ) = _validated_persistent_invocation(
            payload.get("invocation"),
            entrypoint_relative=entrypoint_relative,
            entrypoint_bytes=skill_files[entrypoint_relative],
            argv=argv,
        )
        runtime_profile, egress_policy, profile_environment = _profile_process_environment()
        admission_reservation = f"opening:{op_id}"
        _reserve_process_admission(admission_reservation)

        try:
            temp_dir = _make_execution_temp_dir("skill_process_")
            skill_root = temp_dir / "skill"
            workspace = temp_dir / "workspace"
            runtime_root = temp_dir / "runtime"
            _materialize_snapshot(skill_root, skill_files, immutable=True)
            _materialize_snapshot(workspace, workspace_files, immutable=False)
            runtime_root.mkdir(mode=0o700)
            (runtime_root / "home").mkdir(mode=0o700)
            (runtime_root / "tmp").mkdir(mode=0o700)
            output_dir = workspace / "output_result"
            if output_dir.exists() and not output_dir.is_dir():
                raise ProtocolError(
                    "invalid_workspace_snapshot",
                    "workspace/output_result must be a directory.",
                )
            output_dir.mkdir(mode=0o700, exist_ok=True)
            entrypoint = skill_root.joinpath(*entrypoint_relative.split("/"))
            if invocation_mode in {"instance", "factory"}:
                interpreter = "python"
                runner_path = runtime_root / "persistent_instance_runner.py"
                constructor_path = runtime_root / "constructor.json"
                with runner_path.open("xb") as stream:
                    stream.write(PERSISTENT_INSTANCE_RUNNER_SOURCE.encode("utf-8"))
                with constructor_path.open("xb") as stream:
                    stream.write(constructor_request or b"")
                os.chmod(runner_path, 0o400)
                os.chmod(constructor_path, 0o400)
                runner_arguments = [
                    str(skill_root),
                    str(entrypoint),
                    str(constructor_path),
                    invocation_mode,
                    str(class_name or factory_name or ""),
                    str(MAX_FUNCTION_INPUT_BYTES),
                    str(MAX_FUNCTION_RESULT_CHARS),
                    str(max(MAX_STDOUT_BYTES, MAX_STDERR_BYTES)),
                ]
                command = (
                    _browser_runtime_command(runner_path, runner_arguments)
                    if runtime_profile in {
                        "browser-automation-v1",
                        SESSION_SANDBOX_RUNTIME_PROFILE,
                    }
                    else [
                        sys.executable,
                        "-I",
                        "-B",
                        str(runner_path),
                        *runner_arguments,
                    ]
                )
            else:
                interpreter, command = _interpreter_command(
                    entrypoint,
                    skill_root=skill_root,
                    runtime_root=runtime_root,
                    runtime_profile=runtime_profile,
                )
            workdir = (
                workspace
                if cwd_policy == "workspace"
                else skill_root
                if cwd_policy == "skill"
                else entrypoint.parent
            )
            environment = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(runtime_root / "home"),
                "TMPDIR": str(runtime_root / "tmp"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "CHATDS_WORKSPACE": str(workspace),
                "CHATDS_SKILL_DIR": str(skill_root),
                "CHATDS_SKILL_ROOT": str(skill_root),
                "SKILL_DIR": str(skill_root),
                "CHATDS_OUTPUT_DIR": str(output_dir),
                **BLAS_THREAD_ENV,
            }
            environment.update(profile_environment)
            _prepare_worker_tree(
                temp_dir,
                immutable_roots=(skill_root,),
                writable_roots=(workspace, runtime_root),
            )
            if egress_origins or runtime_profile in {
                "browser-automation-v1",
                SESSION_SANDBOX_RUNTIME_PROFILE,
            }:
                egress_bridge = _start_egress_bridge(
                    egress_origins,
                    egress_rules=egress_rules,
                    private_origins=private_origins,
                    policy_version=egress_policy_version,
                    budget_scope_sha256=budget_scope_sha256,
                    call_id_sha256=call_id_sha256,
                    runtime_root=runtime_root,
                )
            if egress_bridge is not None:
                environment.update(egress_bridge.environment)
                if egress_origins:
                    egress_policy = "origin_allowlist_proxy"
            initial = {
                relative: (len(content), hashlib.sha256(content).hexdigest())
                for relative, content in workspace_files.items()
            }
            now = time.monotonic()
            handle = _new_process_handle(
                scope_digest=scope_digest,
                skill_sha256=skill_sha256,
                script_sha256=script_sha256,
            )
            lease = _ProcessLease(
                handle=handle,
                scope_digest=scope_digest,
                skill_sha256=skill_sha256,
                script_sha256=script_sha256,
                open_op_id=op_id,
                open_fingerprint=fingerprint,
                entrypoint_relative=entrypoint_relative,
                entrypoint_bytes=skill_files[entrypoint_relative],
                argv=argv,
                cwd_policy=cwd_policy,
                invocation_mode=invocation_mode,
                class_name=class_name,
                factory_name=factory_name,
                runtime_profile=runtime_profile,
                egress_policy=egress_policy,
                egress_policy_version=egress_policy_version,
                egress_bridge=egress_bridge,
                egress_audit_receipt=None,
                interpreter=interpreter,
                command=[*command, *argv],
                workdir=workdir,
                environment=environment,
                temp_dir=temp_dir,
                skill_root=skill_root,
                workspace=workspace,
                runtime_root=runtime_root,
                workspace_baseline=initial,
                created_at=now,
                last_activity=now,
                idle_ttl_seconds=idle_ttl_seconds,
                max_runtime_seconds=max_runtime_seconds,
                absolute_expires_at=now + max_runtime_seconds,
            )
            _commit_process_admission(admission_reservation, handle)
            admission_handle = handle
            _PROCESS_LEASES[handle] = lease
            _PROCESS_OPEN_OPERATIONS[(scope_digest, op_id)] = (fingerprint, handle)
            lease_published = True
            temp_dir = None
            return _process_success(
                request_id,
                "open",
                lease,
                entrypoint=entrypoint_relative,
                interpreter=interpreter,
                idle_ttl_seconds=idle_ttl_seconds,
                max_runtime_seconds=max_runtime_seconds,
                invocation_mode=invocation_mode,
                **({"class_name": class_name} if class_name is not None else {}),
                **({"factory_name": factory_name} if factory_name is not None else {}),
            )
        finally:
            if not lease_published:
                try:
                    if egress_bridge is not None:
                        _seal_egress_bridge(egress_bridge)
                finally:
                    try:
                        if temp_dir is not None:
                            _remove_process_tree(temp_dir)
                    finally:
                        # No worker process was published or started. Releasing
                        # this exact reservation is safe and must not be skipped
                        # merely because bridge sealing itself failed.
                        if admission_handle is not None:
                            _cancel_process_admission(admission_handle)
                        elif admission_reservation is not None:
                            _cancel_process_admission(admission_reservation)
            elif temp_dir is not None:
                _remove_process_tree(temp_dir)


def _start_process_lease(
    lease: _ProcessLease,
    *,
    request_id: str,
) -> dict[str, Any]:
    if lease.state != "open" or lease.process is not None:
        raise ProtocolError(
            "process_already_started",
            "The exact lease entrypoint can only be started once.",
        )
    try:
        process = subprocess.Popen(
            _resource_limited_command(
                lease.command,
                cpu_seconds=lease.max_runtime_seconds,
                persistent=True,
            ),
            cwd=lease.workdir,
            env=lease.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
            **_native_worker_popen_kwargs(),
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise ProtocolError(
            "interpreter_spawn_failed",
            f"Could not start the fixed interpreter ({type(exc).__name__}).",
        ) from exc
    lease.process = process
    # start_new_session creates a fresh process group before exec, so the
    # leader PID is also the stable process-group ID even after leader exit.
    lease.process_group_id = process.pid
    lease.state = "running"
    if process.stdin is not None:
        try:
            os.set_blocking(process.stdin.fileno(), False)
        except (AttributeError, OSError):
            pass
    for stream_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        if stream is None:
            continue
        thread = threading.Thread(
            target=_drain_process_stream,
            args=(lease, stream_name, stream),
            daemon=True,
            name=f"skill-process-{stream_name}",
        )
        lease.reader_threads.append(thread)
        thread.start()
    return _process_success(
        request_id,
        "start",
        lease,
        interpreter=lease.interpreter,
        entrypoint=lease.entrypoint_relative,
        invocation_mode=lease.invocation_mode,
        **({"class_name": lease.class_name} if lease.class_name is not None else {}),
        **({"factory_name": lease.factory_name} if lease.factory_name is not None else {}),
    )


def _write_process_bytes(
    content: bytes,
    lease: _ProcessLease,
    *,
    request_id: str,
    operation: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not content or len(content) > MAX_PROCESS_STDIN_CHUNK_BYTES:
        raise ProtocolError(
            "invalid_stdin",
            f"Each stdin write must contain 1 to {MAX_PROCESS_STDIN_CHUNK_BYTES} bytes.",
        )
    if lease.stdin_bytes_written + len(content) > MAX_PROCESS_STDIN_TOTAL_BYTES:
        raise ProtocolError(
            "stdin_limit_exceeded",
            "Process lease exhausted its aggregate stdin quota.",
        )
    if lease.stdin_closed:
        raise ProtocolError(
            "stdin_closed",
            "Process stdin has been explicitly closed.",
        )
    process = lease.process
    if lease.state != "running" or process is None or process.poll() is not None:
        _refresh_process_state_locked(lease)
        raise ProtocolError("process_not_running", "Cannot write stdin because the process is not running.")
    if process.stdin is None or process.stdin.closed:
        raise ProtocolError("stdin_closed", "Process stdin is closed.")
    try:
        written = os.write(process.stdin.fileno(), content)
    except BlockingIOError as exc:
        raise ProtocolError(
            "stdin_backpressure",
            "Process stdin is temporarily full; retry remaining bytes with a new op_id.",
        ) from exc
    except (BrokenPipeError, OSError, ValueError) as exc:
        raise ProtocolError("stdin_closed", "Process stdin is no longer writable.") from exc
    lease.stdin_bytes_written += written
    return _process_success(
        request_id,
        operation,
        lease,
        bytes_written=written,
        requested_bytes=len(content),
        partial=written != len(content),
        stdin_total_bytes=lease.stdin_bytes_written,
        **(extra or {}),
    )


def _write_process_lease(
    payload: dict[str, Any],
    lease: _ProcessLease,
    *,
    request_id: str,
) -> dict[str, Any]:
    if lease.invocation_mode != "cli":
        raise ProtocolError(
            "invalid_invocation",
            "Raw stdin writes are disabled for structured instance leases; use call.",
        )
    encoded = payload.get("stdin_b64")
    if not isinstance(encoded, str):
        raise ProtocolError("invalid_stdin", "stdin_b64 must be a bounded base64 string.")
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, binascii.Error, ValueError) as exc:
        raise ProtocolError("invalid_stdin", "stdin_b64 is not valid base64.") from exc
    return _write_process_bytes(
        content,
        lease,
        request_id=request_id,
        operation="write",
    )


def _close_process_stdin(
    lease: _ProcessLease,
    *,
    request_id: str,
) -> dict[str, Any]:
    if lease.invocation_mode != "cli":
        raise ProtocolError(
            "invalid_invocation",
            "Explicit stdin EOF is available only for CLI process leases.",
        )
    process = lease.process
    if process is None:
        raise ProtocolError(
            "process_not_started",
            "The lease process has not been started.",
        )
    already_closed = lease.stdin_closed
    if not lease.stdin_closed:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        lease.stdin_closed = True
    _refresh_process_state_locked(lease)
    return _process_success(
        request_id,
        "stdin_close",
        lease,
        stdin_closed=True,
        already_closed=already_closed,
    )


def _call_process_instance(
    payload: dict[str, Any],
    lease: _ProcessLease,
    *,
    request_id: str,
    op_id: str,
) -> dict[str, Any]:
    encoded = _validated_persistent_method_call(payload, lease, call_id=op_id)
    return _write_process_bytes(
        encoded,
        lease,
        request_id=request_id,
        operation="call",
        extra={
            "call_id": op_id,
            "class_name": lease.class_name,
            "factory_name": lease.factory_name,
            "method_name": payload.get("method_name"),
        },
    )


def _read_process_lease(
    payload: dict[str, Any],
    lease: _ProcessLease,
    *,
    request_id: str,
) -> dict[str, Any]:
    offsets: dict[str, int] = {}
    for stream_name in ("stdout", "stderr"):
        value = payload.get(f"{stream_name}_offset", 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
            raise ProtocolError(
                "invalid_stream_offset",
                f"{stream_name}_offset must be a non-negative bounded integer.",
            )
        offsets[stream_name] = value
    max_bytes = payload.get("max_bytes", MAX_PROCESS_READ_BYTES)
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_PROCESS_READ_BYTES
    ):
        raise ProtocolError(
            "invalid_read_limit",
            f"max_bytes must be between 1 and {MAX_PROCESS_READ_BYTES}.",
        )
    wait_ms = payload.get("wait_ms", 0)
    if (
        isinstance(wait_ms, bool)
        or not isinstance(wait_ms, int)
        or not 0 <= wait_ms <= MAX_PROCESS_READ_WAIT_MS
    ):
        raise ProtocolError(
            "invalid_read_wait",
            f"wait_ms must be between 0 and {MAX_PROCESS_READ_WAIT_MS}.",
        )
    for stream_name, buffer in (("stdout", lease.stdout), ("stderr", lease.stderr)):
        if offsets[stream_name] > buffer.end_offset:
            raise ProtocolError(
                "invalid_stream_offset",
                f"{stream_name}_offset is beyond the emitted stream.",
            )

    if wait_ms and lease.state == "running":
        deadline = time.monotonic() + wait_ms / 1_000
        while (
            lease.state == "running"
            and offsets["stdout"] >= lease.stdout.end_offset
            and offsets["stderr"] >= lease.stderr.end_offset
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            lease.condition.wait(timeout=remaining)
            _refresh_process_state_locked(lease)

    stdout, stdout_next, stdout_loss = lease.stdout.read(
        offsets["stdout"],
        max_bytes,
    )
    stderr, stderr_next, stderr_loss = lease.stderr.read(
        offsets["stderr"],
        max_bytes,
    )
    _refresh_process_state_locked(lease)
    returncode = lease.process.poll() if lease.process is not None else None
    return _process_success(
        request_id,
        "read",
        lease,
        stdout_b64=base64.b64encode(stdout).decode("ascii"),
        stderr_b64=base64.b64encode(stderr).decode("ascii"),
        stdout_start_offset=max(offsets["stdout"], lease.stdout.start_offset),
        stderr_start_offset=max(offsets["stderr"], lease.stderr.start_offset),
        stdout_next_offset=stdout_next,
        stderr_next_offset=stderr_next,
        stdout_end_offset=lease.stdout.end_offset,
        stderr_end_offset=lease.stderr.end_offset,
        stdout_data_loss=stdout_loss,
        stderr_data_loss=stderr_loss,
        stdout_truncated=lease.stdout.truncated,
        stderr_truncated=lease.stderr.truncated,
        stdout_eof=lease.stdout.eof,
        stderr_eof=lease.stderr.eof,
        returncode=returncode,
    )


def _sync_process_lease(
    lease: _ProcessLease,
    *,
    request_id: str,
    op_id: str,
    operation: str = "sync",
) -> dict[str, Any]:
    if lease.pending_sync_token is not None:
        raise ProtocolError(
            "sync_ack_required",
            "The previous artifact batch must be acknowledged before another sync.",
        )
    artifacts, deleted_count, current = _collect_workspace_artifacts_with_state(
        lease.workspace,
        lease.workspace_baseline,
    )
    sync_token = secrets.token_urlsafe(32)
    lease.pending_sync_token = sync_token
    lease.pending_sync_state = current
    lease.pending_sync_close = operation == "close"
    lease.pending_sync_ack_deadline = (
        time.monotonic() + PROCESS_SYNC_ACK_GRACE_SECONDS
    )
    lease.pending_sync_prepare_op_id = op_id
    return _process_success(
        request_id,
        operation,
        lease,
        artifacts=artifacts,
        sync_token=sync_token,
        sync_pending=True,
        sync_ack_grace_seconds=PROCESS_SYNC_ACK_GRACE_SECONDS,
        deleted_workspace_files_ignored=deleted_count,
    )


def _signal_process_lease(
    payload: dict[str, Any],
    lease: _ProcessLease,
    *,
    request_id: str,
) -> dict[str, Any]:
    signal_name = payload.get("signal")
    signals = {
        "interrupt": signal.SIGINT,
        "terminate": signal.SIGTERM,
        "kill": signal.SIGKILL,
    }
    if signal_name not in signals:
        raise ProtocolError(
            "invalid_signal",
            "signal must be exactly 'interrupt', 'terminate', or 'kill'.",
        )
    if lease.process is None:
        raise ProtocolError("process_not_started", "The lease process has not been started.")
    delivered = _signal_process_group(
        lease.process,
        signals[str(signal_name)],
        process_group_id=lease.process_group_id,
    )
    _refresh_process_state_locked(lease)
    return _process_success(
        request_id,
        "signal",
        lease,
        signal=signal_name,
        signal_delivered=delivered,
    )


def _terminalize_failed_process_close(
    lease: _ProcessLease,
    *,
    request_id: str,
    artifact_error_code: str,
) -> dict[str, Any]:
    """Discard an unsafe batch and release only after containment is proven."""

    _discard_pending_process_sync_locked(lease)
    try:
        _sweep_configured_worker_uid()
        if not _remove_process_tree(lease.temp_dir):
            raise ProtocolError(
                "worker_containment_failed",
                "Closed worker tree could not be removed safely.",
            )
        _release_process_admission(lease.handle)
    except ProtocolError:
        # The process is stopped, but either shared-state cleanup or tree
        # removal was not proven.  Retain both the executor admission and the
        # Harness slot quarantine instead of ever overlapping another lease.
        lease.state = "quarantined"
        lease.close_reason = "worker_containment_failed"
        lease.pending_expiry_reason = "close_artifact_collection_failed"
        lease.closed_at = None
        lease.condition.notify_all()
        return _terminal_process_error(
            request_id,
            "close",
            lease,
            "worker_containment_failed",
            "The close batch was discarded and the executor slot was quarantined.",
            terminal_state="quarantined",
            artifact_error_code=artifact_error_code,
        )

    lease.state = "closed"
    lease.close_reason = "close_artifact_collection_failed"
    lease.pending_expiry_reason = None
    lease.closed_at = time.monotonic()
    lease.condition.notify_all()
    return _terminal_process_error(
        request_id,
        "close",
        lease,
        "close_artifact_collection_failed",
        "The process closed safely, but its unsafe artifact batch was discarded.",
        terminal_state="closed",
        artifact_error_code=artifact_error_code,
    )


def _close_process_lease(
    lease: _ProcessLease,
    *,
    request_id: str,
    op_id: str,
) -> dict[str, Any]:
    try:
        _stop_process_lease_locked(lease)
    except ProtocolError as exc:
        _discard_pending_process_sync_locked(lease)
        _quarantine_process_cleanup_locked(
            lease,
            error_code=exc.code,
            retry_reason="close_egress_cleanup_failed",
        )
        return _terminal_process_error(
            request_id,
            "close",
            lease,
            exc.code,
            "The process stopped, but its egress bridge did not seal; the "
            "executor slot remains quarantined for cleanup retry.",
            terminal_state="quarantined",
            artifact_error_code=exc.code,
            returncode=(
                lease.process.poll()
                if lease.process is not None
                else None
            ),
        )
    try:
        _sweep_configured_worker_uid()
    except ProtocolError:
        lease.state = "quarantined"
        lease.close_reason = "worker_containment_failed"
        lease.pending_expiry_reason = "close_worker_sweep_failed"
        lease.closed_at = None
        lease.condition.notify_all()
        return _terminal_process_error(
            request_id,
            "close",
            lease,
            "worker_containment_failed",
            "The stopped process could not be proven contained; its slot was quarantined.",
            terminal_state="quarantined",
            artifact_error_code="worker_containment_failed",
        )
    lease.state = "closing"
    try:
        response = _sync_process_lease(
            lease,
            request_id=request_id,
            op_id=op_id,
            operation="close",
        )
    except Exception as exc:
        return _terminalize_failed_process_close(
            lease,
            request_id=request_id,
            artifact_error_code=(
                exc.code
                if isinstance(exc, ProtocolError)
                else "process_internal_error"
            ),
        )
    response["returncode"] = (
        lease.process.poll()
        if lease.process is not None
        else None
    )
    lease.condition.notify_all()
    return response


def _ack_process_sync(
    payload: dict[str, Any],
    lease: _ProcessLease,
    *,
    request_id: str,
) -> dict[str, Any]:
    token = payload.get("sync_token")
    if (
        not isinstance(token, str)
        or lease.pending_sync_token is None
        or not hmac.compare_digest(token, lease.pending_sync_token)
        or lease.pending_sync_state is None
    ):
        raise ProtocolError(
            "invalid_sync_ack",
            "sync_token does not match the pending artifact batch.",
        )
    acknowledged_close = lease.pending_sync_close
    if acknowledged_close:
        _sweep_configured_worker_uid()
        if not _remove_process_tree(lease.temp_dir):
            raise ProtocolError(
                "worker_containment_failed",
                "Closed worker tree could not be removed safely.",
            )
        _release_process_admission(lease.handle)
    # Deletions remain ignored. Retaining prior entries means a later
    # recreation is reported as a modification against the host copy.
    lease.workspace_baseline.update(lease.pending_sync_state)
    lease.pending_sync_token = None
    lease.pending_sync_state = None
    lease.pending_sync_close = False
    lease.pending_sync_ack_deadline = None
    lease.pending_sync_prepare_op_id = None
    if acknowledged_close:
        lease.state = "closed"
        lease.close_reason = "client_close"
        lease.closed_at = time.monotonic()
        lease.condition.notify_all()
    return _process_success(
        request_id,
        "ack",
        lease,
        acknowledged_operation="close" if acknowledged_close else "sync",
        sync_acknowledged=True,
    )


def _run_process_lease(payload: dict[str, Any]) -> dict[str, Any]:
    request_id: str | None = None
    operation = payload.get("operation") if isinstance(payload, dict) else None
    handle = payload.get("lease_handle") if isinstance(payload, dict) else None
    bound_lease: _ProcessLease | None = None
    try:
        if payload.get("protocol_version") != PROCESS_PROTOCOL_VERSION:
            raise ProtocolError(
                "unsupported_protocol",
                f"process lease protocol_version must be {PROCESS_PROTOCOL_VERSION}.",
            )
        _validate_process_request_auth(payload)
        _validate_process_controller_security()
        request_id = _validated_request_id(payload.get("request_id"))
        if operation not in {
            "reap_all",
            "open",
            "start",
            "write",
            "stdin_close",
            "call",
            "read",
            "sync",
            "ack",
            "signal",
            "close",
        }:
            raise ProtocolError(
                "invalid_process_operation",
                "Unsupported process lease operation.",
            )
        op_id = _validated_process_op_id(payload.get("op_id"))
        if operation == "reap_all":
            unexpected = set(payload) - {
                "protocol_version",
                "kind",
                "operation",
                "request_id",
                "op_id",
                "auth_hmac",
            }
            if unexpected:
                raise ProtocolError(
                    "invalid_process_request",
                    "Controller reap request contains unexpected fields.",
                )
            reaped = _controller_reap_process_leases()
            runtime_profile, egress_policy, _ = _profile_process_environment()
            return {
                "protocol_version": PROCESS_PROTOCOL_VERSION,
                "kind": "process_lease_result",
                "operation": "reap_all",
                "request_id": request_id,
                "status": "success",
                "network": "disabled",
                "runtime_profile": runtime_profile,
                "network_policy": {
                    "direct": "disabled",
                    "egress": egress_policy,
                },
                "reaped_leases": reaped,
                "worker_processes_empty": True,
            }
        fingerprint = _process_operation_fingerprint(payload)
        scope_digest = _validated_process_owner_scope(payload.get("owner_scope"))
        _cleanup_expired_process_leases()

        if operation == "open":
            return _open_process_lease(
                payload,
                request_id=request_id,
                op_id=op_id,
                scope_digest=scope_digest,
                fingerprint=fingerprint,
            )
        if (
            not isinstance(handle, str)
            or len(handle) > 128
            or re.fullmatch(r"pl2_[A-Za-z0-9_-]+_[0-9a-f]{32}", handle) is None
        ):
            raise ProtocolError("invalid_lease_handle", "lease_handle is not a valid opaque handle.")
        with _PROCESS_LEASES_LOCK:
            lease = _PROCESS_LEASES.get(handle)
        if lease is None:
            raise ProtocolError("lease_lost", "The process lease is unknown or no longer retained.")

        with lease.lock:
            _validated_process_binding(payload, lease)
            bound_lease = lease
            cached = _cached_process_response(
                lease,
                op_id=op_id,
                fingerprint=fingerprint,
                request_id=request_id,
            )
            if cached is not None:
                return cached
            operation_count = len(lease.operations)
            if (
                (
                    operation not in {"close", "ack"}
                    and operation_count >= MAX_PROCESS_OPERATIONS - 2
                )
                or (
                    operation == "close"
                    and operation_count >= MAX_PROCESS_OPERATIONS - 1
                )
                or (
                    operation == "ack"
                    and operation_count >= MAX_PROCESS_OPERATIONS
                )
            ):
                raise ProtocolError(
                    "operation_limit_exceeded",
                    "Process lease exhausted its bounded operation budget.",
                )
            reservation = _process_cache_reservation(payload, operation)
            close_reserve = (
                0
                if operation in {"close", "ack"}
                else MAX_PROCESS_CLOSE_CACHE_RESERVE_BYTES
            )
            if (
                lease.operation_cache_bytes + reservation + close_reserve
                > MAX_PROCESS_OPERATION_CACHE_BYTES
            ):
                raise ProtocolError(
                    "operation_cache_limit_exceeded",
                    "Process lease cannot reserve a bounded idempotency result plus close receipt.",
                )
            if lease.state == "expired":
                raise ProtocolError(
                    "lease_expired",
                    f"The process lease expired ({lease.close_reason or 'expired'}).",
                )
            if lease.state == "quarantined":
                raise ProtocolError(
                    "worker_containment_failed",
                    "The process lease is quarantined until worker cleanup is proven.",
                )
            if lease.state == "closed":
                raise ProtocolError("lease_closed", "The process lease has already been closed.")
            if lease.state == "closing" and operation != "ack":
                raise ProtocolError(
                    "sync_ack_required",
                    "The close artifact batch must be acknowledged before any other operation.",
                )
            if (
                lease.pending_sync_token is not None
                and operation in {"sync", "close"}
            ):
                raise ProtocolError(
                    "sync_ack_required",
                    "The previous artifact batch must be acknowledged before another sync or close.",
                )
            lease.last_activity = time.monotonic()

            if operation == "start":
                response = _start_process_lease(lease, request_id=request_id)
            elif operation == "write":
                response = _write_process_lease(payload, lease, request_id=request_id)
            elif operation == "stdin_close":
                response = _close_process_stdin(
                    lease,
                    request_id=request_id,
                )
            elif operation == "call":
                response = _call_process_instance(
                    payload,
                    lease,
                    request_id=request_id,
                    op_id=op_id,
                )
            elif operation == "read":
                response = _read_process_lease(payload, lease, request_id=request_id)
            elif operation == "sync":
                response = _sync_process_lease(
                    lease,
                    request_id=request_id,
                    op_id=op_id,
                )
            elif operation == "ack":
                response = _ack_process_sync(
                    payload,
                    lease,
                    request_id=request_id,
                )
            elif operation == "signal":
                response = _signal_process_lease(payload, lease, request_id=request_id)
            else:
                response = _close_process_lease(
                    lease,
                    request_id=request_id,
                    op_id=op_id,
                )
            _cache_process_response(
                lease,
                op_id=op_id,
                fingerprint=fingerprint,
                response=response,
            )
            return response
    except ProtocolError as exc:
        if bound_lease is not None:
            return _bound_process_error(
                request_id,
                operation if isinstance(operation, str) else None,
                exc.code,
                str(exc),
                lease=bound_lease,
            )
        return _process_error(
            request_id,
            operation if isinstance(operation, str) else None,
            exc.code,
            str(exc),
            handle=handle if isinstance(handle, str) else None,
        )
    except Exception as exc:
        if bound_lease is not None:
            return _bound_process_error(
                request_id,
                operation if isinstance(operation, str) else None,
                "process_internal_error",
                "Persistent process operation failed safely "
                f"({type(exc).__name__}).",
                lease=bound_lease,
            )
        return _process_error(
            request_id,
            operation if isinstance(operation, str) else None,
            "process_internal_error",
            f"Persistent process operation failed safely ({type(exc).__name__}).",
            handle=handle if isinstance(handle, str) else None,
        )


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProtocolError("invalid_request", "Request body must be a JSON object.")
    request_kind = payload.get("kind") if isinstance(payload.get("kind"), str) else "legacy_code"
    if not _request_kind_allowed(request_kind):
        if request_kind == "process_lease":
            return _process_error(
                payload.get("request_id") if isinstance(payload.get("request_id"), str) else None,
                payload.get("operation") if isinstance(payload.get("operation"), str) else None,
                "request_kind_disabled",
                "This executor profile does not accept persistent process requests.",
                handle=(
                    payload.get("lease_handle")
                    if isinstance(payload.get("lease_handle"), str)
                    else None
                ),
            )
        if request_kind == "runtime_capabilities":
            return _runtime_capability_error(
                payload.get("request_id") if isinstance(payload.get("request_id"), str) else None,
                "request_kind_disabled",
                "This executor profile does not accept capability probes.",
            )
        if request_kind == "skill_script":
            return _skill_error(
                (
                    payload.get("request_id")
                    if isinstance(payload.get("request_id"), str)
                    else None
                ),
                "request_kind_disabled",
                "This executor profile does not accept Skill scripts.",
                egress_policy=_requested_v1_egress_policy(payload),
                **_skill_invocation_receipt_fields(payload),
            )
        if request_kind == "declared_command":
            return _declared_command_error(
                (
                    payload.get("request_id")
                    if isinstance(payload.get("request_id"), str)
                    else None
                ),
                "request_kind_disabled",
                "This executor profile does not accept declared commands.",
                executable=(
                    payload.get("executable")
                    if isinstance(payload.get("executable"), str)
                    else None
                ),
                cwd=(
                    payload.get("cwd")
                    if isinstance(payload.get("cwd"), str)
                    else None
                ),
                command_sha256=_declared_command_request_sha256(payload),
                egress_policy=_requested_v1_egress_policy(payload),
            )
        return {
            "status": "error",
            "error_code": "request_kind_disabled",
            "error": "This executor profile does not accept the requested operation.",
            "network": "disabled",
        }
    if payload.get("kind") == "process_lease":
        return _run_process_lease(payload)
    if payload.get("kind") == "runtime_capabilities":
        return _run_runtime_capabilities(payload)
    if payload.get("kind") == "skill_script":
        return _run_v1_serialized(payload, _run_skill_script)
    if payload.get("kind") == "declared_command":
        return _run_v1_serialized(payload, _run_declared_command)
    if payload.get("kind") == "session_code":
        return _run_v1_serialized(payload, _run_session_code)
    if "kind" not in payload:
        return _run_v1_serialized(payload, _run_code)
    raise ProtocolError("unsupported_request_kind", "Unsupported executor request kind.")


def _encode_response(response: dict[str, Any]) -> bytes:
    encoded = json.dumps(
        response,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) <= MAX_RESPONSE_BYTES:
        return encoded
    request_id = response.get("request_id") if isinstance(response, dict) else None
    response_kind = response.get("kind") if isinstance(response, dict) else None
    response_network_policy = (
        response.get("network_policy")
        if isinstance(response, dict)
        and isinstance(response.get("network_policy"), dict)
        else {}
    )
    response_egress_policy = str(
        response_network_policy.get("egress") or "none"
    )
    if response_kind == "session_code_result":
        fallback = _session_code_error(
            request_id,
            "response_limit_exceeded",
            "Executor response exceeded the bounded protocol limit.",
        )
    elif response_kind == "skill_script_result":
        invocation_mode = (
            response.get("invocation_mode")
            if response.get("invocation_mode")
            in {"cli", "function", "instance_method"}
            else "cli"
        )
        fallback = _skill_error(
            request_id,
            "response_limit_exceeded",
            "Executor response exceeded the bounded protocol limit.",
            egress_policy=response_egress_policy,
            invocation_mode=invocation_mode,
            **(
                {"function_name": response.get("function_name")}
                if invocation_mode == "function"
                and isinstance(response.get("function_name"), str)
                else {}
            ),
            **(
                {
                    "class_name": response.get("class_name"),
                    "method_name": response.get("method_name"),
                }
                if invocation_mode == "instance_method"
                and isinstance(response.get("class_name"), str)
                and isinstance(response.get("method_name"), str)
                else {}
            ),
        )
    elif response_kind == "declared_command_result":
        fallback = _declared_command_error(
            request_id,
            "response_limit_exceeded",
            "Executor response exceeded the bounded protocol limit.",
            executable=(response.get("executable") if isinstance(response, dict) else None),
            cwd=(response.get("cwd") if isinstance(response, dict) else None),
            command_sha256=(
                response.get("command_sha256") if isinstance(response, dict) else None
            ),
            egress_policy=response_egress_policy,
        )
    elif response_kind == "runtime_capabilities_result":
        fallback = _runtime_capability_error(
            request_id,
            "response_limit_exceeded",
            "Executor response exceeded the bounded protocol limit.",
        )
    elif response_kind == "process_lease_result":
        fallback = _process_error(
            request_id,
            (
                response.get("operation")
                if isinstance(response.get("operation"), str)
                else None
            ),
            "response_limit_exceeded",
            "Executor response exceeded the bounded protocol limit.",
            handle=(
                response.get("lease_handle")
                if isinstance(response.get("lease_handle"), str)
                else None
            ),
        )
    else:
        fallback = {
            "status": "error",
            "error": "Executor response exceeded the bounded protocol limit.",
        }
    return json.dumps(fallback, separators=(",", ":")).encode("utf-8") + b"\n"


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            response = {"status": "error", "error": "Request too large."}
        elif not raw.endswith(b"\n"):
            response = {"status": "error", "error": "Request must be one bounded JSON line."}
        else:
            try:
                response = _run(json.loads(raw.decode("utf-8")))
            except Exception as exc:
                response = {
                    "status": "error",
                    "error": f"Invalid request: {type(exc).__name__}: {exc}",
                }
        self.wfile.write(_encode_response(response))


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def healthcheck() -> int:
    """Verify the live socket speaks this image's capability protocol."""

    try:
        _trusted_resource_launcher()
        _configured_egress_limits()
    except (ProtocolError, RuntimeError):
        return 1
    if _untrusted_execution_enabled():
        try:
            _validate_worker_controller_security(require_root=True)
            worker_uid, worker_gid = _configured_worker_identity(
                require_explicit=True
            )
            # A live lease legitimately owns its private descendants beneath
            # these fixed roots.  Startup, admission, sync/close, and teardown
            # perform the zero-residue audit; an out-of-process Docker
            # healthcheck cannot distinguish that active state.  Validate the
            # immutable root boundary here and let the live capability request
            # below prove the controller is responsive during execution.
            _validate_worker_shared_state_roots(
                worker_uid=worker_uid,
                worker_gid=worker_gid,
            )
        except ProtocolError:
            return 1
    if _process_protocol_enabled():
        try:
            _process_request_auth_key()
        except ProtocolError:
            return 1
    request_id = str(uuid.uuid4())
    request = json.dumps({
        "protocol_version": PROTOCOL_VERSION,
        "kind": "runtime_capabilities",
        "request_id": request_id,
        "requirements": ["packaging>=20"],
        "commands": ["bash", "node"],
        "environment_variables": ["CHATDS_WORKSPACE"],
        "platform_groups": [["linux"]],
    }, separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(SOCKET_PATH))
            connection.sendall(request)
            raw = bytearray()
            while b"\n" not in raw and len(raw) <= 256 * 1024:
                chunk = connection.recv(64 * 1024)
                if not chunk:
                    break
                raw.extend(chunk)
        if b"\n" not in raw:
            return 1
        response = json.loads(bytes(raw).split(b"\n", 1)[0].decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return 1
    identity = response.get("runtime_identity")
    execution_identity = (
        identity.get("execution_identity")
        if isinstance(identity, dict)
        else None
    )
    return 0 if (
        response.get("protocol_version") == PROTOCOL_VERSION
        and response.get("kind") == "runtime_capabilities_result"
        and response.get("request_id") == request_id
        and response.get("status") == "success"
        and response.get("valid") is True
        and isinstance(identity, dict)
        and identity.get("execution_runtime") == "isolated_skill_executor"
        and identity.get("network") == "disabled"
        and isinstance(identity.get("runtime_build_sha256"), str)
        and re.fullmatch(
            r"[0-9a-f]{64}",
            identity["runtime_build_sha256"],
        ) is not None
        and identity.get("runtime_profile") in {
            "base-v1",
            "browser-automation-v1",
            SESSION_SANDBOX_RUNTIME_PROFILE,
        }
        and isinstance(identity.get("network_policy"), dict)
        and identity["network_policy"].get("direct") == "disabled"
        and identity["network_policy"].get("egress") in {
            "none",
            "policy_proxy",
            "origin_allowlist_proxy",
        }
        and (
            (
                identity.get("runtime_profile") in {
                    "browser-automation-v1",
                    SESSION_SANDBOX_RUNTIME_PROFILE,
                }
                and identity.get("display_backend") == "wayland-headless"
                and identity.get("headed_browser") is True
                and identity.get("x11") is False
            )
            or (
                identity.get("runtime_profile") == "base-v1"
                and identity.get("display_backend") == "none"
                and identity.get("headed_browser") is False
                and identity.get("x11") is False
            )
        )
        and identity.get("dependency_install") == "disabled"
        and isinstance(execution_identity, dict)
        and execution_identity.get("resource_launcher") == "prlimit"
        and isinstance(execution_identity.get("uid_isolated"), bool)
        and execution_identity.get("shared_state_isolated") is True
        and (
            not _untrusted_execution_enabled()
            or execution_identity.get("uid_isolated") is True
        )
    ) else 1


def _configured_socket_mode() -> int:
    raw = os.environ.get("EXECUTOR_SOCKET_MODE", "0600")
    if raw not in {"0600", "0660"}:
        raise RuntimeError("EXECUTOR_SOCKET_MODE must be exactly 0600 or 0660.")
    return int(raw, 8)


def _configured_socket_gid() -> int:
    raw = os.environ.get("EXECUTOR_SOCKET_GID")
    if raw is None:
        return os.getegid()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("EXECUTOR_SOCKET_GID must be a numeric GID.") from exc
    if not 0 <= value <= 2**31 - 1:
        raise RuntimeError("EXECUTOR_SOCKET_GID is outside the supported range.")
    return value


def _prepare_socket_directory() -> tuple[int, int]:
    """Make the controller own the UDS directory without worker write access."""

    mode = _configured_socket_mode()
    socket_gid = _configured_socket_gid()
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        item = SOCKET_PATH.parent.lstat()
    except OSError as exc:
        raise RuntimeError("Executor socket directory is unavailable.") from exc
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
        raise RuntimeError("Executor socket directory must be a real directory.")
    controller_uid = os.geteuid()
    if controller_uid == 0:
        os.chown(
            SOCKET_PATH.parent,
            controller_uid,
            socket_gid,
            follow_symlinks=False,
        )
    elif item.st_uid != controller_uid or item.st_gid != socket_gid:
        raise RuntimeError("Executor controller does not own its socket directory.")
    # Group traversal is sufficient for a 0660 socket. The directory is never
    # group-writable, so neither Harness nor the worker can unlink the UDS.
    os.chmod(SOCKET_PATH.parent, 0o750 if mode == 0o660 else 0o700)
    return mode, socket_gid


def _secure_socket_file(mode: int, socket_gid: int) -> None:
    item = SOCKET_PATH.lstat()
    if not stat.S_ISSOCK(item.st_mode):
        raise RuntimeError("Executor socket path is not a Unix-domain socket.")
    if os.geteuid() == 0:
        os.chown(SOCKET_PATH, os.geteuid(), socket_gid, follow_symlinks=False)
    os.chmod(SOCKET_PATH, mode)


def _harden_daemon_dumpability() -> bool:
    """Best-effort same-UID /proc protection until deployment splits UIDs."""

    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # Linux prctl(2): PR_SET_DUMPABLE = 4.
        return libc.prctl(4, 0, 0, 0, 0) == 0
    except (AttributeError, OSError, ValueError):
        return False


def main() -> None:
    global DAEMON_DUMPABILITY_HARDENED
    DAEMON_DUMPABILITY_HARDENED = _harden_daemon_dumpability()
    _trusted_resource_launcher()
    _configured_egress_limits()
    if _untrusted_execution_enabled():
        _validate_worker_controller_security(require_root=True)
        _sweep_configured_worker_uid()
        _purge_configured_worker_shared_state()
    if _process_protocol_enabled():
        _process_request_auth_key()
    socket_mode, socket_gid = _prepare_socket_directory()
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    janitor_stop = threading.Event()
    janitor = threading.Thread(
        target=_process_lease_janitor,
        args=(janitor_stop,),
        daemon=True,
        name="skill-process-lease-janitor",
    )
    janitor.start()
    try:
        with Server(str(SOCKET_PATH), Handler) as server:
            _secure_socket_file(socket_mode, socket_gid)
            server.serve_forever(poll_interval=0.2)
    finally:
        janitor_stop.set()
        janitor.join(timeout=2)
        _shutdown_all_process_leases()


if __name__ == "__main__":
    if sys.argv[1:] == ["--healthcheck"]:
        raise SystemExit(healthcheck())
    main()
