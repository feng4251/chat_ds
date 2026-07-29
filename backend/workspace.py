"""Session workspace lifecycle and safe context-file access."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from workspace_lock import (
    SESSION_PENDING_DIRECTORY,
    SESSION_PENDING_SUFFIX,
    SESSION_TOMBSTONE_DIRECTORY,
    SESSION_TOMBSTONE_SUFFIX,
    WorkspaceMutationLockError,
    run_sync_cancellation_safe,
    workspace_mutation_guard,
    workspace_mutation_lock_is_held,
)


WORKSPACE_ROOT = Path("/nfs/temp/chat_ds")
MAX_CONTEXT_CHARS = 20_000
MAX_WORKSPACE_FILE_CHARS = 200_000
_ResultT = TypeVar("_ResultT")
_PROCESS_BOOT_ID = uuid.uuid4().hex
_SESSION_DELETION_MARKER = (
    "chatds-session-deletion-v2\n"
    f"boot_id={_PROCESS_BOOT_ID}\n"
).encode("ascii")
_SESSION_DELETION_MARKER_PATTERN = re.compile(
    rb"chatds-session-deletion-v2\nboot_id=[a-f0-9]{32}\n"
)

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


def _safe_identity_component(value: str, *, field: str) -> str:
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


def _workspace_root_directory() -> Path:
    """Return the configured storage root only when it is a real directory."""

    candidate = Path(WORKSPACE_ROOT)
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
                "Workspace storage parent is unavailable.",
                code="workspace_lock_unsafe",
            ) from exc
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or stat.S_ISLNK(parent_stat.st_mode)
            or (parent_stat.st_dev, parent_stat.st_ino)
            != (parent_resolved_stat.st_dev, parent_resolved_stat.st_ino)
        ):
            raise WorkspaceMutationLockError(
                "Workspace storage parent must be a stable real directory.",
                code="workspace_lock_unsafe",
            )
        flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        parent_descriptor = os.open(parent_resolved, flags)
        try:
            os.mkdir(
                _safe_identity_component(
                    candidate.name,
                    field="workspace storage root",
                ),
                mode=0o700,
                dir_fd=parent_descriptor,
            )
            os.fsync(parent_descriptor)
        except FileExistsError:
            pass
        finally:
            os.close(parent_descriptor)
        candidate_stat = candidate.lstat()
        try:
            resolved = candidate.resolve(strict=True)
            resolved_stat = resolved.stat()
        except (OSError, RuntimeError) as exc:
            raise WorkspaceMutationLockError(
                "Workspace storage root is unavailable.",
                code="workspace_lock_unsafe",
            ) from exc
    except OSError as exc:
        raise WorkspaceMutationLockError(
            "Workspace storage root is unavailable.",
            code="workspace_lock_unsafe",
        ) from exc
    else:
        try:
            resolved = candidate.resolve(strict=True)
            resolved_stat = resolved.stat()
        except (OSError, RuntimeError) as exc:
            raise WorkspaceMutationLockError(
                "Workspace storage root is unavailable.",
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
            "Workspace storage root must be a stable real directory.",
            code="workspace_lock_unsafe",
        )
    return resolved


def _safe_directory_chain(
    components: tuple[str, ...],
    *,
    create: bool,
) -> Path:
    """Open/create a root-relative directory chain without following links."""

    root = _workspace_root_directory()
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    current = root
    try:
        for component in components:
            safe_component = _safe_identity_component(
                component,
                field="workspace path component",
            )
            created = False
            if create:
                try:
                    os.mkdir(
                        safe_component,
                        mode=0o700,
                        dir_fd=descriptor,
                    )
                    created = True
                except FileExistsError:
                    pass
            try:
                child_descriptor = os.open(
                    safe_component,
                    flags,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise WorkspaceMutationLockError(
                    "Workspace directory chain is unsafe or unavailable.",
                    code="workspace_lock_unsafe",
                ) from exc
            try:
                child_stat = os.fstat(child_descriptor)
                lexical = current / safe_component
                lexical_stat = lexical.lstat()
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or not stat.S_ISDIR(lexical_stat.st_mode)
                    or stat.S_ISLNK(lexical_stat.st_mode)
                    or (child_stat.st_dev, child_stat.st_ino)
                    != (lexical_stat.st_dev, lexical_stat.st_ino)
                ):
                    raise WorkspaceMutationLockError(
                        "Workspace directory changed during validation.",
                        code="workspace_lock_unsafe",
                    )
                if created:
                    # Persist both the new directory inode and the parent
                    # directory entry before this helper reports success.
                    os.fsync(child_descriptor)
                    os.fsync(descriptor)
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
                "Workspace directory identity changed after creation.",
                code="workspace_lock_unsafe",
            )
        return current
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, WorkspaceMutationLockError):
            raise
        raise WorkspaceMutationLockError(
            "Workspace directory escaped its configured storage root.",
            code="workspace_lock_unsafe",
        ) from exc
    finally:
        os.close(descriptor)


def session_tombstone_path(user_id: str, session_id: str) -> Path:
    safe_user = _safe_identity_component(user_id, field="user_id")
    safe_session = _safe_identity_component(session_id, field="session_id")
    return (
        WORKSPACE_ROOT
        / safe_user
        / SESSION_TOMBSTONE_DIRECTORY
        / f"{safe_session}{SESSION_TOMBSTONE_SUFFIX}"
    )


def session_pending_fence_path(user_id: str, session_id: str) -> Path:
    safe_user = _safe_identity_component(user_id, field="user_id")
    safe_session = _safe_identity_component(session_id, field="session_id")
    return (
        WORKSPACE_ROOT
        / safe_user
        / SESSION_PENDING_DIRECTORY
        / f"{safe_session}{SESSION_PENDING_SUFFIX}"
    )


def _read_session_pending_fence(
    user_id: str,
    session_id: str,
) -> tuple[bytes, tuple[int, int]] | None:
    """Read one lifecycle fence without following any directory or file link."""

    safe_user = _safe_identity_component(user_id, field="user_id")
    safe_session = _safe_identity_component(session_id, field="session_id")
    marker = session_pending_fence_path(safe_user, safe_session)
    root = _workspace_root_directory()
    current = root
    for component in (safe_user, SESSION_PENDING_DIRECTORY):
        current = current / component
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkspaceMutationLockError(
                "Session lifecycle fence path cannot be inspected.",
                code="workspace_lock_unsafe",
            ) from exc
        if (
            not stat.S_ISDIR(component_stat.st_mode)
            or stat.S_ISLNK(component_stat.st_mode)
        ):
            raise WorkspaceMutationLockError(
                "Session lifecycle fence path is unsafe.",
                code="workspace_lock_unsafe",
            )
    marker_parent = _safe_directory_chain(
        (safe_user, SESSION_PENDING_DIRECTORY),
        create=False,
    )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        file_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    directory_fd = os.open(marker_parent, directory_flags)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            marker.name,
            file_flags,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        os.close(directory_fd)
        return None
    except OSError as exc:
        os.close(directory_fd)
        raise WorkspaceMutationLockError(
            "Session lifecycle fence cannot be inspected.",
            code="workspace_lock_unsafe",
        ) from exc
    try:
        marker_stat = os.fstat(descriptor)
        path_stat = os.stat(
            marker.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or marker_stat.st_nlink != 1
            or marker_stat.st_mode & 0o077
            or (marker_stat.st_dev, marker_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise WorkspaceMutationLockError(
                "Session lifecycle fence is unsafe.",
                code="workspace_lock_unsafe",
            )
        chunks: list[bytes] = []
        remaining = 512
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        current_stat = os.stat(
            marker.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            current_stat.st_dev,
            current_stat.st_ino,
        ) != (
            marker_stat.st_dev,
            marker_stat.st_ino,
        ):
            raise WorkspaceMutationLockError(
                "Session lifecycle fence changed while being read.",
                code="workspace_lock_unsafe",
            )
        return (
            b"".join(chunks),
            (marker_stat.st_dev, marker_stat.st_ino),
        )
    except FileNotFoundError:
        raise WorkspaceMutationLockError(
            "Session lifecycle fence changed while being read.",
            code="workspace_lock_unsafe",
        )
    except OSError as exc:
        raise WorkspaceMutationLockError(
            "Session lifecycle fence cannot be read safely.",
            code="workspace_lock_unsafe",
        ) from exc
    finally:
        os.close(descriptor)
        os.close(directory_fd)


def session_deletion_tombstone_exists(
    user_id: str,
    session_id: str,
) -> bool:
    return _read_session_deletion_tombstone(user_id, session_id) is not None


def session_deletion_tombstone_authorizes_cleanup(
    user_id: str,
    session_id: str,
) -> bool:
    """Require a complete, recognized delete record before removing data."""

    state = _read_session_deletion_tombstone(user_id, session_id)
    if state is None:
        return False
    payload, _identity = state
    if _SESSION_DELETION_MARKER_PATTERN.fullmatch(payload) is None:
        raise WorkspaceMutationLockError(
            "Session deletion fence payload is invalid.",
            code="workspace_lock_unsafe",
        )
    return True


def _read_session_deletion_tombstone(
    user_id: str,
    session_id: str,
) -> tuple[bytes, tuple[int, int]] | None:
    """Read one marker through a no-follow dirfd and bind it to its inode."""

    safe_user = _safe_identity_component(user_id, field="user_id")
    marker = session_tombstone_path(safe_user, session_id)
    root = _workspace_root_directory()
    current = root
    for component in (safe_user, SESSION_TOMBSTONE_DIRECTORY):
        current = current / component
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkspaceMutationLockError(
                "Session deletion fence path cannot be inspected.",
                code="workspace_lock_unsafe",
            ) from exc
        if (
            not stat.S_ISDIR(component_stat.st_mode)
            or stat.S_ISLNK(component_stat.st_mode)
        ):
            raise WorkspaceMutationLockError(
                "Session deletion fence path is unsafe.",
                code="workspace_lock_unsafe",
            )
        if (
            component == SESSION_TOMBSTONE_DIRECTORY
            and (
                stat.S_IMODE(component_stat.st_mode) != 0o700
                or component_stat.st_uid != os.geteuid()
            )
        ):
            raise WorkspaceMutationLockError(
                "Session deletion fence directory is not private and owned.",
                code="workspace_lock_unsafe",
            )
    marker_parent = _safe_directory_chain(
        (
            safe_user,
            SESSION_TOMBSTONE_DIRECTORY,
        ),
        create=False,
    )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        file_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    directory_fd = os.open(marker_parent, directory_flags)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            marker.name,
            file_flags,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        os.close(directory_fd)
        return None
    except OSError as exc:
        os.close(directory_fd)
        raise WorkspaceMutationLockError(
            "Session deletion fence cannot be inspected.",
            code="workspace_lock_unsafe",
        ) from exc
    try:
        marker_stat = os.fstat(descriptor)
        path_stat = os.stat(
            marker.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or marker_stat.st_nlink != 1
            or stat.S_IMODE(marker_stat.st_mode) != 0o600
            or marker_stat.st_uid != os.geteuid()
            or marker_stat.st_size > 256
            or (marker_stat.st_dev, marker_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise WorkspaceMutationLockError(
                "Session deletion fence is unsafe.",
                code="workspace_lock_unsafe",
            )
        chunks: list[bytes] = []
        remaining = 256
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        # Reject a final-component swap after the read as well.
        current_stat = os.stat(
            marker.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            current_stat.st_dev,
            current_stat.st_ino,
        ) != (
            marker_stat.st_dev,
            marker_stat.st_ino,
        ):
            raise WorkspaceMutationLockError(
                "Session deletion fence changed while being read.",
                code="workspace_lock_unsafe",
            )
        return (
            b"".join(chunks),
            (marker_stat.st_dev, marker_stat.st_ino),
        )
    except FileNotFoundError:
        raise WorkspaceMutationLockError(
            "Session deletion fence changed while being read.",
            code="workspace_lock_unsafe",
        )
    except OSError as exc:
        raise WorkspaceMutationLockError(
            "Session deletion fence cannot be read safely.",
            code="workspace_lock_unsafe",
        ) from exc
    finally:
        os.close(descriptor)
        os.close(directory_fd)


def session_deletion_tombstone_is_current_process(
    user_id: str,
    session_id: str,
) -> bool:
    state = _read_session_deletion_tombstone(user_id, session_id)
    if state is None:
        return False
    payload, _identity = state
    return payload == _SESSION_DELETION_MARKER


def _session_pending_fence_payload(operation_id: str) -> bytes:
    identity = str(operation_id).strip().lower()
    if re.fullmatch(r"[a-f0-9]{64}", identity) is None:
        raise ValueError(
            "Session lifecycle operation id must be a SHA-256 hex digest."
        )
    return (
        "chatds-session-lifecycle-pending-v1\n"
        f"operation_id={identity}\n"
    ).encode("ascii")


def session_pending_fence_matches(
    user_id: str,
    session_id: str,
    operation_id: str,
) -> bool:
    state = _read_session_pending_fence(user_id, session_id)
    return (
        state is not None
        and state[0] == _session_pending_fence_payload(operation_id)
    )


def _claim_session_pending_fence_unlocked(
    user_id: str,
    session_id: str,
    operation_id: str,
) -> bool:
    safe_user = _safe_identity_component(user_id, field="user_id")
    safe_session = _safe_identity_component(session_id, field="session_id")
    payload = _session_pending_fence_payload(operation_id)
    marker = session_pending_fence_path(safe_user, safe_session)
    marker_parent = _safe_directory_chain(
        (safe_user, SESSION_PENDING_DIRECTORY),
        create=True,
    )
    parent_stat = marker_parent.lstat()
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or stat.S_ISLNK(parent_stat.st_mode)
        or parent_stat.st_mode & 0o077
    ):
        raise WorkspaceMutationLockError(
            "Session lifecycle fence directory is unsafe.",
            code="workspace_lock_unsafe",
        )
    parent_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        parent_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        file_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    parent_descriptor = os.open(marker_parent, parent_flags)
    descriptor: int | None = None
    created = False
    descriptor_identity: tuple[int, int] | None = None
    try:
        try:
            descriptor = os.open(
                marker.name,
                file_flags,
                0o600,
                dir_fd=parent_descriptor,
            )
            created = True
        except FileExistsError:
            state = _read_session_pending_fence(
                safe_user,
                safe_session,
            )
            if state is not None and state[0] == payload:
                return False
            raise WorkspaceMutationLockError(
                "Session is fenced by another lifecycle operation.",
                code="workspace_session_pending",
            )
        descriptor_stat = os.fstat(descriptor)
        descriptor_identity = (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        )
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or descriptor_stat.st_nlink != 1
            or descriptor_stat.st_mode & 0o077
        ):
            raise WorkspaceMutationLockError(
                "Session lifecycle fence is unsafe.",
                code="workspace_lock_unsafe",
            )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError(
                    "Short write while publishing lifecycle fence."
                )
            offset += written
        os.fsync(descriptor)
        path_stat = os.stat(
            marker.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            path_stat.st_dev,
            path_stat.st_ino,
        ) != descriptor_identity:
            raise WorkspaceMutationLockError(
                "Session lifecycle fence changed while being published.",
                code="workspace_lock_unsafe",
            )
        os.fsync(parent_descriptor)
        return True
    except BaseException:
        if created and descriptor_identity is not None:
            try:
                current_stat = os.stat(
                    marker.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    current_stat.st_dev,
                    current_stat.st_ino,
                ) == descriptor_identity:
                    os.unlink(marker.name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def claim_session_pending_fence(
    user_id: str,
    session_id: str,
    operation_id: str,
) -> bool:
    """Claim an exact durable lifecycle fence before exposing a session."""

    require_session_workspace_not_deleted(user_id, session_id)
    workspace = (
        WORKSPACE_ROOT
        / _safe_identity_component(user_id, field="user_id")
        / _safe_identity_component(session_id, field="session_id")
        / "workspace"
    )
    if workspace.exists() or workspace.is_symlink():
        with workspace_mutation_guard(
            workspace,
            allow_pending=True,
        ):
            return _claim_session_pending_fence_unlocked(
                user_id,
                session_id,
                operation_id,
            )
    return _claim_session_pending_fence_unlocked(
        user_id,
        session_id,
        operation_id,
    )


def _clear_session_pending_fence_unlocked(
    user_id: str,
    session_id: str,
    *,
    expected_payload: bytes | None,
) -> bool:
    safe_user = _safe_identity_component(user_id, field="user_id")
    safe_session = _safe_identity_component(session_id, field="session_id")
    state = _read_session_pending_fence(safe_user, safe_session)
    if state is None:
        return False
    payload, expected_identity = state
    if expected_payload is not None and payload != expected_payload:
        raise WorkspaceMutationLockError(
            "Session is fenced by another lifecycle operation.",
            code="workspace_session_pending",
        )
    marker = session_pending_fence_path(safe_user, safe_session)
    marker_parent = _safe_directory_chain(
        (safe_user, SESSION_PENDING_DIRECTORY),
        create=False,
    )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(marker_parent, directory_flags)
    try:
        current_stat = os.stat(
            marker.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            current_stat.st_dev,
            current_stat.st_ino,
        ) != expected_identity:
            raise WorkspaceMutationLockError(
                "Session lifecycle fence changed while being cleared.",
                code="workspace_lock_unsafe",
            )
        os.unlink(marker.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    try:
        marker.parent.rmdir()
    except OSError:
        pass
    return True


def clear_session_pending_fence(
    user_id: str,
    session_id: str,
    operation_id: str,
) -> bool:
    """Atomically release only the exact lifecycle fence owned by a retry."""

    expected_payload = _session_pending_fence_payload(operation_id)
    workspace = (
        WORKSPACE_ROOT
        / _safe_identity_component(user_id, field="user_id")
        / _safe_identity_component(session_id, field="session_id")
        / "workspace"
    )
    if workspace.exists() or workspace.is_symlink():
        with workspace_mutation_guard(
            workspace,
            allow_pending=True,
        ):
            return _clear_session_pending_fence_unlocked(
                user_id,
                session_id,
                expected_payload=expected_payload,
            )
    return _clear_session_pending_fence_unlocked(
        user_id,
        session_id,
        expected_payload=expected_payload,
    )


def clear_session_pending_fence_after_deletion(
    user_id: str,
    session_id: str,
) -> bool:
    """Remove any lifecycle fence only after durable deletion wins."""

    if not session_deletion_tombstone_authorizes_cleanup(
        user_id,
        session_id,
    ):
        raise WorkspaceMutationLockError(
            "Lifecycle fence cleanup requires a durable deletion marker.",
            code="workspace_lock_unsafe",
        )
    return _clear_session_pending_fence_unlocked(
        user_id,
        session_id,
        expected_payload=None,
    )


def require_session_workspace_not_deleted(
    user_id: str,
    session_id: str,
) -> None:
    if session_deletion_tombstone_exists(user_id, session_id):
        raise WorkspaceMutationLockError(
            "Session workspace has a durable deletion fence.",
            code="workspace_session_deleted",
        )


def require_session_workspace_active(user_id: str, session_id: str) -> None:
    require_session_workspace_not_deleted(user_id, session_id)
    if _read_session_pending_fence(user_id, session_id) is not None:
        raise WorkspaceMutationLockError(
            "Session workspace has a durable lifecycle fence.",
            code="workspace_session_pending",
        )


def _publish_session_deletion_tombstone_unlocked(
    user_id: str,
    session_id: str,
) -> Path:
    marker = session_tombstone_path(user_id, session_id)
    marker_parent = _safe_directory_chain(
        (
            _safe_identity_component(user_id, field="user_id"),
            SESSION_TOMBSTONE_DIRECTORY,
        ),
        create=True,
    )
    directory_stat = marker.parent.lstat()
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or stat.S_ISLNK(directory_stat.st_mode)
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
        or directory_stat.st_uid != os.geteuid()
    ):
        raise WorkspaceMutationLockError(
            "Session deletion fence directory is unsafe.",
            code="workspace_lock_unsafe",
        )

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        parent_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
    parent_descriptor = os.open(marker_parent, parent_flags)
    descriptor: int | None = None
    created = False
    try:
        try:
            descriptor = os.open(
                marker.name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
            created = True
        except FileExistsError:
            existing_flags = os.O_WRONLY
            if hasattr(os, "O_CLOEXEC"):
                existing_flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                existing_flags |= os.O_NOFOLLOW
            descriptor = os.open(
                marker.name,
                existing_flags,
                dir_fd=parent_descriptor,
            )
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.stat(
            marker.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or descriptor_stat.st_nlink != 1
            or stat.S_IMODE(descriptor_stat.st_mode) != 0o600
            or descriptor_stat.st_uid != os.geteuid()
            or (descriptor_stat.st_dev, descriptor_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise WorkspaceMutationLockError(
                "Session deletion fence is unsafe.",
                code="workspace_lock_unsafe",
            )
        if not created:
            # A retry after process restart claims the durable intent for this
            # boot while holding the session workspace flock. Periodic stale
            # recovery therefore cannot clear an active retry's fence.
            os.ftruncate(descriptor, 0)
        offset = 0
        while offset < len(_SESSION_DELETION_MARKER):
            written = os.write(
                descriptor,
                _SESSION_DELETION_MARKER[offset:],
            )
            if written <= 0:
                raise OSError("Short write while publishing deletion fence.")
            offset += written
        os.fsync(descriptor)
        current_stat = os.stat(
            marker.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            current_stat.st_dev,
            current_stat.st_ino,
        ) != (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ):
            raise WorkspaceMutationLockError(
                "Session deletion fence changed while being published.",
                code="workspace_lock_unsafe",
            )
        os.fsync(parent_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)
    return marker


def publish_session_deletion_tombstone(
    user_id: str,
    session_id: str,
) -> Path:
    """Durably linearize deletion after every earlier workspace mutation."""

    safe_user = _safe_identity_component(user_id, field="user_id")
    safe_session = _safe_identity_component(session_id, field="session_id")
    # Even a never-opened conversation gets the canonical sibling flock
    # before its marker is published. This gives publish/claim/stale-clear one
    # shared serialization point without weakening the durable marker.
    workspace = _safe_directory_chain(
        (safe_user, safe_session, "workspace"),
        create=True,
    )
    with workspace_mutation_guard(
        workspace,
        allow_deleted=True,
        allow_pending=True,
    ):
        return _publish_session_deletion_tombstone_unlocked(
            safe_user,
            safe_session,
        )


async def publish_session_deletion_tombstone_async(
    user_id: str,
    session_id: str,
) -> Path:
    return await run_sync_cancellation_safe(
        lambda: publish_session_deletion_tombstone(user_id, session_id)
    )


def clear_stale_session_deletion_tombstone(
    user_id: str,
    session_id: str,
) -> bool:
    """Clear only a pre-boot marker after an in-lock payload recheck."""

    safe_user = _safe_identity_component(user_id, field="user_id")
    safe_session = _safe_identity_component(session_id, field="session_id")
    # The bounded live-row scan calls this for every discovered conversation.
    # Do not create workspace state for the overwhelmingly common no-marker
    # case. A marker published after this check belongs to a newer operation
    # and is intentionally left untouched.
    if _read_session_deletion_tombstone(
        safe_user,
        safe_session,
    ) is None:
        return False
    workspace = _safe_directory_chain(
        (safe_user, safe_session, "workspace"),
        create=True,
    )
    marker = session_tombstone_path(safe_user, safe_session)
    with workspace_mutation_guard(
        workspace,
        allow_deleted=True,
        allow_pending=True,
    ):
        state = _read_session_deletion_tombstone(
            safe_user,
            safe_session,
        )
        if state is None:
            return False
        payload, expected_identity = state
        if payload == _SESSION_DELETION_MARKER:
            return False
        marker_parent = _safe_directory_chain(
            (safe_user, SESSION_TOMBSTONE_DIRECTORY),
            create=False,
        )
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        directory_fd = os.open(marker_parent, directory_flags)
        try:
            current_stat = os.stat(
                marker.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                current_stat.st_dev,
                current_stat.st_ino,
            ) != expected_identity:
                return False
            # Publish/claim uses the same workspace flock, so this identity
            # compare and unlink is a proper compare-and-delete transaction.
            os.unlink(marker.name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    try:
        marker.parent.rmdir()
    except OSError:
        pass
    return True


async def clear_stale_session_deletion_tombstone_async(
    user_id: str,
    session_id: str,
) -> bool:
    return await run_sync_cancellation_safe(
        lambda: clear_stale_session_deletion_tombstone(
            user_id,
            session_id,
        )
    )


def session_root(user_id: str, session_id: str) -> Path:
    safe_user = _safe_identity_component(user_id, field="user_id")
    safe_session = _safe_identity_component(session_id, field="session_id")
    require_session_workspace_active(safe_user, safe_session)
    root = _safe_directory_chain(
        (safe_user, safe_session),
        create=True,
    )
    require_session_workspace_active(safe_user, safe_session)
    return root


def workspace_dir(user_id: str, session_id: str, *, create: bool = True) -> Path:
    safe_user = _safe_identity_component(user_id, field="user_id")
    safe_session = _safe_identity_component(session_id, field="session_id")
    session_root(safe_user, safe_session)
    root = (
        _workspace_root_directory()
        / safe_user
        / safe_session
        / "workspace"
    )
    if create or root.exists() or root.is_symlink():
        return _safe_directory_chain(
            (safe_user, safe_session, "workspace"),
            create=create,
        )
    return root


def _bootstrap_is_complete(root: Path) -> bool:
    return all((root / filename).exists() for filename in BOOTSTRAP_FILES) and (
        root / ".gitignore"
    ).exists()


def _ensure_bootstrap_unlocked(root: Path) -> None:
    for filename, content in BOOTSTRAP_FILES.items():
        path = root / filename
        if not path.exists():
            atomic_write_text(path, content)
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        atomic_write_text(
            gitignore,
            ".env\n.env.*\n!.env.example\n*.key\n*.pem\nsecrets*\n",
        )


def ensure_workspace(user_id: str, session_id: str) -> Path:
    root = workspace_dir(user_id, session_id)
    # The fast path is read-only.  Bootstrap creation uses the exact same
    # sibling flock as Harness artifact commits, preventing a first-request
    # bootstrap write from racing a Harness apply.
    if not _bootstrap_is_complete(root):
        with workspace_mutation_guard(root):
            _ensure_bootstrap_unlocked(root)
    return root


async def ensure_workspace_async(user_id: str, session_id: str) -> Path:
    """Create/repair bootstrap state without blocking an asyncio event loop."""

    return await run_sync_cancellation_safe(
        lambda: ensure_workspace(user_id, session_id)
    )


@contextmanager
def session_workspace_mutation_guard(
    user_id: str,
    session_id: str,
    *,
    timeout_seconds: float | None = None,
) -> Iterator[Path]:
    """Yield a stable session workspace while its shared mutation lock is held."""

    root = ensure_workspace(user_id, session_id)
    with workspace_mutation_guard(root, timeout_seconds=timeout_seconds):
        yield root


async def run_session_workspace_mutation_async(
    user_id: str,
    session_id: str,
    operation: Callable[[Path], _ResultT],
    *,
    timeout_seconds: float | None = None,
) -> _ResultT:
    """Run a complete session mutation in one cancellation-drained worker."""

    def _guarded_operation() -> _ResultT:
        with session_workspace_mutation_guard(
            user_id,
            session_id,
            timeout_seconds=timeout_seconds,
        ) as root:
            return operation(root)

    return await run_sync_cancellation_safe(_guarded_operation)


def safe_workspace_path_in_root(
    root: Path,
    relative_path: str,
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve one model-visible path beneath an already selected workspace."""

    candidate_root = Path(root)
    try:
        candidate_root_stat = candidate_root.lstat()
        resolved_root = candidate_root.resolve(strict=True)
        resolved_root_stat = resolved_root.stat()
    except (OSError, RuntimeError) as exc:
        raise ValueError("Workspace root is unavailable.") from exc
    if (
        not stat.S_ISDIR(candidate_root_stat.st_mode)
        or stat.S_ISLNK(candidate_root_stat.st_mode)
        or (candidate_root_stat.st_dev, candidate_root_stat.st_ino)
        != (resolved_root_stat.st_dev, resolved_root_stat.st_ino)
    ):
        raise ValueError("Workspace root is unavailable.")

    if not relative_path or not relative_path.strip():
        raise ValueError("Path cannot be empty.")
    path_obj = Path(relative_path)
    if path_obj.is_absolute():
        raise ValueError("Absolute paths are not allowed.")
    if ".." in path_obj.parts:
        raise ValueError("Path traversal is not allowed.")
    current = resolved_root
    for part in path_obj.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinks are not allowed.")
    candidate = (resolved_root / relative_path).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Path traversal is not allowed.") from exc
    if must_exist and not candidate.exists():
        raise FileNotFoundError(relative_path)
    return candidate


