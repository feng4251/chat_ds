"""File tools — sandboxed file read/write/search operations.

Each user+session gets an isolated workspace directory under
/nfs/temp/chat_ds/<user_id>/<session_id>/workspace/.
Path traversal attacks (..) are rejected. Operations:
  - read_file: read file content with optional offset/limit
  - write_file: write/overwrite a file
  - search_files: search file contents (ripgrep) or find files by name (glob)
"""

from __future__ import annotations

import json
import logging
import subprocess
import os
import tempfile
from pathlib import Path

from tools.path_security import sandbox_dir, validate_path
from tools.approval import check_file_write_safety
from tools.omission_guard import (
    compacted_history_omission_error,
    contains_compacted_history_omission,
)

logger = logging.getLogger(__name__)

SANDBOX_BASE = Path("/nfs/temp/chat_ds")
DEFAULT_MAX_READ_CHARS = 100_000
DEFAULT_SEARCH_LIMIT = 50


# ── Path resolution (delegates to shared path_security) ─────────────────

def _sandbox_dir(user_id: str, session_id: str) -> Path:
    """Return (and create) the files sandbox directory for a user+session."""
    return sandbox_dir(user_id, session_id, sub="workspace")


def _resolve(filepath: str, user_id: str, session_id: str) -> Path:
    """Resolve a relative path within the user+session sandbox."""
    return validate_path(filepath, user_id, session_id, sub="workspace")


# ── Tool handlers ──────────────────────────────────────────────────────────

