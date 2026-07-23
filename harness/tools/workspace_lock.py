"""Short cross-process critical section for one session workspace.

Skill executors work on private snapshots and may run concurrently. Only the
final compare-and-swap/apply step is serialized with ordinary workspace file
tools. Keeping the lock out of the workspace prevents model-authored paths
from replacing or deleting the coordination file.
"""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class WorkspaceMutationLockError(RuntimeError):
    """The runtime-owned workspace lock could not be acquired safely."""


@contextmanager
def workspace_mutation_guard(workspace: Path) -> Iterator[None]:
    """Serialize the brief filesystem mutation phase for ``workspace``.

    This advisory lock is shared by Harness processes using this helper.
    Long-running Skill computation happens before entering the guard; callers
    should hold it only while revalidating and replacing workspace files.
    """

    try:
        root = Path(workspace).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceMutationLockError(
            "Workspace root is unavailable for mutation locking."
        ) from exc
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise WorkspaceMutationLockError(
            "Workspace root cannot be inspected for mutation locking."
        ) from exc
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
        raise WorkspaceMutationLockError(
            "Workspace root must be a real directory for mutation locking."
        )

    lock_path = root.parent / ".chatds-workspace-mutation.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    descriptor: int | None = None
    locked = False
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_nlink != 1
            or lock_stat.st_mode & 0o077
        ):
            raise WorkspaceMutationLockError(
                "Workspace mutation lock must be a private regular file."
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    except WorkspaceMutationLockError:
        raise
    except OSError as exc:
        raise WorkspaceMutationLockError(
            "Workspace mutation lock could not be acquired safely."
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
