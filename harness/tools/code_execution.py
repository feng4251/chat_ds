"""Python execution through a dedicated network-isolated container."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path, PurePosixPath

from tools.approval import check_code_danger, check_code_warnings
from tools.path_security import SANDBOX_ROOT, sandbox_dir

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_CODE_BYTES = 900_000
MAX_SNAPSHOT_FILES = 120
MAX_SNAPSHOT_FILE_BYTES = 200_000
MAX_SNAPSHOT_TOTAL_BYTES = 650_000
SNAPSHOT_EXTENSIONS = {
    ".py", ".json", ".csv", ".tsv", ".txt", ".md", ".yaml", ".yml",
}
SKILL_DATA_ROOT = Path(os.environ.get("SKILL_DATA_ROOT", "/app/data/skills"))
EXECUTOR_SOCKET = os.environ.get(
    "EXECUTOR_SOCKET", "/run/chat-ds-executor/executor.sock"
)


def _safe_snapshot_relpath(path: Path, root: Path, prefix: str) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    if path.suffix.lower() not in SNAPSHOT_EXTENSIONS:
        return None
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if any(part in {"", ".", ".."} for part in rel.parts):
        return None
    return str(PurePosixPath(prefix, *rel.parts))


def _snapshot_file(path: Path, root: Path, prefix: str, files: list[dict], budget: dict) -> bool:
    rel = _safe_snapshot_relpath(path, root, prefix)
    if rel is None or any(existing.get("path") == rel for existing in files):
        return False
    if budget["files"] >= MAX_SNAPSHOT_FILES or budget["bytes"] >= MAX_SNAPSHOT_TOTAL_BYTES:
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size > MAX_SNAPSHOT_FILE_BYTES or budget["bytes"] + size > MAX_SNAPSHOT_TOTAL_BYTES:
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except UnicodeDecodeError:
        return False
    content = content.encode("utf-8", errors="replace").decode("utf-8")
    files.append({"path": rel, "content": content})
    budget["files"] += 1
    budget["bytes"] += size
    return True


def _snapshot_files_from_root(root: Path, prefix: str, files: list[dict], budget: dict) -> None:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if budget["files"] >= MAX_SNAPSHOT_FILES or budget["bytes"] >= MAX_SNAPSHOT_TOTAL_BYTES:
            return
        _snapshot_file(path, root, prefix, files, budget)


def _referenced_session_files(code: str, user_id: str, session_id: str) -> list[tuple[Path, Path, str]]:
    refs: list[tuple[Path, Path, str]] = []
    roots = [
        ((SKILL_DATA_ROOT / user_id / session_id).resolve(), f"{SKILL_DATA_ROOT}/{user_id}/{session_id}/", "skills"),
        ((SANDBOX_ROOT / user_id / session_id / "workspace").resolve(), f"{SANDBOX_ROOT}/{user_id}/{session_id}/workspace/", "workspace"),
    ]
    for root, marker, prefix in roots:
        if marker not in code:
            continue
        for match in re.finditer(re.escape(marker) + r"[^'\"\s)]+", code):
            rel_text = match.group(0)[len(marker):]
            rel = PurePosixPath(rel_text)
            if rel.is_absolute() or ".." in rel.parts:
                continue
            path = (root / Path(*rel.parts)).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            refs.append((path, root, prefix))
    return refs


def _session_snapshot(user_id: str, session_id: str, code: str = "") -> list[dict]:
    if not user_id or user_id == "default" or not session_id or session_id == "default":
        return []
    files: list[dict] = []
    budget = {"files": 0, "bytes": 0}
    for path, root, prefix in _referenced_session_files(code, user_id, session_id):
        _snapshot_file(path, root, prefix, files, budget)
    _snapshot_files_from_root(sandbox_dir(user_id, session_id, sub="workspace"), "workspace", files, budget)
    skill_root = (SKILL_DATA_ROOT / user_id / session_id).resolve()
    try:
        skill_root.relative_to(SKILL_DATA_ROOT.resolve())
    except ValueError:
        return files
    for pattern in ("SKILL.md", "__manifest__", "*.py", "*.json", "*.csv", "*.md"):
        for path in sorted(skill_root.rglob(pattern)):
            if budget["files"] >= MAX_SNAPSHOT_FILES or budget["bytes"] >= MAX_SNAPSHOT_TOTAL_BYTES:
                return files
            _snapshot_file(path, skill_root, "skills", files, budget)
    return files


def _code_with_session_snapshot(code: str, user_id: str, session_id: str) -> str:
    files = _session_snapshot(user_id, session_id, code)
    if not files:
        return code
    skill_abs_prefix = f"{SKILL_DATA_ROOT}/{user_id}/{session_id}/"
    workspace_abs_prefix = f"{SANDBOX_ROOT}/{user_id}/{session_id}/workspace/"
    rewritten = code.replace(skill_abs_prefix, "skills/").replace(workspace_abs_prefix, "workspace/")
    prelude = (
        "import json as __chatds_json, pathlib as __chatds_pathlib\n"
        f"__chatds_files = __chatds_json.loads({json.dumps(json.dumps(files, ensure_ascii=False))})\n"
        "for __chatds_file in __chatds_files:\n"
        "    __chatds_path = __chatds_pathlib.Path(__chatds_file['path'])\n"
        "    __chatds_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    __chatds_path.write_text(__chatds_file['content'], encoding='utf-8', errors='replace')\n"
        "    if __chatds_file['path'].startswith('workspace/'):\n"
        "        __chatds_workspace_path = __chatds_pathlib.Path(__chatds_file['path'][10:])\n"
        "        __chatds_workspace_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "        __chatds_workspace_path.write_text(__chatds_file['content'], encoding='utf-8', errors='replace')\n"
        "del __chatds_json, __chatds_pathlib, __chatds_files, __chatds_file, __chatds_path\n"
        "try:\n"
        "    del __chatds_workspace_path\n"
        "except NameError:\n"
        "    pass\n"
    )
    return prelude + rewritten


async def execute_code(
    code: str,
    timeout: int = DEFAULT_TIMEOUT,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Run Python in a separate container with no network namespace."""
    if not code or not code.strip():
        return json.dumps({"status": "error", "error": "No code provided."})
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return json.dumps({"status": "error", "error": "Code payload is too large."})

    danger = check_code_danger(code)
    if danger:
        return json.dumps({"status": "blocked", "error": danger})

    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    code = _code_with_session_snapshot(code, user_id, session_id)
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return json.dumps({"status": "error", "error": "Code plus session snapshot is too large."})
    request = json.dumps(
        {"code": code, "timeout": timeout}, ensure_ascii=False
    ).encode("utf-8") + b"\n"

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(EXECUTOR_SOCKET), timeout=3
        )
        writer.write(request)
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout + 10)
        writer.close()
        await writer.wait_closed()
        if not raw:
            raise RuntimeError("executor closed the socket without a response")
        result = json.loads(raw.decode("utf-8"))
        warnings = check_code_warnings(code)
        if warnings:
            result["warnings"] = warnings
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        logger.exception("Isolated executor request failed")
        return json.dumps({
            "status": "error",
            "error": (
                "The isolated code executor is unavailable: "
                f"{type(exc).__name__}: {exc}"
            ),
        }, ensure_ascii=False)


EXECUTE_CODE_SCHEMA = {
    "name": "execute_code",
    "description": (
        "Run Python code in a dedicated, ephemeral container with networking "
        "disabled. Use it for calculations and pure data processing. It receives "
        "a read-only snapshot of the current session workspace under ./workspace "
        "and installed session skill resources under ./skills; do not use absolute "
        "host/container paths. It cannot reach web, internal APIs, MCP servers, or "
        "install packages at runtime. "
        f"Default timeout is {DEFAULT_TIMEOUT}s; maximum is {MAX_TIMEOUT}s."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source code to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum execution time in seconds (default {DEFAULT_TIMEOUT}).",
                "default": DEFAULT_TIMEOUT,
                "minimum": 1,
                "maximum": MAX_TIMEOUT,
            },
        },
        "required": ["code"],
    },
}
