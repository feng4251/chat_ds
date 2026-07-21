"""Network-isolated execution daemon.

The container is launched with ``network_mode: none`` and communicates with
the harness only through a shared Unix-domain socket. Protocol-v1 supports
session-code and Skill-script requests (including declarative public-function
calls); a legacy source-only calculation form remains for compatibility.
Skill execution receives immutable Skill file bytes plus a disposable
session-workspace snapshot; neither host tree is mounted into the executor.
"""

from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import hmac
import importlib.metadata
import json
import os
import resource
import re
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement


SOCKET_PATH = Path(os.environ.get(
    "EXECUTOR_SOCKET", "/run/chat-ds-executor/executor.sock"
))

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024 * 1024
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
MAX_WORKSPACE_FILE_BYTES = 8 * 1024 * 1024
MAX_WORKSPACE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_FILES = 512
MAX_OUTPUT_FILE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_TOTAL_BYTES = 24 * 1024 * 1024
MAX_OUTPUT_ENTRIES = 4_096
MAX_RUNTIME_REQUIREMENTS = 80
MAX_RUNTIME_COMMANDS = 80
MAX_RUNTIME_ENVIRONMENT_VARIABLES = 80
MAX_RUNTIME_PLATFORM_GROUPS = 32
MAX_RUNTIME_PLATFORMS_PER_GROUP = 32
MAX_RUNTIME_DECLARATION_CHARS = 512

MAX_ADDRESS_SPACE_BYTES = int(os.environ.get(
    "EXECUTOR_MAX_ADDRESS_SPACE_BYTES", str(2 * 1024 * 1024 * 1024)
))

SUPPORTED_INTERPRETERS = {
    ".py": "python",
    ".sh": "bash",
    ".bash": "bash",
    ".js": "node",
    ".mjs": "node",
}

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


