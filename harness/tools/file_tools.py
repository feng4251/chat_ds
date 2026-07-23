"""File tools — sandboxed file read/write/search operations.

Each user+session gets an isolated workspace directory under
/nfs/temp/chat_ds/<user_id>/<session_id>/workspace/.
Path traversal attacks (..) are rejected. Operations:
  - read_file: read file content with optional offset/limit
  - write_file: write/overwrite a file
  - merge_files: atomically concatenate workspace files
  - search_files: search file contents (ripgrep) or find files by name (glob)
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from tools.path_security import sandbox_dir, validate_path
from tools.approval import check_file_write_safety
from tools.omission_guard import (
    compacted_history_omission_error,
    contains_compacted_history_omission,
)
from tools.workspace_lock import workspace_mutation_guard

logger = logging.getLogger(__name__)

SANDBOX_BASE = Path("/nfs/temp/chat_ds")
DEFAULT_MAX_READ_CHARS = 100_000
DEFAULT_SEARCH_LIMIT = 50


# ── Path resolution (delegates to shared path_security) ─────────────────

def _sandbox_dir(user_id: str, session_id: str) -> Path:
    """Return (and create) the files sandbox directory for a user+session."""
    return sandbox_dir(user_id, session_id, sub="workspace")


def _make_workspace_path_readable(path: Path) -> None:
    try:
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.parent.chmod(0o755)
            path.chmod(0o644)
    except OSError:
        pass


def _resolve(filepath: str, user_id: str, session_id: str) -> Path:
    """Resolve a relative path within the user+session sandbox."""
    clean = str(filepath or "")
    if clean.startswith("workspace/"):
        clean = clean[len("workspace/"):]
    return validate_path(clean, user_id, session_id, sub="workspace")


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
    workspace = _sandbox_dir(user_id, session_id).resolve()
    with workspace_mutation_guard(workspace):
        return await _write_file_locked(
            filepath,
            content,
            user_id=user_id,
            session_id=session_id,
        )


async def _write_file_locked(
    filepath: str,
    content: str,
    user_id: str,
    session_id: str,
) -> str:
    """Perform one write while the session workspace mutation lock is held."""

    try:
        path = _resolve(filepath, user_id, session_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    # Additional safety check after path validation
    safety_err = check_file_write_safety(str(path))
    if safety_err:
        return json.dumps({"error": safety_err})
    if content == "" and path.exists():
        try:
            existing_size = path.stat().st_size
        except OSError:
            existing_size = 0
        if existing_size > 0:
            return json.dumps({
                "error": "Refusing to overwrite a non-empty existing file with empty content. Use patch_file or provide the full replacement content.",
                "reason": "empty_overwrite_blocked",
                "existing_size": existing_size,
            }, ensure_ascii=False)

    temp_path: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after parent creation so a concurrently introduced link
        # cannot redirect the final rename.
        path = _resolve(filepath, user_id, session_id)
        descriptor, temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".write.tmp",
            dir=str(path.parent),
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        temp_path = None
        _make_workspace_path_readable(path)
        return json.dumps({
            "status": "written",
            "path": filepath,
            "size": len(content),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Cannot write file: {e}"})
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


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
    workspace = _sandbox_dir(user_id, session_id).resolve()
    with workspace_mutation_guard(workspace):
        return await _patch_file_locked(
            filepath,
            old_text,
            new_text,
            replace_all=replace_all,
            user_id=user_id,
            session_id=session_id,
        )


async def _patch_file_locked(
    filepath: str,
    old_text: str,
    new_text: str,
    *,
    replace_all: bool,
    user_id: str,
    session_id: str,
) -> str:
    """Read/CAS-replace one file while the workspace lock is held."""

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
        _make_workspace_path_readable(path)
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


def _normalize_merge_pattern(pattern: str) -> str:
    """Return a workspace-relative POSIX glob pattern or raise ValueError."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("Merge glob patterns must be non-empty strings.")
    clean = pattern
    if clean.startswith("workspace/"):
        clean = clean[len("workspace/"):]
    if not clean or clean.startswith("/"):
        raise ValueError(f"Absolute paths are not allowed in merge patterns: {pattern}")
    parts = clean.split("/")
    if ".." in parts:
        raise ValueError(f"Path traversal detected in merge pattern: {pattern}")
    if "\x00" in clean:
        raise ValueError("NUL bytes are not allowed in merge patterns.")
    return clean


def _matches_merge_pattern(relative_path: str, pattern: str) -> bool:
    """Match a POSIX workspace path against a glob with recursive ``**``."""
    path_parts = tuple(part for part in relative_path.split("/") if part not in {"", "."})
    pattern_parts = tuple(part for part in pattern.split("/") if part not in {"", "."})
    memo: dict[tuple[int, int], bool] = {}

    def _match(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = _match(pattern_index + 1, path_index) or (
                path_index < len(path_parts)
                and _match(pattern_index, path_index + 1)
            )
        else:
            result = (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], pattern_parts[pattern_index])
                and _match(pattern_index + 1, path_index + 1)
            )
        memo[key] = result
        return result

    return _match(0, 0)


