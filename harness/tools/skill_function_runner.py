"""Static subprocess runner for declarative calls into authorized Skill modules.

This module intentionally accepts only a resolved Skill file, one public
function identifier, and JSON positional/keyword arguments.  It never accepts
source code, expressions, dotted attribute paths, or pickle data.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import traceback
from typing import Any


PUBLIC_FUNCTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
MAX_INPUT_BYTES = 128_000
MAX_ARGS = 64
MAX_KWARGS = 128
MAX_JSON_DEPTH = 20
MAX_RESULT_FILE_BYTES = 260_000
RESULT_DIRECTORY_COMPONENTS = (".chatds", "function_calls")


class _CappedTextIO(io.TextIOBase):
    """A text sink that discards content after a deterministic character cap."""

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self._parts: list[str] = []
        self._size = 0
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        text = value if isinstance(value, str) else str(value)
        original_length = len(text)
        remaining = max(0, self.limit - self._size)
        if remaining:
            kept = text[:remaining]
            self._parts.append(kept)
            self._size += len(kept)
        if original_length > remaining:
            self.truncated = True
        return original_length

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        value = "".join(self._parts)
        if self.truncated:
            value += "\n... [truncated]"
        return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--script", required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--max-result-chars", required=True, type=int)
    parser.add_argument("--max-stdout-chars", required=True, type=int)
    parser.add_argument("--max-stderr-chars", required=True, type=int)
    return parser.parse_args()


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_result_directory(workspace: Path) -> int:
    """Open the managed result directory without following path links.

    Each child is opened relative to the already-authorized parent descriptor.
    This closes the usual check/resolve/open gap and ensures a replacement
    symlink is rejected even if it appears after initial path validation.
    """

    flags = _directory_open_flags()
    try:
        current_fd = os.open(workspace, flags)
    except OSError as exc:
        raise ValueError("Managed workspace is missing, linked, or not a directory.") from exc
    try:
        for component in RESULT_DIRECTORY_COMPONENTS:
            try:
                child_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                raise ValueError(
                    "Managed function-call directory is missing, linked, or not a directory."
                ) from exc
            try:
                child_stat = os.fstat(child_fd)
                if not stat.S_ISDIR(child_stat.st_mode):
                    raise ValueError(
                        "Managed function-call directory contains a non-directory component."
                    )
            except Exception:
                os.close(child_fd)
                raise
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _validate_paths(script_text: str, result_text: str) -> tuple[Path, Path]:
    skill_root_text = os.environ.get("CHATDS_SKILL_ROOT")
    workspace_text = os.environ.get("CHATDS_WORKSPACE")
    if not skill_root_text or not workspace_text:
        raise ValueError("Managed Skill roots are unavailable.")
    skill_root = Path(skill_root_text).resolve()
    workspace_input = Path(workspace_text)
    if not workspace_input.is_absolute() or workspace_input.is_symlink():
        raise ValueError("Managed workspace must be an absolute, non-linked directory.")
    try:
        workspace = workspace_input.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Managed workspace is unavailable.") from exc
    if not workspace.is_dir():
        raise ValueError("Managed workspace is not a directory.")
    script = Path(script_text)
    result = Path(result_text)
    if not script.is_absolute() or not result.is_absolute():
        raise ValueError("Managed runner paths must be absolute harness-resolved paths.")
    if script.is_symlink() or not script.is_file() or script.suffix != ".py":
        raise ValueError("Selected Skill script is missing, linked, or not Python.")
    resolved_script = script.resolve()
    try:
        resolved_script.relative_to(skill_root)
    except ValueError as exc:
        raise ValueError("Selected script escapes the current session Skill root.") from exc
    allowed_results = workspace.joinpath(*RESULT_DIRECTORY_COMPONENTS)
    if result.parent != allowed_results or result.name in {"", ".", ".."}:
        raise ValueError("Result envelope path escapes the managed function-call directory.")

    directory_fd = _open_result_directory(workspace)
    try:
        try:
            os.stat(result.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("Result envelope path already exists or is linked.")
    finally:
        os.close(directory_fd)
    return resolved_script, result


def _validate_json_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"Argument JSON exceeds nesting depth {MAX_JSON_DEPTH}.")
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_depth(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Argument JSON object keys must be strings.")
            _validate_json_depth(item, depth + 1)
        return
    raise ValueError(f"Unsupported argument JSON type: {type(value).__name__}.")


def _read_payload() -> tuple[list[Any], dict[str, Any]]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError(f"Function argument JSON exceeds {MAX_INPUT_BYTES} bytes.")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"args", "kwargs"}:
        raise ValueError("Function payload must contain exactly args and kwargs.")
    positional = payload["args"]
    keywords = payload["kwargs"]
    if not isinstance(positional, list) or len(positional) > MAX_ARGS:
        raise ValueError(f"args must be a JSON array with at most {MAX_ARGS} items.")
    if not isinstance(keywords, dict) or len(keywords) > MAX_KWARGS:
        raise ValueError(f"kwargs must be a JSON object with at most {MAX_KWARGS} items.")
    for key in keywords:
        if not PUBLIC_FUNCTION_RE.fullmatch(key) or key.startswith("_"):
            raise ValueError(f"Invalid public keyword argument name: {key!r}.")
    _validate_json_depth(positional)
    _validate_json_depth(keywords)
    return positional, keywords


def _load_public_function(script: Path, function_name: str):
    if not PUBLIC_FUNCTION_RE.fullmatch(function_name) or function_name.startswith("_"):
        raise ValueError("Function must be one public, non-dotted Python identifier.")
    module_name = "_chatds_skill_" + hashlib.sha256(str(script).encode("utf-8")).hexdigest()[:20]
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
    function = module.__dict__.get(function_name)
    if not callable(function):
        raise ValueError(f"Selected public function {function_name!r} is not callable as a Python function.")
    try:
        declared_function = inspect.unwrap(function)
    except (ValueError, TypeError) as exc:
        raise ValueError("Could not validate the selected function's decorator chain.") from exc
    if not inspect.isfunction(declared_function):
        raise ValueError("Imported or replaced functions are not callable through this interface.")
    if getattr(declared_function, "__module__", None) != module_name:
        raise ValueError("Imported or replaced functions are not callable through this interface.")
    if getattr(declared_function, "__name__", None) != function_name:
        raise ValueError("Reassigned functions are not callable through this interface.")
    if getattr(declared_function, "__qualname__", None) != function_name:
        raise ValueError("Nested or dynamically rebound functions are not callable through this interface.")
    return function


def _bounded_json_result(value: Any, limit: int) -> dict[str, Any]:
    limit = max(1, int(limit))
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    chunks: list[str] = []
    length = 0
    try:
        for chunk in encoder.iterencode(value):
            if length + len(chunk) > limit:
                remaining = max(0, limit - length)
                if remaining:
                    chunks.append(chunk[:remaining])
                    length += remaining
                return {
                    "result": None,
                    "result_truncated": True,
                    "result_preview": "".join(chunks),
                    "result_json_chars": f">{limit}",
                }
            chunks.append(chunk)
            length += len(chunk)
    except (TypeError, ValueError) as exc:
        return {
            "result": None,
            "result_truncated": False,
            "result_type": type(value).__name__,
            "result_serialization_error": f"{type(exc).__name__}: {exc}",
        }
    encoded = "".join(chunks)
    return {
        "result": json.loads(encoded),
        "result_truncated": False,
        "result_json_chars": length,
    }


def _invoke(script: Path, function_name: str, result_limit: int) -> dict[str, Any]:
    positional, keywords = _read_payload()
    function = _load_public_function(script, function_name)
    try:
        inspect.signature(function).bind(*positional, **keywords)
    except TypeError as exc:
        raise TypeError(f"Arguments do not match {function_name}: {exc}") from exc
    value = function(*positional, **keywords)
    if inspect.isawaitable(value):
        value = asyncio.run(value)
    result = {
        "status": "success",
        "function_name": function_name,
    }
    result.update(_bounded_json_result(value, result_limit))
    return result


def _write_envelope(path: Path, envelope: dict[str, Any], workspace: Path) -> None:
    encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_RESULT_FILE_BYTES:
        encoded = json.dumps({
            "status": "error",
            "error": f"Function result envelope exceeded {MAX_RESULT_FILE_BYTES} bytes.",
        }).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_fd = _open_result_directory(workspace)
    try:
        try:
            descriptor = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        except OSError as exc:
            raise ValueError("Result envelope path is linked, replaced, or already exists.") from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
    finally:
        os.close(directory_fd)


def main() -> int:
    options = _parse_args()
    stdout = _CappedTextIO(options.max_stdout_chars)
    stderr = _CappedTextIO(options.max_stderr_chars)
    result_path: Path | None = None
    workspace_path: Path | None = None
    envelope: dict[str, Any]
    exit_code = 0
    try:
        script, result_path = _validate_paths(options.script, options.result_file)
        workspace_path = Path(os.environ["CHATDS_WORKSPACE"]).resolve(strict=True)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            envelope = _invoke(script, options.function, options.max_result_chars)
    except BaseException as exc:  # convert SystemExit/KeyboardInterrupt from Skill code into audit data
        exit_code = 1
        envelope = {
            "status": "error",
            "function_name": options.function,
            "error": f"{type(exc).__name__}: {exc}",
            "exception_type": type(exc).__name__,
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-20_000:],
        }
        if result_path is None:
            sys.stderr.write(
                f"Managed function request was rejected before result-path authorization: "
                f"{type(exc).__name__}: {exc}\n"
            )
            return 2
    envelope["stdout"] = stdout.getvalue()
    envelope["stderr"] = stderr.getvalue()
    envelope["stdout_truncated"] = stdout.truncated
    envelope["stderr_truncated"] = stderr.truncated
    try:
        if workspace_path is None:
            raise ValueError("Managed workspace authorization was not retained.")
        _write_envelope(result_path, envelope, workspace_path)
    except Exception as exc:
        sys.stderr.write(f"Could not write managed result envelope: {type(exc).__name__}: {exc}\n")
        return 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