class ProtocolError(ValueError):
    """A stable validation failure safe to return across the socket."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _child_limits(cpu_seconds: int = 125) -> None:
    os.setsid()
    cpu = max(1, min(int(cpu_seconds), MAX_SKILL_TIMEOUT + 5))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_AS, (MAX_ADDRESS_SPACE_BYTES,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_FILE_BYTES,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


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
    # placed in a fresh process group by _child_limits; reap that entire group
    # after the leader exits so no background code survives one request.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass

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
    temp_dir = Path(tempfile.mkdtemp(prefix="exec_", dir="/tmp"))
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

    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", str(script)],
            cwd=temp_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=lambda: _child_limits(timeout + 5),
            close_fds=True,
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
        shutil.rmtree(temp_dir, ignore_errors=True)


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
        os.chmod(destination, 0o444 if immutable else 0o600)
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
        environment_results = [
            {
                "name": name,
                "available": name in SKILL_RUNTIME_ENVIRONMENT_VARIABLES,
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
                "dependency_install": "disabled",
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
) -> tuple[str, list[str]]:
    kind = SUPPORTED_INTERPRETERS.get(entrypoint.suffix)
    if kind == "python":
        runner_path = runtime_root / "cli_runner.py"
        with runner_path.open("xb") as stream:
            stream.write(CLI_RUNNER_SOURCE.encode("utf-8"))
        os.chmod(runner_path, 0o400)
        return kind, [
            sys.executable,
            "-I",
            "-B",
            str(runner_path),
            str(skill_root),
            str(entrypoint),
        ]
    if kind == "bash":
        executable = Path("/bin/bash")
        if not executable.is_file():
            raise ProtocolError("interpreter_unavailable", "The fixed bash interpreter is unavailable.")
        return kind, [str(executable), "--noprofile", "--norc", str(entrypoint)]
    if kind == "node":
        executable = Path("/usr/bin/node")
        if not executable.is_file():
            raise ProtocolError("interpreter_unavailable", "The fixed Node.js interpreter is unavailable.")
        return kind, [str(executable), str(entrypoint)]
    raise ProtocolError(
        "unsupported_script_type",
        "entrypoint must end in .py, .sh, .bash, .js, or .mjs.",
    )


def _read_regular_file(path: Path, *, display_path: str) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProtocolError("unsafe_workspace_output", f"Cannot safely open output: {display_path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ProtocolError(
                "unsafe_workspace_output",
                f"Workspace output is not an independent regular file: {display_path}",
            )
        if before.st_size > MAX_OUTPUT_FILE_BYTES:
            raise ProtocolError("output_limit_exceeded", f"Workspace output is too large: {display_path}")
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
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if len(content) != before.st_size or before_identity != after_identity:
            raise ProtocolError("workspace_output_race", f"Workspace output changed during collection: {display_path}")
        return content, after
    finally:
        os.close(descriptor)


def _collect_workspace_artifacts(
    workspace: Path,
    initial: dict[str, tuple[int, str]],
) -> tuple[list[dict[str, Any]], int]:
    artifacts: list[dict[str, Any]] = []
    observed: set[str] = set()
    output_total = 0
    entries = 0
    stack: list[tuple[Path, str, int]] = [(workspace, "", 0)]

    while stack:
        directory, prefix, depth = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ProtocolError("unsafe_workspace_output", "Cannot safely scan the workspace output.") from exc
        for child in children:
            entries += 1
            if entries > MAX_OUTPUT_ENTRIES:
                raise ProtocolError("output_limit_exceeded", "Workspace output has too many filesystem entries.")
            relative = f"{prefix}/{child.name}" if prefix else child.name
            safe_relative = _safe_relative_path(relative, field="workspace output path")
            if PurePosixPath(safe_relative).parts[0] in RESERVED_WORKSPACE_ROOTS:
                raise ProtocolError(
                    "unsafe_workspace_output",
                    f"Writes to reserved workspace paths are forbidden: {safe_relative}",
                )
            try:
                item_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise ProtocolError("unsafe_workspace_output", f"Cannot inspect output: {safe_relative}") from exc
            mode = item_stat.st_mode
            if stat.S_ISLNK(mode):
                raise ProtocolError("unsafe_workspace_output", f"Symlink output is forbidden: {safe_relative}")
            if stat.S_ISDIR(mode):
                if depth + 1 >= MAX_PATH_DEPTH:
                    raise ProtocolError("output_limit_exceeded", f"Workspace output is too deeply nested: {safe_relative}")
                stack.append((Path(child.path), safe_relative, depth + 1))
                continue
            if not stat.S_ISREG(mode) or item_stat.st_nlink != 1:
                raise ProtocolError(
                    "unsafe_workspace_output",
                    f"Only independent regular-file outputs are allowed: {safe_relative}",
                )
            observed.add(safe_relative)
            content, stable_stat = _read_regular_file(Path(child.path), display_path=safe_relative)
            digest = hashlib.sha256(content).hexdigest()
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

    artifacts.sort(key=lambda item: item["path"])
    return artifacts, len(set(initial) - observed)


def _skill_error(
    request_id: str | None,
    code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "skill_script_result",
        "request_id": request_id,
        "status": "error",
        "error_code": code,
        "error": message,
        "network": "disabled",
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
    **extra: Any,
) -> dict[str, Any]:
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
    try:
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise ProtocolError(
                "unsupported_protocol", f"protocol_version must be {PROTOCOL_VERSION}."
            )
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

        temp_dir = Path(tempfile.mkdtemp(prefix="declared_command_", dir="/tmp"))
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
            "CHATDS_OUTPUT_DIR": str(output_dir),
            **BLAS_THREAD_ENV,
        }
        try:
            proc = subprocess.Popen(
                [resolved_executable, *argv],
                shell=False,
                cwd=workspace if cwd_policy == "workspace" else skill_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=lambda: _child_limits(timeout + 5),
                close_fds=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ProtocolError(
                "command_spawn_failed",
                f"Could not start the declared executable ({type(exc).__name__}).",
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
            "kind": "declared_command_result",
            "request_id": request_id,
            "status": status,
            "returncode": proc.returncode,
            "executable": executable,
            "cwd": cwd_policy,
            "command_sha256": command_sha256,
            "shell": False,
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
                error_code="command_timeout",
                error=f"Declared command timed out after {timeout}s.",
            )
        elif proc.returncode != 0:
            response.update(
                error_code="command_exit_nonzero",
                error=stderr_text or f"Declared command exited with return code {proc.returncode}.",
            )
        return response
    except ProtocolError as exc:
        return _declared_command_error(
            request_id,
            exc.code,
            str(exc),
            executable=executable,
            cwd=cwd_policy,
            command_sha256=command_sha256,
            duration_seconds=round(time.monotonic() - started, 3),
        )
    except Exception as exc:
        return _declared_command_error(
            request_id,
            "executor_internal_error",
            f"The declared-command executor failed safely ({type(exc).__name__}).",
            executable=executable,
            cwd=cwd_policy,
            command_sha256=command_sha256,
            duration_seconds=round(time.monotonic() - started, 3),
        )
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _run_skill_script(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    request_id: str | None = None
    temp_dir: Path | None = None
    invocation_mode = "cli"
    function_name: str | None = None
    class_name: str | None = None
    method_name: str | None = None
    try:
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise ProtocolError("unsupported_protocol", f"protocol_version must be {PROTOCOL_VERSION}.")
        request_id = _validated_request_id(payload.get("request_id"))
        entrypoint_relative = _safe_relative_path(payload.get("entrypoint"), field="entrypoint")
        argv = _validated_args(payload.get("argv", []))
        timeout = _validated_timeout(payload.get("timeout", 120))
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

        temp_dir = Path(tempfile.mkdtemp(prefix="skill_exec_", dir="/tmp"))
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
            command = [
                sys.executable,
                "-I",
                "-B",
                str(runner_path),
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
        else:
            interpreter, command = _interpreter_command(
                entrypoint,
                skill_root=skill_root,
                runtime_root=runtime_root,
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
            "CHATDS_OUTPUT_DIR": str(output_dir),
            **BLAS_THREAD_ENV,
        }

        try:
            proc = subprocess.Popen(
                [*command, *argv] if invocation_mode == "cli" else command,
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=lambda: _child_limits(timeout + 5),
                close_fds=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise ProtocolError(
                "interpreter_spawn_failed",
                f"Could not start the fixed interpreter ({type(exc).__name__}).",
            ) from exc

        stdout, stderr, stdout_truncated, stderr_truncated, timed_out = (
            _communicate_capped(proc, timeout=timeout)
        )
        artifacts, deleted_count = _collect_workspace_artifacts(workspace, initial)
        envelope: dict[str, Any] | None = None
        if invocation_mode in {"function", "instance_method"} and not timed_out:
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
        status = "timeout" if timed_out else (
            str(envelope.get("status")) if envelope is not None
            else ("success" if proc.returncode == 0 else "error")
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
        if timed_out:
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
        return response
    except ProtocolError as exc:
        return _skill_error(
            request_id,
            exc.code,
            str(exc),
            invocation_mode=invocation_mode,
            **({"function_name": function_name} if function_name else {}),
            **({"class_name": class_name} if class_name else {}),
            **({"method_name": method_name} if method_name else {}),
            duration_seconds=round(time.monotonic() - started, 3),
        )
    except Exception as exc:
        return _skill_error(
            request_id,
            "executor_internal_error",
            f"The isolated Skill executor failed safely ({type(exc).__name__}).",
            invocation_mode=invocation_mode,
            **({"function_name": function_name} if function_name else {}),
            **({"class_name": class_name} if class_name else {}),
            **({"method_name": method_name} if method_name else {}),
            duration_seconds=round(time.monotonic() - started, 3),
        )
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


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

        temp_dir = Path(tempfile.mkdtemp(prefix="session_code_", dir="/tmp"))
        skills_root = temp_dir / "skills"
        workspace = temp_dir / "workspace"
        runtime_root = temp_dir / "runtime"
        code_root = temp_dir / "code"
        _materialize_snapshot(skills_root, skill_files, immutable=True)
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
            "CHATDS_OUTPUT_DIR": str(output_dir),
            **BLAS_THREAD_ENV,
        }
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-B", str(script)],
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=lambda: _child_limits(timeout + 5),
                close_fds=True,
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
            shutil.rmtree(temp_dir, ignore_errors=True)


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProtocolError("invalid_request", "Request body must be a JSON object.")
    if payload.get("kind") == "runtime_capabilities":
        return _run_runtime_capabilities(payload)
    if payload.get("kind") == "skill_script":
        return _run_skill_script(payload)
    if payload.get("kind") == "declared_command":
        return _run_declared_command(payload)
    if payload.get("kind") == "session_code":
        return _run_session_code(payload)
    if "kind" not in payload:
        return _run_code(payload)
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
    if response_kind == "session_code_result":
        fallback = _session_code_error(
            request_id,
            "response_limit_exceeded",
            "Executor response exceeded the bounded protocol limit.",
        )
    elif response_kind == "skill_script_result":
        fallback = _skill_error(
            request_id,
            "response_limit_exceeded",
            "Executor response exceeded the bounded protocol limit.",
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
        )
    elif response_kind == "runtime_capabilities_result":
        fallback = _runtime_capability_error(
            request_id,
            "response_limit_exceeded",
            "Executor response exceeded the bounded protocol limit.",
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
    return 0 if (
        response.get("protocol_version") == PROTOCOL_VERSION
        and response.get("kind") == "runtime_capabilities_result"
        and response.get("request_id") == request_id
        and response.get("status") == "success"
        and response.get("valid") is True
        and isinstance(identity, dict)
        and identity.get("execution_runtime") == "isolated_skill_executor"
        and identity.get("network") == "disabled"
        and identity.get("dependency_install") == "disabled"
    ) else 1


def main() -> None:
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    with Server(str(SOCKET_PATH), Handler) as server:
        os.chmod(SOCKET_PATH, 0o666)
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    if sys.argv[1:] == ["--healthcheck"]:
        raise SystemExit(healthcheck())
    main()
