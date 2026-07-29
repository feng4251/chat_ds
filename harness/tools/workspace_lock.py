"""Bounded cross-process coordination for one session workspace."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import os
import stat
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar


WORKSPACE_MUTATION_LOCK_FILENAME = ".chatds-workspace-mutation.lock"
WORKSPACE_MUTATION_LOCK_ROOT_ENV = "WORKSPACE_MUTATION_LOCK_ROOT"
WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT_ENV = (
    "WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT"
)
WORKSPACE_MUTATION_LOCK_IDENTITY_DOMAIN = (
    b"chatds-workspace-mutation-lock-v1\0"
)
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


def _workspace_session_identity(root: Path) -> tuple[str, str]:
    """Return the canonical logical coordinate shared with the Backend."""

    if (
        root.name != "workspace"
        or root.parent == root
        or root.parent.parent == root.parent
    ):
        raise WorkspaceMutationLockError(
            "Workspace does not have a canonical session coordinate.",
            code="workspace_lock_unsafe",
        )
    user_id = unicodedata.normalize("NFC", root.parent.parent.name)
    session_id = unicodedata.normalize("NFC", root.parent.name)
    for value in (user_id, session_id):
        if (
            not value
            or value in {".", ".."}
            or "\x00" in value
            or Path(value).name != value
        ):
            raise WorkspaceMutationLockError(
                "Workspace session coordinate is unsafe.",
                code="workspace_lock_unsafe",
            )
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WorkspaceMutationLockError(
                "Workspace session coordinate is not valid UTF-8.",
                code="workspace_lock_unsafe",
            ) from exc
        if len(encoded) > 255:
            raise WorkspaceMutationLockError(
                "Workspace session coordinate is too long.",
                code="workspace_lock_unsafe",
            )
    return user_id, session_id


def _workspace_lock_identity(root: Path) -> str:
    user_id, session_id = _workspace_session_identity(root)
    user_bytes = user_id.encode("utf-8")
    session_bytes = session_id.encode("utf-8")
    digest = hashlib.sha256()
    digest.update(WORKSPACE_MUTATION_LOCK_IDENTITY_DOMAIN)
    digest.update(len(user_bytes).to_bytes(4, "big"))
    digest.update(user_bytes)
    digest.update(len(session_bytes).to_bytes(4, "big"))
    digest.update(session_bytes)
    return digest.hexdigest()


def _configured_lock_root_requires_mountpoint() -> bool:
    raw = os.environ.get(
        WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT_ENV,
        "0",
    )
    if raw not in {"0", "1"}:
        raise WorkspaceMutationLockError(
            "Workspace mutation lock mountpoint policy is invalid.",
            code="workspace_lock_unsafe",
        )
    return raw == "1"


def _configured_local_lock_root() -> Path | None:
    raw = os.environ.get(WORKSPACE_MUTATION_LOCK_ROOT_ENV)
    require_mountpoint = _configured_lock_root_requires_mountpoint()
    if raw is None or not raw.strip():
        if require_mountpoint:
            raise WorkspaceMutationLockError(
                "Workspace mutation lock root is required by deployment policy.",
                code="workspace_lock_unsafe",
            )
        return None
    if raw != raw.strip():
        raise WorkspaceMutationLockError(
            "Workspace mutation lock root is not canonical.",
            code="workspace_lock_unsafe",
        )
    candidate = Path(raw)
    if (
        not candidate.is_absolute()
        or candidate == Path("/")
        or ".." in candidate.parts
    ):
        raise WorkspaceMutationLockError(
            "Workspace mutation lock root must be an absolute safe path.",
            code="workspace_lock_unsafe",
        )

    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    parent = candidate.parent
    if parent == candidate:
        raise WorkspaceMutationLockError(
            "Workspace mutation lock root has no safe parent.",
            code="workspace_lock_unsafe",
        )
    descriptor = os.open("/", directory_flags)
    current = Path("/")
    try:
        for component in parent.parts[1:]:
            if (
                not component
                or component in {".", ".."}
                or "\x00" in component
            ):
                raise WorkspaceMutationLockError(
                    "Workspace mutation lock root is unsafe.",
                    code="workspace_lock_unsafe",
                )
            try:
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise WorkspaceMutationLockError(
                    "Workspace mutation lock root contains an unsafe path.",
                    code="workspace_lock_unsafe",
                ) from exc
            try:
                descriptor_stat = os.fstat(child_descriptor)
                path_stat = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(descriptor_stat.st_mode)
                    or not stat.S_ISDIR(path_stat.st_mode)
                    or stat.S_ISLNK(path_stat.st_mode)
                    or (descriptor_stat.st_dev, descriptor_stat.st_ino)
                    != (path_stat.st_dev, path_stat.st_ino)
                ):
                    raise WorkspaceMutationLockError(
                        "Workspace mutation lock root changed during validation.",
                        code="workspace_lock_unsafe",
                    )
            except BaseException:
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
            current /= component

        parent_stat = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_mode & 0o022
            or parent_stat.st_uid != os.geteuid()
        ):
            raise WorkspaceMutationLockError(
                "Workspace mutation lock parent is unsafe.",
                code="workspace_lock_unsafe",
            )
        if require_mountpoint and (
            current == Path("/") or not os.path.ismount(current)
        ):
            raise WorkspaceMutationLockError(
                "Workspace mutation lock parent must be a dedicated mountpoint.",
                code="workspace_lock_unsafe",
            )

        leaf = candidate.name
        if (
            not leaf
            or leaf in {".", ".."}
            or "\x00" in leaf
        ):
            raise WorkspaceMutationLockError(
                "Workspace mutation lock root leaf is unsafe.",
                code="workspace_lock_unsafe",
            )
        try:
            os.mkdir(leaf, mode=0o700, dir_fd=descriptor)
            os.fsync(descriptor)
        except FileExistsError:
            pass
        except OSError as exc:
            raise WorkspaceMutationLockError(
                "Workspace mutation lock root cannot be created safely.",
                code="workspace_lock_io",
            ) from exc
        try:
            root_descriptor = os.open(
                leaf,
                directory_flags,
                dir_fd=descriptor,
            )
        except OSError as exc:
            raise WorkspaceMutationLockError(
                "Workspace mutation lock root is unsafe.",
                code="workspace_lock_unsafe",
            ) from exc
        try:
            root_stat = os.fstat(root_descriptor)
            path_stat = os.stat(
                leaf,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        finally:
            os.close(root_descriptor)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or not stat.S_ISDIR(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or stat.S_IMODE(root_stat.st_mode) != 0o700
            or root_stat.st_uid != os.geteuid()
            or (root_stat.st_dev, root_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise WorkspaceMutationLockError(
                "Workspace mutation lock root must be private and owned.",
                code="workspace_lock_unsafe",
            )
        current /= leaf
    finally:
        os.close(descriptor)
    return candidate


def workspace_mutation_lock_path(workspace: Path) -> Path:
    """Return the deterministic cross-service lock path for ``workspace``.

    A configured local root keeps ``flock`` off a potentially remote workspace
    filesystem.  The legacy sibling path remains only for source compatibility
    when the local lock plane is not configured.
    """

    root, _ = _workspace_identity(workspace)
    lock_root = _configured_local_lock_root()
    if lock_root is None:
        return root.parent / WORKSPACE_MUTATION_LOCK_FILENAME
    return lock_root / f"v1-{_workspace_lock_identity(root)}.lock"


def _lock_file_is_current(
    parent_descriptor: int,
    lock_name: str,
    descriptor_stat: os.stat_result,
) -> bool:
    try:
        path_stat = os.stat(
            lock_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError:
        return False
    return (
        stat.S_ISREG(path_stat.st_mode)
        and path_stat.st_nlink == 1
        and stat.S_IMODE(path_stat.st_mode) == 0o600
        and path_stat.st_uid == os.geteuid()
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
    lock_path = workspace_mutation_lock_path(root)
    lock_key = str(lock_path)
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
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    descriptor: int | None = None
    parent_descriptor: int | None = None
    locked = False
    entered_body = False
    try:
        parent_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            parent_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            parent_flags |= os.O_NOFOLLOW
        parent_descriptor = os.open(lock_path.parent, parent_flags)
        parent_stat = os.fstat(parent_descriptor)
        lexical_parent_stat = lock_path.parent.lstat()
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or not stat.S_ISDIR(lexical_parent_stat.st_mode)
            or stat.S_ISLNK(lexical_parent_stat.st_mode)
            or (parent_stat.st_dev, parent_stat.st_ino)
            != (lexical_parent_stat.st_dev, lexical_parent_stat.st_ino)
        ):
            raise WorkspaceMutationLockError(
                "Workspace mutation lock directory is unsafe.",
                code="workspace_lock_unsafe",
            )
        descriptor = os.open(
            lock_path.name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        descriptor_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or descriptor_stat.st_nlink != 1
            or stat.S_IMODE(descriptor_stat.st_mode) != 0o600
            or descriptor_stat.st_uid != os.geteuid()
            or not _lock_file_is_current(
                parent_descriptor,
                lock_path.name,
                descriptor_stat,
            )
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
            or not _lock_file_is_current(
                parent_descriptor,
                lock_path.name,
                descriptor_stat,
            )
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
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
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