def safe_workspace_path(
    user_id: str,
    session_id: str,
    relative_path: str,
    *,
    must_exist: bool = False,
) -> Path:
    root = ensure_workspace(user_id, session_id).resolve()
    return safe_workspace_path_in_root(
        root,
        relative_path,
        must_exist=must_exist,
    )


def _directory_descriptor_matches_path(
    descriptor: int,
    path: Path,
) -> bool:
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(descriptor_stat.st_mode)
        and stat.S_ISDIR(path_stat.st_mode)
        and not stat.S_ISLNK(path_stat.st_mode)
        and (descriptor_stat.st_dev, descriptor_stat.st_ino)
        == (path_stat.st_dev, path_stat.st_ino)
    )


@contextmanager
def workspace_parent_directory_fd(
    workspace: Path,
    relative_path: str,
    *,
    create_parents: bool = True,
) -> Iterator[tuple[int, Path, str]]:
    """Yield a no-follow parent fd for one workspace-relative leaf."""

    candidate_root = Path(workspace)
    candidate_stat = candidate_root.lstat()
    root = candidate_root.resolve(strict=True)
    resolved_stat = root.stat()
    if (
        not stat.S_ISDIR(candidate_stat.st_mode)
        or stat.S_ISLNK(candidate_stat.st_mode)
        or (candidate_stat.st_dev, candidate_stat.st_ino)
        != (resolved_stat.st_dev, resolved_stat.st_ino)
    ):
        raise ValueError("Workspace root must be a stable real directory.")
    path_obj = Path(relative_path)
    if (
        not relative_path
        or not relative_path.strip()
        or path_obj.is_absolute()
        or ".." in path_obj.parts
        or path_obj.name in {"", ".", ".."}
    ):
        raise ValueError("Unsafe workspace-relative path.")
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    current = root
    try:
        if not _directory_descriptor_matches_path(descriptor, root):
            raise ValueError("Workspace root changed before traversal.")
        for component in path_obj.parent.parts:
            if component in {"", "."}:
                continue
            if create_parents:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child_descriptor = os.open(
                component,
                flags,
                dir_fd=descriptor,
            )
            child_path = current / component
            if not _directory_descriptor_matches_path(
                child_descriptor,
                child_path,
            ):
                os.close(child_descriptor)
                raise ValueError(
                    "Workspace parent changed during no-follow traversal."
                )
            os.close(descriptor)
            descriptor = child_descriptor
            current = child_path
        resolved_parent = current.resolve(strict=True)
        resolved_parent.relative_to(root)
        if not _directory_descriptor_matches_path(
            descriptor,
            current,
        ):
            raise ValueError(
                "Workspace parent changed after no-follow traversal."
            )
        yield descriptor, current, path_obj.name
    finally:
        os.close(descriptor)


