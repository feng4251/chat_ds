"""Path security — shared sandbox path validation for file and code tools.

Provides a single ``validate_path()`` entry point that all file-system-facing
tools use to resolve and validate paths within the per-user per-session sandbox.

Defences:
  - Rejects absolute paths
  - Rejects ``..`` traversal (prefix check after realpath)
  - Rejects symlinks
  - Resolves through ``os.path.realpath()`` and re-validates the prefix
"""

from __future__ import annotations

import os
from pathlib import Path

SANDBOX_ROOT = Path("/nfs/temp/chat_ds")


def sandbox_dir(user_id: str, session_id: str, sub: str = "files") -> Path:
    """Return (and create) a sandbox sub-directory for a user+session.

    Args:
        user_id: User identifier.
        session_id: Session identifier.
        sub: Sub-directory name within the session dir (e.g. "files", "sandbox").

    Returns:
        Resolved Path to the created directory.
    """
    d = (SANDBOX_ROOT / user_id / session_id / sub).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_path(
    filepath: str,
    user_id: str,
    session_id: str,
    sub: str = "files",
    *,
    must_exist: bool = False,
) -> Path:
    """Validate and resolve a relative path within the user+session sandbox.

    Steps:
      1. Reject absolute paths.
      2. Reject empty / whitespace-only paths.
      3. Join with the sandbox root and call ``os.path.realpath()`` to resolve
         symlinks and ``..`` components.
      4. Verify the resolved path is still prefixed by the sandbox root.
      5. Optionally verify the path exists on disk.

    Args:
        filepath: Relative path to validate.
        user_id: User identifier.
        session_id: Session identifier.
        sub: Sandbox sub-directory ("files", "sandbox", "browser", "results").
        must_exist: If True, raise when the path does not exist.

    Returns:
        Resolved absolute ``Path`` inside the sandbox.

    Raises:
        ValueError: For any security policy violation.
        FileNotFoundError: When *must_exist* is True and the path is absent.
    """
    if not filepath or not filepath.strip():
        raise ValueError("Empty path is not allowed.")

    if os.path.isabs(filepath):
        raise ValueError("Absolute paths are not allowed. Use a relative path.")

    path_obj = Path(filepath)
    if ".." in path_obj.parts:
        raise ValueError(f"Path traversal detected: {filepath}")

    # Resolve the sandbox root (defence against the root itself being a symlink)
    root = sandbox_dir(user_id, session_id, sub)

    # Reject every existing symlink in the lexical path before realpath()
    # dereferences it. Checking only the resolved leaf misses symlink parents.
    current = root
    for part in path_obj.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Symlinks are not allowed: {filepath}")

    # Join and resolve through realpath to catch symlinks and traversal
    resolved = os.path.realpath(str(root / filepath))

    # Component-aware containment check avoids /root vs /root-evil confusion.
    path = Path(resolved)
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError(f"Path traversal detected: {filepath}")

    if must_exist and not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    return path
