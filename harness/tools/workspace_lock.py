"""Bounded cross-process coordination for one session workspace."""

from __future__ import annotations

import asyncio
import errno
import fcntl
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
    """The runtime-owned workspace lock could not be acquired safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "workspace_lock_unsafe",
    ) -> None:
        super().__init__(message)
        self.code = code


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
        root = candidate.resolve(strict=True)
        root_stat = root.stat()
    except (OSError, RuntimeError) as exc:
        raise WorkspaceMutationLockError(
            "Workspace root is unavailable for mutation locking.",
            code="workspace_lock_unsafe",
        ) from exc
    if (
        not stat.S_ISDIR(candidate_stat.st_mode)
        or stat.S_ISLNK(candidate_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or (candidate_stat.st_dev, candidate_stat.st_ino)
        != (root_stat.st_dev, root_stat.st_ino)
    ):
        raise WorkspaceMutationLockError(
            "Workspace root must be a stable real directory.",
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


def _session_tombstone_path(workspace: Path) -> Path | None:
    root = Path(workspace)
    if root.name != "workspace" or root.parent == root:
        return None
    return (
        root.parent.parent
        / SESSION_TOMBSTONE_DIRECTORY
        / f"{root.parent.name}{SESSION_TOMBSTONE_SUFFIX}"
    )


def _session_pending_path(workspace: Path) -> Path | None:
    root = Path(workspace)
    if root.name != "workspace" or root.parent == root:
        return None
    return (
        root.parent.parent
        / SESSION_PENDING_DIRECTORY
        / f"{root.parent.name}{SESSION_PENDING_SUFFIX}"
    )


def _raise_if_session_pending(workspace: Path) -> None:
    pending = _session_pending_path(workspace)
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
    tombstone = _session_tombstone_path(workspace)
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


def require_session_workspace_active(workspace: Path) -> None:
    """Fail before any access when a durable lifecycle fence is present."""

    _raise_if_session_deleted(Path(workspace))
    _raise_if_session_pending(Path(workspace))


def _held_workspace_locks() -> dict[str, tuple[tuple[int, int], int]]:
    process_id = os.getpid()
    if getattr(_THREAD_STATE, "process_id", None) != process_id:
        _THREAD_STATE.process_id = process_id
        _THREAD_STATE.held = {}
    return _THREAD_STATE.held


@contextmanager
def workspace_mutation_guard(
    workspace: Path,
    *,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    """Serialize one synchronous mutation and revalidate after lock wait."""

    root, expected_workspace_identity = _workspace_identity(workspace)
    _raise_if_session_deleted(root)
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
                "Workspace root disappeared during lock acquisition.",
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
        _raise_if_session_deleted(root)
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


async def run_sync_cancellation_safe(
    operation: Callable[[], _ResultT],
) -> _ResultT:
    """Run blocking work off-loop and drain it before re-delivering cancel."""

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
) -> _ResultT:
    """Acquire, mutate, and release entirely inside one bounded worker."""

    def _guarded_operation() -> _ResultT:
        with workspace_mutation_guard(
            workspace,
            timeout_seconds=timeout_seconds,
        ):
            return operation()

    return await run_sync_cancellation_safe(_guarded_operation)