def require_open_workspace_directory_current(
    descriptor: int,
    path: Path,
    workspace: Path,
) -> None:
    """Revalidate an opened mutation parent immediately before commit."""

    if not _directory_descriptor_matches_path(descriptor, path):
        raise ValueError("Workspace mutation parent identity changed.")
    try:
        path.resolve(strict=True).relative_to(
            Path(workspace).resolve(strict=True)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            "Workspace mutation parent escaped its workspace."
        ) from exc


def _containing_session_workspace(path: Path) -> Path | None:
    """Return the canonical session workspace for a Backend-managed path."""

    try:
        base = _workspace_root_directory()
        candidate = Path(path).absolute()
        relative = candidate.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return None
    if len(relative.parts) < 4 or relative.parts[2] != "workspace":
        return None
    root = base.joinpath(*relative.parts[:3])
    try:
        root_stat = root.lstat()
        resolved = root.resolve(strict=True)
        resolved_stat = resolved.stat()
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_ISLNK(root_stat.st_mode)
        or (root_stat.st_dev, root_stat.st_ino)
        != (resolved_stat.st_dev, resolved_stat.st_ino)
    ):
        return None
    return root


def _atomic_write_text_unlocked(path: Path, content: str) -> None:
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


def _atomic_write_workspace_bytes_unlocked(
    workspace: Path,
    path: Path,
    content: bytes,
) -> None:
    try:
        relative = Path(path).absolute().relative_to(
            Path(workspace).absolute()
        )
    except ValueError as exc:
        raise ValueError(
            "Atomic write target escaped the session workspace."
        ) from exc
    temporary_name = f".chat_ds_{os.getpid()}_{os.urandom(8).hex()}"
    with workspace_parent_directory_fd(
        workspace,
        relative.as_posix(),
        create_parents=True,
    ) as (parent_descriptor, parent_path, leaf_name):
        try:
            existing = os.stat(
                leaf_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise ValueError("Atomic write target cannot be a symlink.")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        try:
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("Atomic workspace write made no progress.")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            require_open_workspace_directory_current(
                parent_descriptor,
                parent_path,
                workspace,
            )
            os.replace(
                temporary_name,
                leaf_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.fsync(parent_descriptor)
        except BaseException:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except OSError:
                pass
            raise


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically write text, acquiring the session lock when applicable."""

    workspace = _containing_session_workspace(path)
    if workspace is None:
        _atomic_write_text_unlocked(path, content)
        return
    if workspace_mutation_lock_is_held(workspace):
        _atomic_write_workspace_bytes_unlocked(
            workspace,
            path,
            content.encode("utf-8"),
        )
        return
    with workspace_mutation_guard(workspace):
        _atomic_write_workspace_bytes_unlocked(
            workspace,
            path,
            content.encode("utf-8"),
        )


def _atomic_write_bytes_unlocked(path: Path, content: bytes) -> None:
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


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomically write bytes, acquiring the session lock when applicable."""

    workspace = _containing_session_workspace(path)
    if workspace is None:
        _atomic_write_bytes_unlocked(path, content)
        return
    if workspace_mutation_lock_is_held(workspace):
        _atomic_write_workspace_bytes_unlocked(workspace, path, content)
        return
    with workspace_mutation_guard(workspace):
        _atomic_write_workspace_bytes_unlocked(workspace, path, content)


def workspace_file_metadata(path: Path) -> dict:
    ext = path.suffix.lower().lstrip('.')
    mime_type = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
    text_exts = {
        'txt', 'md', 'markdown', 'mdx', 'py', 'js', 'jsx', 'ts', 'tsx', 'json',
        'yaml', 'yml', 'toml', 'csv', 'tsv', 'log', 'xml', 'html', 'css', 'sql',
        'sh', 'bash', 'r', 'R', 'ini', 'cfg', 'conf', 'tex', 'bib', 'rst',
    }
    is_text = mime_type.startswith('text/') or ext in text_exts
    if ext in {'md', 'markdown', 'mdx'}:
        preview_kind = 'markdown'
    elif is_text:
        preview_kind = 'text'
    elif mime_type == 'application/pdf' or ext == 'pdf':
        preview_kind = 'pdf'
    elif mime_type.startswith('image/'):
        preview_kind = 'image'
    elif ext in {'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx'}:
        preview_kind = 'office'
    else:
        preview_kind = 'binary'
    return {
        'ext': ext,
        'mime_type': mime_type,
        'is_text': is_text,
        'preview_kind': preview_kind,
    }


def list_workspace_files(user_id: str, session_id: str) -> list[dict]:
    root = ensure_workspace(user_id, session_id).resolve()
    files: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = str(path.relative_to(root))
        stat = path.stat()
        files.append({
            "path": rel,
            "size": stat.st_size,
            "updated_at": stat.st_mtime,
            **workspace_file_metadata(path),
        })
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
    target = workspace_dir(user_id, target_session_id)
    lock_roots = [target]
    if source.exists():
        lock_roots.append(source)
    # Stable ordering avoids lock inversion if two maintenance operations ever
    # clone opposite directions concurrently.
    with ExitStack() as stack:
        for root in sorted(lock_roots, key=lambda item: str(item.resolve())):
            stack.enter_context(workspace_mutation_guard(root))
        if source.exists():
            shutil.copytree(source, target, dirs_exist_ok=True)
        _ensure_bootstrap_unlocked(target)


async def clone_session_workspace_async(
    user_id: str,
    source_session_id: str,
    target_session_id: str,
) -> None:
    await run_sync_cancellation_safe(
        lambda: clone_session_workspace(
            user_id,
            source_session_id,
            target_session_id,
        )
    )


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
