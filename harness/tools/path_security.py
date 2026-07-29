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
import stat
from pathlib import Path

from tools.workspace_lock import (
    WorkspaceMutationLockError,
    require_session_workspace_active,
)

SANDBOX_ROOT = Path("/nfs/temp/chat_ds")


def _safe_component(value: str, *, field: str) -> str:
    text = str(value)
    if (
        not text
        or text in {".", ".."}
        or "\x00" in text
        or Path(text).name != text
        or len(os.fsencode(text)) > 255
    ):
        raise ValueError(f"Unsafe {field}.")
    return text


def _safe_storage_root(base_root: Path) -> Path:
    candidate = Path(base_root)
    try:
        candidate_stat = candidate.lstat()
    except FileNotFoundError:
        parent = candidate.parent
        try:
            parent_stat = parent.lstat()
            parent_resolved = parent.resolve(strict=True)
            parent_resolved_stat = parent_resolved.stat()
        except (OSError, RuntimeError) as exc:
            raise WorkspaceMutationLockError(
                "Sandbox storage parent is unavailable.",
                code="workspace_lock_unsafe",
            ) from exc
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or stat.S_ISLNK(parent_stat.st_mode)
            or (parent_stat.st_dev, parent_stat.st_ino)
            != (parent_resolved_stat.st_dev, parent_resolved_stat.st_ino)
        ):
            raise WorkspaceMutationLockError(
                "Sandbox storage parent must be a stable real directory.",
                code="workspace_lock_unsafe",
            )
        parent_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            parent_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            parent_flags |= os.O_NOFOLLOW
        parent_descriptor = os.open(parent_resolved, parent_flags)
        try:
            os.mkdir(
                _safe_component(candidate.name, field="sandbox root"),
                mode=0o700,
                dir_fd=parent_descriptor,
            )
        except FileExistsError:
            pass
        finally:
            os.close(parent_descriptor)
        candidate_stat = candidate.lstat()
    except OSError as exc:
        raise WorkspaceMutationLockError(
            "Sandbox storage root is unavailable.",
            code="workspace_lock_unsafe",
        ) from exc
    try:
        resolved = candidate.resolve(strict=True)
        resolved_stat = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise WorkspaceMutationLockError(
            "Sandbox storage root cannot be resolved safely.",
            code="workspace_lock_unsafe",
        ) from exc
    if (
        not stat.S_ISDIR(candidate_stat.st_mode)
        or stat.S_ISLNK(candidate_stat.st_mode)
        or not stat.S_ISDIR(resolved_stat.st_mode)
        or (candidate_stat.st_dev, candidate_stat.st_ino)
        != (resolved_stat.st_dev, resolved_stat.st_ino)
    ):
        raise WorkspaceMutationLockError(
            "Sandbox storage root must be a stable real directory.",
            code="workspace_lock_unsafe",
        )
    return resolved


def safe_sandbox_dir(
    base_root: Path,
    user_id: str,
    session_id: str,
    sub: str = "files",
) -> Path:
    """Create a sandbox chain with mkdirat/openat and no symlink traversal."""

    root = _safe_storage_root(base_root)
    safe_user = _safe_component(user_id, field="user_id")
    safe_session = _safe_component(session_id, field="session_id")
    safe_sub = _safe_component(sub, field="sandbox subdirectory")
    session_workspace = root / safe_user / safe_session / "workspace"
    require_session_workspace_active(session_workspace)

    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    current = root
    try:
        for component in (safe_user, safe_session, safe_sub):
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            try:
                child_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise WorkspaceMutationLockError(
                    "Sandbox directory chain is unsafe or unavailable.",
                    code="workspace_lock_unsafe",
                ) from exc
            try:
                child_stat = os.fstat(child_descriptor)
                lexical = current / component
                lexical_stat = lexical.lstat()
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or not stat.S_ISDIR(lexical_stat.st_mode)
                    or stat.S_ISLNK(lexical_stat.st_mode)
                    or (child_stat.st_dev, child_stat.st_ino)
                    != (lexical_stat.st_dev, lexical_stat.st_ino)
                ):
                    raise WorkspaceMutationLockError(
                        "Sandbox directory changed during validation.",
                        code="workspace_lock_unsafe",
                    )
            except BaseException:
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
            current = lexical

        final_stat = os.fstat(descriptor)
        resolved = current.resolve(strict=True)
        resolved_stat = resolved.stat()
        resolved.relative_to(root)
        if (
            (final_stat.st_dev, final_stat.st_ino)
            != (resolved_stat.st_dev, resolved_stat.st_ino)
        ):
            raise WorkspaceMutationLockError(
                "Sandbox directory identity changed after creation.",
                code="workspace_lock_unsafe",
            )
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, WorkspaceMutationLockError):
            raise
        raise WorkspaceMutationLockError(
            "Sandbox directory escaped its configured storage root.",
            code="workspace_lock_unsafe",
        ) from exc
    finally:
        os.close(descriptor)

    require_session_workspace_active(session_workspace)
    return current


def sandbox_dir(user_id: str, session_id: str, sub: str = "files") -> Path:
    """Return (and create) a sandbox sub-directory for a user+session.

    Args:
        user_id: User identifier.
        session_id: Session identifier.
        sub: Sub-directory name within the session dir (e.g. "files", "sandbox").

    Returns:
        Resolved Path to the created directory.
    """
    return safe_sandbox_dir(
        SANDBOX_ROOT,
        user_id,
        session_id,
        sub,
    )


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