def _workspace_file_candidates(workspace: Path) -> list[tuple[str, Path, bool]]:
    """List workspace files without following directory symlinks."""
    candidates: list[tuple[str, Path, bool]] = []
    for current, dirnames, filenames in os.walk(workspace, topdown=True, followlinks=False):
        current_path = Path(current)
        dirnames[:] = sorted(
            name for name in dirnames
            if not (current_path / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = current_path / filename
            relative = path.relative_to(workspace).as_posix()
            candidates.append((relative, path, path.is_symlink()))
    return sorted(candidates, key=lambda item: item[0])


def _text_file_summary(path: Path) -> dict:
    """Return bounded, deterministic acceptance metadata for a text file."""
    lines = 0
    first_nonempty: str | None = None
    last_nonempty: str | None = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            lines += 1
            stripped = line.strip()
            if stripped:
                if first_nonempty is None:
                    first_nonempty = stripped
                last_nonempty = stripped

    max_metadata_chars = 2_000

    def _bounded(value: str | None) -> tuple[str | None, bool]:
        if value is None or len(value) <= max_metadata_chars:
            return value, False
        return value[:max_metadata_chars] + "…", True

    first_value, first_truncated = _bounded(first_nonempty)
    last_value, last_truncated = _bounded(last_nonempty)
    return {
        "bytes": path.stat().st_size,
        "lines": lines,
        "first_nonempty_line": first_value,
        "last_nonempty_line": last_value,
        "first_nonempty_line_chars": len(first_nonempty or ""),
        "last_nonempty_line_chars": len(last_nonempty or ""),
        "metadata_truncated": first_truncated or last_truncated,
    }


async def merge_files(
    output_filepath: str,
    input_files: list[str] | None = None,
    patterns: list[str] | None = None,
    separator: str = "",
    user_id: str = "default",
    session_id: str = "default",
) -> str:
    """Atomically concatenate workspace files in a deterministic order.

    Explicit ``input_files`` retain caller order. Each glob pattern is then
    expanded in lexicographic workspace-relative order. Files selected more
    than once are included only at their first occurrence.
    """
    workspace = _sandbox_dir(user_id, session_id).resolve()
    with workspace_mutation_guard(workspace):
        return await _merge_files_locked(
            output_filepath,
            input_files=input_files,
            patterns=patterns,
            separator=separator,
            user_id=user_id,
            session_id=session_id,
        )


async def _merge_files_locked(
    output_filepath: str,
    input_files: list[str] | None,
    patterns: list[str] | None,
    separator: str,
    user_id: str,
    session_id: str,
) -> str:
    """Read inputs and atomically replace output under the workspace lock."""

    if input_files is not None and not isinstance(input_files, list):
        return json.dumps({"error": "input_files must be an array of workspace paths."})
    if patterns is not None and not isinstance(patterns, list):
        return json.dumps({"error": "patterns must be an array of workspace glob patterns."})
    if not isinstance(separator, str):
        return json.dumps({"error": "separator must be a string."})
    if not input_files and not patterns:
        return json.dumps({
            "error": "Provide at least one input file or glob pattern.",
            "reason": "missing_inputs",
        })
    if any(not isinstance(item, str) for item in (input_files or [])):
        return json.dumps({"error": "Every input_files item must be a string."})
    if any(not isinstance(item, str) for item in (patterns or [])):
        return json.dumps({"error": "Every patterns item must be a string."})

    try:
        output_path = _resolve(output_filepath, user_id, session_id)
        workspace = _sandbox_dir(user_id, session_id).resolve()
        output_relative = output_path.relative_to(workspace).as_posix()
        normalized_patterns = [
            _normalize_merge_pattern(pattern)
            for pattern in (patterns or [])
        ]
    except (ValueError, OSError) as exc:
        return json.dumps({"error": str(exc)})

    if output_path.exists() and output_path.is_dir():
        return json.dumps({"error": f"Output path is a directory: {output_filepath}"})
    safety_err = check_file_write_safety(str(output_path))
    if safety_err:
        return json.dumps({"error": safety_err})

    matching_output_patterns = [
        pattern
        for pattern in normalized_patterns
        if _matches_merge_pattern(output_relative, pattern)
    ]
    if matching_output_patterns:
        return json.dumps({
            "error": (
                "Output path matches an input glob pattern and could be merged "
                "into itself on this or a later run."
            ),
            "reason": "output_matches_input_pattern",
            "output_filepath": output_relative,
            "matching_patterns": matching_output_patterns,
        }, ensure_ascii=False)

    selected: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    def _add_input(relative: str, path: Path) -> None:
        canonical = path.resolve()
        if canonical == output_path:
            raise ValueError(f"Output file cannot also be an input: {relative}")
        if canonical not in seen:
            seen.add(canonical)
            selected.append((relative, canonical))

    try:
        for filepath in input_files or []:
            path = _resolve(filepath, user_id, session_id)
            if not path.exists():
                raise ValueError(f"Input file not found: {filepath}")
            if path.is_symlink():
                raise ValueError(f"Symlinks are not allowed: {filepath}")
            if not path.is_file():
                raise ValueError(f"Input is not a regular file: {filepath}")
            relative = path.relative_to(workspace).as_posix()
            _add_input(relative, path)

        if normalized_patterns:
            candidates = _workspace_file_candidates(workspace)
            for pattern in normalized_patterns:
                matched = 0
                for relative, candidate, is_symlink in candidates:
                    if not _matches_merge_pattern(relative, pattern):
                        continue
                    if is_symlink:
                        raise ValueError(
                            f"Glob pattern matched a symlink, which is not allowed: {relative}"
                        )
                    resolved = _resolve(relative, user_id, session_id)
                    if not resolved.is_file():
                        continue
                    _add_input(relative, resolved)
                    matched += 1
                if matched == 0:
                    raise ValueError(f"Merge glob pattern matched no files: {pattern}")
    except (ValueError, OSError) as exc:
        return json.dumps({"error": str(exc)})

    if not selected:
        return json.dumps({
            "error": "No regular workspace files were selected for merging.",
            "reason": "no_matching_inputs",
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    input_metadata: list[dict] = []
    separator_bytes = separator.encode("utf-8")
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".merge_", dir=str(output_path.parent))
        with os.fdopen(fd, "wb") as destination:
            for index, (relative, source_path) in enumerate(selected):
                if index:
                    destination.write(separator_bytes)

                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                source_fd = os.open(source_path, flags)
                source_bytes = 0
                line_break_count = 0
                last_byte = b""
                previous_ended_cr = False
                with os.fdopen(source_fd, "rb") as source:
                    source_stat = os.fstat(source.fileno())
                    if not stat.S_ISREG(source_stat.st_mode):
                        raise ValueError(f"Input is not a regular file: {relative}")
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        destination.write(chunk)
                        source_bytes += len(chunk)
                        line_break_count += (
                            chunk.count(b"\n")
                            + chunk.count(b"\r")
                            - chunk.count(b"\r\n")
                        )
                        if previous_ended_cr and chunk.startswith(b"\n"):
                            line_break_count -= 1
                        previous_ended_cr = chunk.endswith(b"\r")
                        last_byte = chunk[-1:]
                input_metadata.append({
                    "path": relative,
                    "bytes": source_bytes,
                    "lines": line_break_count + (
                        1 if source_bytes and last_byte not in {b"\n", b"\r"} else 0
                    ),
                })
            destination.flush()
            os.fsync(destination.fileno())

        summary = _text_file_summary(Path(temp_path))
        os.replace(temp_path, output_path)
        temp_path = None
        _make_workspace_path_readable(output_path)
    except Exception as exc:
        return json.dumps({"error": f"Cannot merge files: {exc}"})
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    return json.dumps({
        "status": "merged",
        "path": output_relative,
        "input_count": len(selected),
        "input_files": [relative for relative, _ in selected],
        "inputs": input_metadata,
        "separator_chars": len(separator),
        "separator_bytes": len(separator_bytes),
        **summary,
    }, ensure_ascii=False)


async def search_files(
    pattern: str,
    target: str = "content",
    path: str = ".",
    file_glob: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
    output_mode: str = "content",
    context_lines: int = 0,
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
        context_lines: Lines of context around matches (content mode only).
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
        pattern, search_dir, sandbox, file_glob, limit, offset, output_mode, context_lines
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

MERGE_FILES_SCHEMA = {
    "name": "merge_files",
    "description": (
        "Safely and atomically concatenate text files inside the session workspace. "
        "Use this instead of generating a large merge script or assuming a shell/cat tool. "
        "Provide explicit input_files, glob patterns, or both. Explicit files retain their "
        "given order; every pattern expands in lexicographic order; overlapping matches are "
        "deduplicated. Each pattern must match at least one file. The output path must not be "
        "an explicit input or match any input pattern, which prevents recursive self-merges. "
        "Returns acceptance metadata including selected inputs, bytes, lines, and first/last "
        "non-empty output lines."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "output_filepath": {
                "type": "string",
                "description": "Relative workspace path for the atomically replaced output file.",
            },
            "input_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional ordered list of relative workspace input files.",
            },
            "patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional workspace-relative glob patterns. Supports recursive **. "
                    "Matches are appended in lexicographic order."
                ),
            },
            "separator": {
                "type": "string",
                "description": "Text inserted between adjacent input files. Defaults to empty.",
                "default": "",
            },
        },
        "required": ["output_filepath"],
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
            "context_lines": {
                "type": "integer",
                "description": "Lines of context around matches for content search.",
                "default": 0,
            },
        },
        "required": ["pattern"],
    },
}