async def read_file(
    filepath: str,
    offset: int = 1,
    limit: int = 500,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Read a file from the user+session sandbox. Lines are 1-indexed.

    Args:
        filepath: Relative path within the sandbox.
        offset: Starting line number (1-indexed, default 1).
        limit: Maximum lines to return (default 500).
        user_id: User identifier for sandbox isolation.
        session_id: Session identifier for sandbox isolation.
    """
    try:
        if filepath.startswith("results/"):
            path = validate_path(filepath[len("results/"):], user_id, session_id, sub="results")
        else:
            path = _resolve(filepath, user_id, session_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    if not path.exists():
        hint = (
            "Use skill_view(name, file_path=...) for files inside an installed skill. "
            "read_file reads workspace files and persisted tool results under results/."
        )
        return json.dumps({"error": f"File not found: {filepath}", "hint": hint})
    if not path.is_file():
        return json.dumps({"error": f"Not a regular file: {filepath}"})
    if path.is_symlink():
        return json.dumps({"error": f"Symlinks are not allowed: {filepath}"})

    # Check file size — reject very large files
    try:
        size = path.stat().st_size
        if size > 10 * 1024 * 1024:  # 10 MB
            return json.dumps({
                "error": f"File too large ({size} bytes). Use offset/limit to read sections."
            })
    except OSError as e:
        return json.dumps({"error": f"Cannot stat file: {e}"})

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return json.dumps({"error": f"Cannot read file: {e}"})

    lines = content.splitlines()
    total_lines = len(lines)

    # Validate offset/limit
    if offset < 1:
        offset = 1
    if limit < 1:
        limit = 500

    start = offset - 1
    if start >= total_lines:
        return json.dumps({
            "content": "",
            "total_lines": total_lines,
            "offset": offset,
            "limit": limit,
        })

    end = min(start + limit, total_lines)
    selected = lines[start:end]
    result_content = "\n".join(selected)

    # Truncate to max chars
    if len(result_content) > DEFAULT_MAX_READ_CHARS:
        result_content = result_content[:DEFAULT_MAX_READ_CHARS] + "\n... [truncated]"

    resp = {
        "content": result_content,
        "total_lines": total_lines,
        "offset": offset,
        "limit": limit,
    }

    # Hint for large files
    if total_lines > limit and size > 50_000:
        resp["hint"] = f"File has {total_lines} lines. Use offset/limit to read more."

    return json.dumps(resp, ensure_ascii=False)


async def write_file(
    filepath: str,
    content: str,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Write content to a file in the user+session sandbox. Always overwrites.

    Args:
        filepath: Relative path within the sandbox.
        content: Text content to write.
        user_id: User identifier for sandbox isolation.
        session_id: Session identifier for sandbox isolation.
    """
    if not isinstance(content, str):
        return json.dumps({"error": "content must be a string"})
    if contains_compacted_history_omission(content):
        return json.dumps(compacted_history_omission_error("content"), ensure_ascii=False)

    try:
        path = _resolve(filepath, user_id, session_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    # Additional safety check after path validation
    safety_err = check_file_write_safety(str(path))
    if safety_err:
        return json.dumps({"error": safety_err})

    # Create parent directories
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        path.write_text(content, encoding="utf-8")
        return json.dumps({
            "status": "written",
            "path": filepath,
            "size": len(content),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Cannot write file: {e}"})


async def patch_file(
    filepath: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Apply an exact, atomic text replacement within a workspace file."""
    if not old_text:
        return json.dumps({"error": "old_text cannot be empty"})
    if contains_compacted_history_omission(old_text):
        return json.dumps(compacted_history_omission_error("old_text"), ensure_ascii=False)
    if contains_compacted_history_omission(new_text):
        return json.dumps(compacted_history_omission_error("new_text"), ensure_ascii=False)
    try:
        path = _resolve(filepath, user_id, session_id)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    if not path.is_file() or path.is_symlink():
        return json.dumps({"error": f"File not found: {filepath}"})
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        return json.dumps({"error": f"Cannot read file: {exc}"})
    count = content.count(old_text)
    if count == 0:
        return json.dumps({
            "error": "old_text was not found exactly",
            "hint": "Read the current file and retry with an exact substring.",
        })
    if count > 1 and not replace_all:
        return json.dumps({
            "error": f"old_text matched {count} locations",
            "hint": "Provide a more specific substring or set replace_all=true.",
        })
    updated = content.replace(old_text, new_text, -1 if replace_all else 1)
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".patch_", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception as exc:
        try:
            os.unlink(temp_path)
        except (OSError, UnboundLocalError):
            pass
        return json.dumps({"error": f"Cannot patch file: {exc}"})
    return json.dumps({
        "status": "patched",
        "path": filepath,
        "replacements": count if replace_all else 1,
        "size": len(updated),
    }, ensure_ascii=False)


async def search_files(
    pattern: str,
    target: str = "content",
    path: str = ".",
    file_glob: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
    output_mode: str = "content",
    context: int = 0,
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Search file contents (ripgrep) or find files by name (glob).

    Args:
        pattern: Search pattern (regex for content, glob for files).
        target: "content" to grep inside files, "files" to find by name.
        path: Relative directory within the sandbox to search.
        file_glob: Optional glob to filter which files to search (content mode only).
        limit: Maximum results (default 50).
        offset: Skip first N results (default 0).
        output_mode: "content" for matching lines, "files_with_matches" for file paths.
        context: Lines of context around matches (content mode only).
        user_id: User identifier for sandbox isolation.
        session_id: Session identifier for sandbox isolation.
    """
    try:
        sandbox = _sandbox_dir(user_id, session_id).resolve()
        search_dir = (sandbox / path).resolve()
        try:
            search_dir.relative_to(sandbox)
        except ValueError:
            return json.dumps({"error": f"Path traversal detected: {path}"})
    except ValueError as e:
        return json.dumps({"error": str(e)})

    if not search_dir.exists():
        return json.dumps({"error": f"Directory not found: {path}"})

    if target == "files":
        return _search_filenames(pattern, search_dir, sandbox, limit, offset)

    return await _search_content(
        pattern, search_dir, sandbox, file_glob, limit, offset, output_mode, context
    )


def _search_filenames(
    pattern: str,
    search_dir: Path,
    sandbox: Path,
    limit: int,
    offset: int,
) -> str:
    """Find files by glob pattern."""
    try:
        results = sorted(search_dir.rglob(pattern))
    except Exception as e:
        return json.dumps({"error": f"Glob error: {e}"})

    # Filter to regular files only, no symlinks
    results = [
        p for p in results
        if p.is_file() and not p.is_symlink()
    ]

    total = len(results)
    results = results[offset:offset + limit]

    matches = [
        {"path": str(r.relative_to(sandbox)), "size": r.stat().st_size}
        for r in results
    ]

    return json.dumps({
        "matches": matches,
        "total": total,
        "offset": offset,
        "limit": limit,
    }, ensure_ascii=False)


async def _search_content(
    pattern: str,
    search_dir: Path,
    sandbox: Path,
    file_glob: str | None,
    limit: int,
    offset: int,
    output_mode: str,
    context: int,
) -> str:
    """Search file contents using ripgrep (rg) or fallback to Python grep."""
    args = ["rg", "--no-heading", "--with-filename", "--line-number",
            "--no-messages", "--no-ignore-parent", "--no-config",
            "--max-count=500"]

    if output_mode == "files_with_matches":
        args.append("--files-with-matches")
    elif context > 0:
        args.extend(["-C", str(context)])

    if file_glob:
        args.extend(["--glob", file_glob])

    args.extend(["--", pattern, str(search_dir)])

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=15,
            errors="replace",
        )
        output = proc.stdout
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Search timed out"})
    except FileNotFoundError:
        # ripgrep not installed — fall back to Python
        return _search_content_python(
            pattern, search_dir, sandbox, file_glob, limit, offset, output_mode, context
        )
    except Exception as e:
        return json.dumps({"error": f"Search error: {e}"})

    if proc.returncode == 1:
        # rg returns 1 for "no matches"
        return json.dumps({"matches": [], "total": 0}, ensure_ascii=False)
    if proc.returncode != 0:
        return json.dumps({"error": proc.stderr[:500]})

    lines = output.strip().splitlines() if output.strip() else []
    total = len(lines)
    lines = lines[offset:offset + limit]

    matches = []
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) >= 3:
            filepath = parts[0]
            lineno = parts[1]
            text = parts[2].strip()
            # Make paths relative to sandbox
            try:
                rel_path = str(Path(filepath).resolve().relative_to(sandbox))
            except ValueError:
                rel_path = filepath
            matches.append({
                "file": rel_path,
                "line": int(lineno),
                "content": text[:500],
            })

    return json.dumps({
        "matches": matches,
        "total": total,
        "offset": offset,
        "limit": limit,
    }, ensure_ascii=False)


def _search_content_python(
    pattern: str,
    search_dir: Path,
    sandbox: Path,
    file_glob: str | None,
    limit: int,
    offset: int,
    output_mode: str,
    context: int,
) -> str:
    """Fallback grep using Python re when ripgrep is not available."""
    import re
    import fnmatch

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return json.dumps({"error": f"Invalid regex pattern: {e}"})

    results = []
    text_extensions = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
        ".toml", ".cfg", ".ini", ".md", ".txt", ".rst", ".html", ".css",
        ".xml", ".csv", ".sh", ".bash", ".zsh", ".fish", ".sql", ".rs",
        ".go", ".java", ".c", ".h", ".cpp", ".hpp", ".rb", ".php",
        ".vue", ".svelte", ".env", ".gitignore", ".dockerignore",
        ".Makefile", ".makefile",
    }

    for filepath in search_dir.rglob("*"):
        if not filepath.is_file() or filepath.is_symlink():
            continue
        # Respect file_glob
        if file_glob and not fnmatch.fnmatch(filepath.name, file_glob):
            continue
        # Skip binary-looking extensions
        if filepath.suffix and filepath.suffix not in text_extensions:
            continue
        # Skip hidden directories
        if any(part.startswith(".") for part in filepath.parts[:-1]):
            continue

        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        if output_mode == "files_with_matches":
            if regex.search(content):
                try:
                    rel = str(filepath.resolve().relative_to(sandbox))
                except ValueError:
                    rel = str(filepath.relative_to(search_dir))
                results.append({"file": rel})
            continue

        for i, line in enumerate(content.splitlines(), 1):
            if regex.search(line):
                try:
                    rel = str(filepath.resolve().relative_to(sandbox))
                except ValueError:
                    rel = str(filepath.relative_to(search_dir))
                results.append({
                    "file": rel,
                    "line": i,
                    "content": line[:500],
                })

    total = len(results)
    results = results[offset:offset + limit]

    return json.dumps({
        "matches": results,
        "total": total,
        "offset": offset,
        "limit": limit,
    }, ensure_ascii=False)


READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": (
        "Read a file from the session workspace sandbox, or a persisted tool result under results/. "
        "This does not read files inside installed skills; use skill_view(name, file_path=...) for skill resources. "
        "Lines are 1-indexed. Use offset/limit to read large workspace files in sections.\n\n"
        "Files up to 10MB are supported. Returns the content with metadata "
        "including total line count."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": "Relative path within the session workspace, or results/<file> for persisted tool output; not an installed skill directory.",
            },
            "offset": {
                "type": "integer",
                "description": "Starting line number (1-indexed, default 1).",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum lines to return (default 500).",
                "default": 500,
            },
        },
        "required": ["filepath"],
    },
}

WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": (
        "Write or overwrite a file in the session sandbox. Parent directories are created "
        "automatically and existing files are overwritten. Never call write_file with empty "
        "args, _raw_args, or malformed JSON; required fields are filepath and content. For a "
        "long requested deliverable, write the complete artifact content, not a placeholder "
        "or stub. For one requested deliverable, prefer one complete file unless the user "
        "explicitly asks for multiple files; do not split chapters into many scratch files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": "Relative path within the sandbox to write.",
            },
            "content": {
                "type": "string",
                "description": "Text content to write to the file.",
            },
        },
        "required": ["filepath", "content"],
    },
}

PATCH_FILE_SCHEMA = {
    "name": "patch_file",
    "description": (
        "Atomically replace an exact text block in a session workspace file. "
        "Never call patch_file with empty args. Required arguments are filepath, "
        "old_text, and new_text. Use read_file first and provide enough surrounding "
        "text to make old_text unique."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filepath": {"type": "string", "description": "Relative workspace path."},
            "old_text": {"type": "string", "description": "Exact text to replace."},
            "new_text": {"type": "string", "description": "Replacement text."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace all exact matches instead of requiring one unique match.",
                "default": False,
            },
        },
        "required": ["filepath", "old_text", "new_text"],
    },
}

SEARCH_FILES_SCHEMA = {
    "name": "search_files",
    "description": (
        "Search file contents (ripgrep/regex) or find files by name (glob) "
        "within the session workspace sandbox only. This does not search installed "
        "skill directories; use skill_view(name, file_path='__manifest__') and "
        "skill_view(name, file_path=...) for skill resources.\n\n"
        "Content search (target='content'): regex pattern matching across "
        "workspace text files with optional file_glob filter.\n"
        "File search (target='files'): glob pattern to find workspace files by name.\n\n"
        "Use file_glob to restrict search to specific file types "
        "(e.g., '*.py', '*.md')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Search pattern — regex for content, glob for files.",
            },
            "target": {
                "type": "string",
                "enum": ["content", "files"],
                "description": "'content' to grep inside files, 'files' to find by name.",
                "default": "content",
            },
            "path": {
                "type": "string",
                "description": "Relative workspace directory to search (default '.').",
                "default": ".",
            },
            "file_glob": {
                "type": "string",
                "description": "Optional glob to filter files (content mode only, e.g. '*.py').",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results (default 50).",
                "default": 50,
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches"],
                "description": "'content' for matching lines, 'files_with_matches' for file paths.",
                "default": "content",
            },
        },
        "required": ["pattern"],
    },
}
