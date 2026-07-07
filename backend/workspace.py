"""Session workspace lifecycle and safe context-file access."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path


WORKSPACE_ROOT = Path("/nfs/temp/chat_ds")
MAX_CONTEXT_CHARS = 20_000
MAX_WORKSPACE_FILE_CHARS = 200_000

BOOTSTRAP_FILES: dict[str, str] = {
    "AGENTS.md": """# Session Instructions

This file contains durable operating instructions for this conversation.
Keep rules concrete, scoped to this workspace, and free of secrets.
""",
    "SOUL.md": """# Communication Style

Be clear, direct, and useful. Match the user's language.
""",
    "USER.md": """# User Context

Record only durable user preferences that are relevant to this session.
""",
    "TOOLS.md": """# Tool Conventions

- Work only inside this session workspace.
- Verify changes before reporting completion.
- Never store credentials in workspace files.
""",
    "MEMORY.md": """# Session Memory

Durable decisions and facts for this workspace can be summarized here.
""",
}

_CONTEXT_PRIORITY = (
    "SOUL.md",
    "AGENTS.md",
    "USER.md",
    "TOOLS.md",
    "MEMORY.md",
    ".hermes.md",
    "HERMES.md",
    "CLAUDE.md",
    ".cursorrules",
)

_THREAT_PATTERNS = (
    (re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions", re.I), "instruction_override"),
    (re.compile(r"disregard\s+(?:your|all|any)\s+(?:rules|instructions|guidelines)", re.I), "instruction_override"),
    (re.compile(r"do\s+not\s+tell\s+the\s+user", re.I), "deception"),
    (re.compile(r"system\s+prompt\s+override", re.I), "system_override"),
    (re.compile(r"curl[^\n]*(?:\$\w*(?:KEY|TOKEN|SECRET)|\.env)", re.I), "credential_exfiltration"),
    (re.compile(r"cat\s+[^\n]*(?:\.env|credentials|\.netrc|\.pgpass)", re.I), "secret_access"),
    (re.compile(r"<(?:div|span)[^>]*(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.I), "hidden_content"),
)
_INVISIBLE = {"\u200b", "\u200c", "\u2060", "\ufeff", "\u202a", "\u202b", "\u202d", "\u202e"}
_TRAJECTORY_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*:\s*(?:bearer|token)\s+)[^\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[\"']?)[^,\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", re.I), "[REDACTED_IMAGE_DATA]"),
)


def session_root(user_id: str, session_id: str) -> Path:
    root = (WORKSPACE_ROOT / user_id / session_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_dir(user_id: str, session_id: str, *, create: bool = True) -> Path:
    root = session_root(user_id, session_id) / "workspace"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_workspace(user_id: str, session_id: str) -> Path:
    root = workspace_dir(user_id, session_id)
    for filename, content in BOOTSTRAP_FILES.items():
        path = root / filename
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            ".env\n.env.*\n!.env.example\n*.key\n*.pem\nsecrets*\n",
            encoding="utf-8",
        )
    return root


def safe_workspace_path(
    user_id: str,
    session_id: str,
    relative_path: str,
    *,
    must_exist: bool = False,
) -> Path:
    if not relative_path or not relative_path.strip():
        raise ValueError("Path cannot be empty.")
    path_obj = Path(relative_path)
    if path_obj.is_absolute():
        raise ValueError("Absolute paths are not allowed.")
    if ".." in path_obj.parts:
        raise ValueError("Path traversal is not allowed.")
    root = ensure_workspace(user_id, session_id).resolve()
    current = root
    for part in path_obj.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinks are not allowed.")
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Path traversal is not allowed.") from exc
    if must_exist and not candidate.exists():
        raise FileNotFoundError(relative_path)
    return candidate


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".chat_ds_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".chat_ds_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def list_workspace_files(user_id: str, session_id: str) -> list[dict]:
    root = ensure_workspace(user_id, session_id).resolve()
    files: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = str(path.relative_to(root))
        stat = path.stat()
        files.append({"path": rel, "size": stat.st_size, "updated_at": stat.st_mtime})
    return files


def scan_context_content(content: str, filename: str) -> str:
    for char in _INVISIBLE:
        if char in content:
            return f"[BLOCKED: {filename} contains invisible control characters.]"
    for pattern, threat in _THREAT_PATTERNS:
        if pattern.search(content):
            return f"[BLOCKED: {filename} matched context security rule '{threat}'.]"
    if len(content) <= MAX_CONTEXT_CHARS:
        return content
    head = int(MAX_CONTEXT_CHARS * 0.7)
    tail = int(MAX_CONTEXT_CHARS * 0.2)
    return (
        content[:head]
        + f"\n\n[...truncated {filename}: {len(content)} chars total...]\n\n"
        + content[-tail:]
    )


def build_workspace_context(user_id: str, session_id: str) -> str:
    root = ensure_workspace(user_id, session_id)
    sections: list[str] = []
    seen: set[str] = set()
    for filename in _CONTEXT_PRIORITY:
        path = root / filename
        if filename in seen or not path.is_file() or path.is_symlink():
            continue
        seen.add(filename)
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if content:
            sections.append(
                f"## {filename}\n\n{scan_context_content(content, filename)}"
            )
    if not sections:
        return ""
    return (
        "# Session Workspace Context\n\n"
        "The following files are user-owned workspace context. Follow them when "
        "they do not conflict with higher-priority instructions.\n\n"
        + "\n\n".join(sections)
    )


def clone_session_workspace(
    user_id: str,
    source_session_id: str,
    target_session_id: str,
) -> None:
    source = workspace_dir(user_id, source_session_id, create=False)
    target = workspace_dir(user_id, target_session_id, create=False)
    if source.exists():
        shutil.copytree(source, target, dirs_exist_ok=True)
    ensure_workspace(user_id, target_session_id)


def serialize_json_list(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return list(default)
    return [str(item) for item in parsed] if isinstance(parsed, list) else list(default)


def redact_trajectory_value(value):
    if isinstance(value, str):
        redacted = value
        for pattern, replacement in _TRAJECTORY_SECRET_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    if isinstance(value, list):
        return [redact_trajectory_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if key.lower() in {
                    "api_key", "apikey", "access_token", "token",
                    "secret", "password", "authorization",
                }
                else redact_trajectory_value(item)
            )
            for key, item in value.items()
        }
    return value
