"""Bounded cross-process coordination for one session workspace.

The Harness has an intentionally separate implementation because its image
does not contain the Backend.  Both implementations use the same private
sibling file and POSIX ``flock`` protocol, so a Backend mutation cannot race a
Harness compare-and-swap/apply operation.
"""

from __future__ import annotations

import asyncio
import fcntl
import errno
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar


WORKSPACE_MUTATION_LOCK_FILENAME = ".chatds-workspace-mutation.lock"
SESSION_TOMBSTONE_DIRECTORY = ".chatds-session-tombstones"
SESSION_TOMBSTONE_SUFFIX = ".deleted"
SESSION_PENDING_DIRECTORY = ".chatds-session-pending"
SESSION_PENDING_SUFFIX = ".pending"
DEFAULT_WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS = 2.0
MAX_WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS = 60.0
_LOCK_RETRY_INTERVAL_SECONDS = 0.01
_THREAD_STATE = threading.local()
_ResultT = TypeVar("_ResultT")


class WorkspaceMutationLockError(RuntimeError):
    """A workspace lock was unsafe or could not be acquired in time."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code

    @property
    def http_status_code(self) -> int:
        if self.code == "workspace_session_deleted":
            return 410
        if self.code in {
            "workspace_lock_timeout",
            "workspace_session_pending",
        }:
            return 423
        return 503

    @property
    def public_message(self) -> str:
        if self.code == "workspace_session_deleted":
            return "This session workspace has been deleted."
        if self.code == "workspace_session_pending":
            return (
                "This session is completing a durable lifecycle operation; "
                "retry shortly."
            )
        if self.code == "workspace_lock_timeout":
            return "Session workspace is busy; retry the mutation shortly."
        return "Session workspace is temporarily unavailable for safe mutation."


def _configured_timeout_seconds() -> float:
    raw = os.environ.get("WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS
    if not (0.0 <= parsed <= MAX_WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS):
        return DEFAULT_WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS
    return parsed


def _workspace_identity(workspace: Path) -> tuple[Path, tuple[int, int]]:
    candidate = Path(workspace)
    try:
        candidate_stat = candidate.lstat()
    except OSError as exc:
        raise WorkspaceMutationLockError(
            "Workspace root is unavailable for mutation locking.",
            code="workspace_lock_unsafe",
        ) from exc
    if not stat.S_ISDIR(candidate_stat.st_mode) or stat.S_ISLNK(
        candidate_stat.st_mode
    ):
        raise WorkspaceMutationLockError(
            "Workspace root must be a real directory for mutation locking.",
            code="workspace_lock_unsafe",
        )
    try:
        root = candidate.resolve(strict=True)
        root_stat = root.stat()
    except (OSError, RuntimeError) as exc:
        raise WorkspaceMutationLockError(
            "Workspace root cannot be resolved for mutation locking.",
            code="workspace_lock_unsafe",
        ) from exc
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or (root_stat.st_dev, root_stat.st_ino)
        != (candidate_stat.st_dev, candidate_stat.st_ino)
    ):
        raise WorkspaceMutationLockError(
            "Workspace root changed while preparing its mutation lock.",
            code="workspace_lock_unsafe",
        )
    return root, (root_stat.st_dev, root_stat.st_ino)


def _lock_file_is_current(
    lock_path: Path,
    descriptor_stat: os.stat_result,
) -> bool:
    try:
        path_stat = lock_path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(path_stat.st_mode)
        and path_stat.st_nlink == 1
        and (path_stat.st_dev, path_stat.st_ino)
        == (descriptor_stat.st_dev, descriptor_stat.st_ino)
    )


def session_tombstone_path_for_workspace(workspace: Path) -> Path | None:
    root = Path(workspace)
    if root.name != "workspace" or root.parent == root:
        return None
    session_dir = root.parent
    user_dir = session_dir.parent
    if user_dir == session_dir:
        return None
    return (
        user_dir
        / SESSION_TOMBSTONE_DIRECTORY
        / f"{session_dir.name}{SESSION_TOMBSTONE_SUFFIX}"
    )


def session_pending_path_for_workspace(workspace: Path) -> Path | None:
    root = Path(workspace)
    if root.name != "workspace" or root.parent == root:
        return None
    session_dir = root.parent
    user_dir = session_dir.parent
    if user_dir == session_dir:
        return None
    return (
        user_dir
        / SESSION_PENDING_DIRECTORY
        / f"{session_dir.name}{SESSION_PENDING_SUFFIX}"
    )


def _raise_if_session_pending(workspace: Path) -> None:
    pending = session_pending_path_for_workspace(workspace)
    if pending is None:
        return
    try:
        marker_stat = pending.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WorkspaceMutationLockError(
            "Session lifecycle fence cannot be inspected.",
            code="workspace_lock_unsafe",
        ) from exc
    if (
        not stat.S_ISREG(marker_stat.st_mode)
        or stat.S_ISLNK(marker_stat.st_mode)
        or marker_stat.st_nlink != 1
        or marker_stat.st_mode & 0o077
    ):
        raise WorkspaceMutationLockError(
            "Session lifecycle fence is unsafe.",
            code="workspace_lock_unsafe",
        )
    raise WorkspaceMutationLockError(
        "Session workspace is fenced by a durable lifecycle marker.",
        code="workspace_session_pending",
    )


def _raise_if_session_deleted(workspace: Path) -> None:
    tombstone = session_tombstone_path_for_workspace(workspace)
    if tombstone is None:
        return
    try:
        marker_stat = tombstone.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WorkspaceMutationLockError(
            "Session deletion fence cannot be inspected.",
            code="workspace_lock_unsafe",
        ) from exc
    if (
        not stat.S_ISREG(marker_stat.st_mode)
        or stat.S_ISLNK(marker_stat.st_mode)
        or marker_stat.st_nlink != 1
        or marker_stat.st_mode & 0o077
    ):
        raise WorkspaceMutationLockError(
            "Session deletion fence is unsafe.",
            code="workspace_lock_unsafe",
        )
    raise WorkspaceMutationLockError(
        "Session workspace is fenced by a durable deletion marker.",
        code="workspace_session_deleted",
    )


def _held_workspace_locks() -> dict[str, tuple[tuple[int, int], int]]:
    process_id = os.getpid()
    if getattr(_THREAD_STATE, "process_id", None) != process_id:
        _THREAD_STATE.process_id = process_id
        _THREAD_STATE.held = {}
    return _THREAD_STATE.held


def workspace_mutation_lock_is_held(workspace: Path) -> bool:
    """Return whether this thread owns the canonical workspace flock."""

    try:
        root, identity = _workspace_identity(workspace)
    except WorkspaceMutationLockError:
        return False
    held = _held_workspace_locks().get(str(root))
    return held is not None and held[0] == identity and held[1] > 0


async def run_sync_cancellation_safe(
    operation: Callable[[], _ResultT],
) -> _ResultT:
    """Run blocking filesystem work off-loop and drain it before cancellation.

    ``asyncio.to_thread`` cannot kill a running syscall. Shielding and draining
    the worker guarantees that a cancelled request never leaves an unobserved
    lock owner or filesystem mutation running in the shared thread pool.
    """

    worker = asyncio.create_task(asyncio.to_thread(operation))
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError as exc:
            if worker.done():
                result = worker.result()
                if cancellation is None:
                    cancellation = exc
                break
            if cancellation is None:
                cancellation = exc
            continue
    if cancellation is not None:
        raise cancellation
    return result


async def run_workspace_mutation_async(
    workspace: Path,
    operation: Callable[[], _ResultT],
    *,
    timeout_seconds: float | None = None,
    allow_deleted: bool = False,
    allow_pending: bool = False,
) -> _ResultT:
    """Acquire, mutate, and release entirely within one bounded worker thread."""

    def _guarded_operation() -> _ResultT:
        with workspace_mutation_guard(
            workspace,
            timeout_seconds=timeout_seconds,
            allow_deleted=allow_deleted,
            allow_pending=allow_pending,
        ):
            return operation()

    return await run_sync_cancellation_safe(_guarded_operation)


@contextmanager
def workspace_mutation_guard(
    workspace: Path,
    *,
    timeout_seconds: float | None = None,
    allow_deleted: bool = False,
    allow_pending: bool = False,
) -> Iterator[None]:
    """Serialize a short Backend mutation with Harness workspace commits."""

    root, expected_workspace_identity = _workspace_identity(workspace)
    if not allow_deleted:
        _raise_if_session_deleted(root)
    if not allow_pending:
        _raise_if_session_pending(root)
    lock_key = str(root)
    held_locks = _held_workspace_locks()
    existing = held_locks.get(lock_key)
    if existing is not None:
        if existing[0] != expected_workspace_identity:
            raise WorkspaceMutationLockError(
                "Workspace identity changed inside a mutation section.",
                code="workspace_lock_unsafe",
            )
        held_locks[lock_key] = (existing[0], existing[1] + 1)
        try:
            yield
        finally:
            current = held_locks.get(lock_key)
            if current is not None:
                if current[1] <= 1:
                    held_locks.pop(lock_key, None)
                else:
                    held_locks[lock_key] = (current[0], current[1] - 1)
        return

    timeout = (
        _configured_timeout_seconds()
        if timeout_seconds is None
        else float(timeout_seconds)
    )
    if not (0.0 <= timeout <= MAX_WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS):
        raise ValueError(
            "timeout_seconds must be between 0 and "
            f"{MAX_WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS:g}"
        )

    lock_path = root.parent / WORKSPACE_MUTATION_LOCK_FILENAME
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    descriptor: int | None = None
    locked = False
    entered_body = False
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        descriptor_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or descriptor_stat.st_nlink != 1
            or descriptor_stat.st_mode & 0o077
            or not _lock_file_is_current(lock_path, descriptor_stat)
        ):
            raise WorkspaceMutationLockError(
                "Workspace mutation lock must be a private regular file.",
                code="workspace_lock_unsafe",
            )

        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceMutationLockError(
                        "Workspace mutation lock acquisition timed out.",
                        code="workspace_lock_timeout",
                    ) from exc
                time.sleep(min(_LOCK_RETRY_INTERVAL_SECONDS, remaining))

        try:
            current_workspace_stat = root.stat()
        except OSError as exc:
            raise WorkspaceMutationLockError(
                "Workspace root disappeared while acquiring its mutation lock.",
                code="workspace_lock_unsafe",
            ) from exc
        if (
            (current_workspace_stat.st_dev, current_workspace_stat.st_ino)
            != expected_workspace_identity
            or not _lock_file_is_current(lock_path, descriptor_stat)
        ):
            raise WorkspaceMutationLockError(
                "Workspace or mutation lock changed during acquisition.",
                code="workspace_lock_unsafe",
            )
        if not allow_deleted:
            _raise_if_session_deleted(root)
        if not allow_pending:
            _raise_if_session_pending(root)
        held_locks[lock_key] = (expected_workspace_identity, 1)
        try:
            entered_body = True
            yield
        finally:
            held_locks.pop(lock_key, None)
    except WorkspaceMutationLockError:
        raise
    except OSError as exc:
        if entered_body:
            raise
        code = (
            "workspace_lock_unsafe"
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}
            else "workspace_lock_io"
        )
        raise WorkspaceMutationLockError(
            "Workspace mutation lock could not be acquired safely.",
            code=code,
        ) from exc
    finally:
        if descriptor is not None:
            if locked:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(descriptor)
            except OSError:
                pass
