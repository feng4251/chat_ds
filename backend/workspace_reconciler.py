"""Crash-durable cleanup for session trees whose database owner is gone."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import threading
import logging
from pathlib import Path

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import workspace as workspace_store
from database import async_session
from models import Conversation, User
from workspace_lock import (
    WORKSPACE_MUTATION_LOCK_FILENAME,
    WorkspaceMutationLockError,
    run_sync_cancellation_safe,
    workspace_mutation_guard,
)


DEFAULT_ORPHAN_RECONCILE_LIMIT = 256
MAX_ORPHAN_RECONCILE_LIMIT = 4096
DEFAULT_ORPHAN_LOCK_TIMEOUT_SECONDS = 0.25
DEFAULT_ORPHAN_RECONCILE_INTERVAL_SECONDS = 60.0
MIN_ORPHAN_RECONCILE_INTERVAL_SECONDS = 1.0
MAX_ORPHAN_RECONCILE_INTERVAL_SECONDS = 3600.0
MAX_LIVE_CONVERSATION_QUERY_BATCH = 400
MAX_PENDING_FORK_JOURNALS_PER_CANDIDATE = 4096

logger = logging.getLogger(__name__)


def _configured_limit() -> int:
    try:
        value = int(
            os.environ.get(
                "WORKSPACE_ORPHAN_RECONCILE_LIMIT",
                str(DEFAULT_ORPHAN_RECONCILE_LIMIT),
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_ORPHAN_RECONCILE_LIMIT
    return max(1, min(value, MAX_ORPHAN_RECONCILE_LIMIT))


def _configured_lock_timeout() -> float:
    try:
        value = float(
            os.environ.get(
                "WORKSPACE_ORPHAN_LOCK_TIMEOUT_SECONDS",
                str(DEFAULT_ORPHAN_LOCK_TIMEOUT_SECONDS),
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_ORPHAN_LOCK_TIMEOUT_SECONDS
    return max(0.0, min(value, 60.0))


class _DiscoveryCursor:
    """Stateful scandir cursor: every tick performs bounded directory work."""

    def __init__(self, root: Path, identity: tuple[int, int]) -> None:
        self.root = root
        self.identity = identity
        self.users = os.scandir(root)
        self.sessions = None
        self.user_id: str | None = None

    def close(self) -> None:
        if self.sessions is not None:
            self.sessions.close()
            self.sessions = None
        self.users.close()


_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_CURSOR: _DiscoveryCursor | None = None
_DEFERRED_RETRY: dict[
    tuple[str, int, int],
    list[tuple[str, str]],
] = {}
_DEFERRED_RETRY_TURN: dict[tuple[str, int, int], bool] = {}


def _next_session_candidate_batch() -> tuple[
    list[tuple[str, str]],
    bool,
    int,
]:
    """Return at most limit candidates and inspect at most 4x limit entries."""

    global _DISCOVERY_CURSOR
    root = workspace_store._workspace_root_directory()
    root_stat = root.stat()
    identity = (root_stat.st_dev, root_stat.st_ino)
    root_key = (str(root), *identity)
    limit = _configured_limit()
    inspection_budget = max(limit, min(limit * 4, 16384))
    candidates: list[tuple[str, str]] = []
    inspected = 0
    cycle_complete = False

    with _DISCOVERY_LOCK:
        deferred = _DEFERRED_RETRY.get(root_key, [])
        retry_turn = not _DEFERRED_RETRY_TURN.get(root_key, False)
        if deferred and retry_turn:
            _DEFERRED_RETRY_TURN[root_key] = True
            candidates = deferred[:limit]
            remaining = deferred[limit:]
            if remaining:
                _DEFERRED_RETRY[root_key] = remaining
            else:
                _DEFERRED_RETRY.pop(root_key, None)
            return candidates, False, 0
        _DEFERRED_RETRY_TURN[root_key] = False
        cursor = _DISCOVERY_CURSOR
        if (
            cursor is None
            or cursor.root != root
            or cursor.identity != identity
        ):
            if cursor is not None:
                cursor.close()
            cursor = _DiscoveryCursor(root, identity)
            _DISCOVERY_CURSOR = cursor

        while inspected < inspection_budget and len(candidates) < limit:
            if cursor.sessions is not None:
                try:
                    entry = next(cursor.sessions)
                    inspected += 1
                except StopIteration:
                    cursor.sessions.close()
                    cursor.sessions = None
                    cursor.user_id = None
                    continue
                if (
                    entry.name in {
                        workspace_store.SESSION_TOMBSTONE_DIRECTORY,
                        workspace_store.SESSION_PENDING_DIRECTORY,
                    }
                    or not entry.is_dir(follow_symlinks=False)
                ):
                    continue
                try:
                    session_id = workspace_store._safe_identity_component(
                        entry.name,
                        field="session_id",
                    )
                except ValueError:
                    continue
                candidates.append((str(cursor.user_id), session_id))
                continue

            try:
                user_entry = next(cursor.users)
                inspected += 1
            except StopIteration:
                cursor.close()
                _DISCOVERY_CURSOR = None
                cycle_complete = True
                break
            if not user_entry.is_dir(follow_symlinks=False):
                continue
            try:
                user_id = workspace_store._safe_identity_component(
                    user_entry.name,
                    field="user_id",
                )
            except ValueError:
                continue
            cursor.user_id = user_id
            cursor.sessions = os.scandir(user_entry.path)
    return candidates, cycle_complete, inspected


def _queue_deferred_candidate(
    user_id: str,
    session_id: str,
) -> None:
    root = workspace_store._workspace_root_directory()
    root_stat = root.stat()
    root_key = (str(root), root_stat.st_dev, root_stat.st_ino)
    candidate = (str(user_id), str(session_id))
    with _DISCOVERY_LOCK:
        queue = _DEFERRED_RETRY.setdefault(root_key, [])
        if candidate not in queue:
            queue.append(candidate)


def _remove_orphan_session_tree(
    user_id: str,
    session_id: str,
) -> str:
    """Remove one fenced tree; never call this without a durable tombstone."""

    if not workspace_store.session_deletion_tombstone_exists(
        user_id,
        session_id,
    ):
        return "not_fenced"
    skill_cleanup = _remove_orphan_session_skills(
        user_id,
        session_id,
    )
    if skill_cleanup is not None:
        return skill_cleanup
    session_dir = (
        workspace_store.WORKSPACE_ROOT / user_id / session_id
    )
    if session_dir.is_symlink():
        return "unsafe_session_tree"
    workspace = session_dir / "workspace"
    if workspace.exists() or workspace.is_symlink():
        try:
            with workspace_mutation_guard(
                workspace,
                timeout_seconds=_configured_lock_timeout(),
                allow_deleted=True,
                allow_pending=True,
            ):
                shutil.rmtree(workspace)
        except WorkspaceMutationLockError as exc:
            return f"deferred:{exc.code}"
        except OSError:
            return "deferred:workspace_cleanup_io"

    # The durable marker blocks every new Backend/Harness workspace create and
    # every post-wait mutation boundary. It is now safe to remove the sibling
    # lock and the rest of the no-longer-owned session tree.
    lock_path = session_dir / WORKSPACE_MUTATION_LOCK_FILENAME
    if lock_path.exists() or lock_path.is_symlink():
        try:
            lock_stat = lock_path.lstat()
        except OSError:
            return "deferred:lock_inspection_io"
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or stat.S_ISLNK(lock_stat.st_mode)
            or lock_stat.st_nlink != 1
            or lock_stat.st_mode & 0o077
        ):
            return "deferred:unsafe_lock"
        try:
            lock_path.unlink()
        except OSError:
            return "deferred:lock_cleanup_io"
    if session_dir.exists():
        try:
            shutil.rmtree(session_dir)
        except OSError:
            return "deferred:session_cleanup_io"
    try:
        workspace_store.clear_session_pending_fence_after_deletion(
            user_id,
            session_id,
        )
    except WorkspaceMutationLockError as exc:
        return f"deferred:{exc.code}"
    return "removed"


def _remove_orphan_session_skills(
    user_id: str,
    session_id: str,
) -> str | None:
    """Remove one session Skill scope without following any path component."""

    from routers import skill_router as skill_api

    safe_user = workspace_store._safe_identity_component(
        user_id,
        field="user_id",
    )
    safe_session = workspace_store._safe_identity_component(
        session_id,
        field="session_id",
    )
    root = Path(skill_api.SKILLS_DATA_DIR)
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return "deferred:skill_root_inspection_io"
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        return "deferred:unsafe_skill_root"
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    root_fd: int | None = None
    user_fd: int | None = None
    try:
        root_fd = os.open(root, flags)
        opened_root_stat = os.fstat(root_fd)
        if (
            opened_root_stat.st_dev,
            opened_root_stat.st_ino,
        ) != (
            root_stat.st_dev,
            root_stat.st_ino,
        ):
            return "deferred:unsafe_skill_root"
        try:
            user_fd = os.open(safe_user, flags, dir_fd=root_fd)
        except FileNotFoundError:
            return None
        try:
            session_stat = os.stat(
                safe_session,
                dir_fd=user_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISDIR(session_stat.st_mode)
            or stat.S_ISLNK(session_stat.st_mode)
        ):
            return "deferred:unsafe_skill_session"
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            return "deferred:unsafe_skill_cleanup_runtime"
        shutil.rmtree(safe_session, dir_fd=user_fd)
        os.fsync(user_fd)
        return None
    except OSError:
        return "deferred:skill_cleanup_io"
    finally:
        if user_fd is not None:
            os.close(user_fd)
        if root_fd is not None:
            os.close(root_fd)


async def cleanup_deleted_session_workspace(
    user_id: str,
    session_id: str,
) -> str:
    """Cleanup after a committed delete; the caller already published fence."""

    return await run_sync_cancellation_safe(
        lambda: _remove_orphan_session_tree(user_id, session_id)
    )


async def _live_conversation_keys(
    db: AsyncSession,
    candidates: list[tuple[str, str]],
) -> set[tuple[str, str]]:
    if not candidates:
        return set()
    live: set[tuple[str, str]] = set()
    for offset in range(
        0,
        len(candidates),
        MAX_LIVE_CONVERSATION_QUERY_BATCH,
    ):
        batch = candidates[
            offset:offset + MAX_LIVE_CONVERSATION_QUERY_BATCH
        ]
        clauses = [
            and_(
                Conversation.user_id == user_id,
                Conversation.id == session_id,
            )
            for user_id, session_id in batch
        ]
        rows = (
            await db.execute(
                select(Conversation.user_id, Conversation.id).where(
                    or_(*clauses)
                )
            )
        ).all()
        live.update(
            (str(user_id), str(session_id))
            for user_id, session_id in rows
        )
    return live


async def _conversation_owner_is_active(
    user_id: str,
    session_id: str,
) -> bool:
    """Recheck one workspace owner from a fresh post-lock DB snapshot.

    The bounded discovery query is only a hint.  Fork publishes its filesystem
    before committing the Conversation row, so reusing that earlier session
    could retain a stale SQLite read transaction after the fork lifecycle lock
    is released.  A fresh session created *after* acquiring the shared
    session/Skill lifecycle lock observes the linearized owner state.
    """

    async with async_session() as verification_db:
        owner = (
            await verification_db.execute(
                select(Conversation.id)
                .join(User, User.id == Conversation.user_id)
                .where(
                    Conversation.id == str(session_id),
                    Conversation.user_id == str(user_id),
                )
            )
        ).scalar_one_or_none()
    return owner is not None


def _read_bounded_regular_json(path: Path) -> dict | None:
    """Read a small journal without following the journal file itself."""

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        descriptor_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or descriptor_stat.st_nlink != 1
            or descriptor_stat.st_size > 256 * 1024
        ):
            return None
        raw = os.read(descriptor, 256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            return None
        path_stat = path.lstat()
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino)
            != (descriptor_stat.st_dev, descriptor_stat.st_ino)
        ):
            return None
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _recoverable_pending_fork_state(
    user_id: str,
    target_id: str,
) -> bool | None:
    """Classify a DB-absent pending target without destroying exact recovery.

    ``True`` means an exact fork journal, lifecycle marker, and immutable
    filesystem snapshot form a recoverable transaction. ``False`` means the
    bounded operation directory was inspected and no such transaction exists.
    ``None`` is deliberately fail-closed: an unsafe or unbounded operation
    scope is deferred rather than converted into a deletion.
    """

    from routers import skill_router as skill_api
    from routers import workspace_router as workspace_api

    safe_user = workspace_store._safe_identity_component(
        user_id,
        field="user_id",
    )
    safe_target = workspace_store._safe_identity_component(
        target_id,
        field="session_id",
    )
    operations_root = (
        Path(skill_api.SKILLS_DATA_DIR)
        / safe_user
        / ".chatds_operations"
    )
    try:
        root_stat = operations_root.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_ISLNK(root_stat.st_mode)
    ):
        return None

    inspected = 0
    try:
        entries = os.scandir(operations_root)
    except OSError:
        return None
    with entries:
        for entry in entries:
            if not entry.name.startswith("fork-"):
                continue
            inspected += 1
            if inspected > MAX_PENDING_FORK_JOURNALS_PER_CANDIDATE:
                return None
            if (
                re.fullmatch(r"fork-[a-f0-9]{64}", entry.name) is None
                or not entry.is_dir(follow_symlinks=False)
            ):
                continue
            operation_dir = operations_root / entry.name
            journal = _read_bounded_regular_json(
                operation_dir / "journal.json"
            )
            if not isinstance(journal, dict):
                continue
            source_id = str(journal.get("source_id") or "")
            snapshot_sha256 = str(
                journal.get("snapshot_sha256") or ""
            )
            workspace_digest = str(
                journal.get("workspace_digest") or ""
            )
            skills_digest = str(journal.get("skills_digest") or "")
            try:
                safe_source = workspace_store._safe_identity_component(
                    source_id,
                    field="source_id",
                )
            except ValueError:
                continue
            if (
                journal.get("version") != 1
                or journal.get("kind") != "fork"
                or journal.get("user_id") != safe_user
                or safe_source == safe_target
                or journal.get("target_id") != safe_target
                or type(journal.get("include_messages")) is not bool
                or not isinstance(journal.get("title"), str)
                or journal.get("state") not in {"prepared", "published"}
                or re.fullmatch(r"[a-f0-9]{64}", snapshot_sha256) is None
                or re.fullmatch(r"[a-f0-9]{64}", workspace_digest) is None
                or re.fullmatch(r"[a-f0-9]{64}", skills_digest) is None
            ):
                continue
            expected_operation_dir = skill_api._skill_operation_dir(
                user_id=safe_user,
                kind="fork",
                identity_parts=[safe_source, safe_target],
            )
            if operation_dir != expected_operation_dir:
                continue
            pending_operation_id = (
                workspace_api._fork_lifecycle_operation_id(
                    user_id=safe_user,
                    source_id=safe_source,
                    target_id=safe_target,
                    snapshot_sha256=snapshot_sha256,
                )
            )
            try:
                if not workspace_store.session_pending_fence_matches(
                    safe_user,
                    safe_target,
                    pending_operation_id,
                ):
                    continue
                target_workspace_root = (
                    workspace_store.WORKSPACE_ROOT
                    / safe_user
                    / safe_target
                )
                target_skill_dir = (
                    Path(skill_api.SKILLS_DATA_DIR)
                    / safe_user
                    / safe_target
                )
                if journal["state"] == "published":
                    return workspace_api._verify_committed_fork_filesystem(
                        target_workspace_root=target_workspace_root,
                        target_skill_dir=target_skill_dir,
                        journal=journal,
                        skill_api=skill_api,
                    )
                staging_root = operation_dir / "staging"
                workspace_available = (
                    workspace_api._fork_directory_matches(
                        target_workspace_root,
                        workspace_digest,
                        skill_api,
                        ignore_workspace_lock=True,
                    )
                    or workspace_api._fork_directory_matches(
                        staging_root / "workspace-session",
                        workspace_digest,
                        skill_api,
                        ignore_workspace_lock=True,
                    )
                )
                skills_available = (
                    workspace_api._fork_directory_matches(
                        target_skill_dir,
                        skills_digest,
                        skill_api,
                    )
                    or workspace_api._fork_directory_matches(
                        staging_root / "skills",
                        skills_digest,
                        skill_api,
                    )
                )
                if workspace_available and skills_available:
                    return True
            except (OSError, ValueError, WorkspaceMutationLockError):
                return None
            except Exception:
                # Digest validation can raise an HTTP error for a malformed
                # tree. That is a completed negative classification, not a
                # reason to trust the transaction.
                continue
    return False


async def _reconcile_session_candidate(
    user_id: str,
    session_id: str,
    *,
    clear_live_tombstones: bool,
) -> tuple[str, bool, str | None]:
    """Linearize one candidate with fork/delete/install before mutating it."""

    # Import lazily: skill_router already owns the canonical per-scope lock and
    # imports workspace helpers during Backend startup.
    from routers import skill_router as skill_api

    async with skill_api._skill_install_lock(user_id, session_id):
        if await _conversation_owner_is_active(user_id, session_id):
            cleared = False
            if clear_live_tombstones:
                cleared = await (
                    workspace_store
                    .clear_stale_session_deletion_tombstone_async(
                        user_id,
                        session_id,
                    )
                )
            return "live", cleared, None

        try:
            pending_exists = await run_sync_cancellation_safe(
                lambda: (
                    workspace_store._read_session_pending_fence(
                        user_id,
                        session_id,
                    )
                    is not None
                )
            )
        except WorkspaceMutationLockError as exc:
            return "pending", False, f"deferred:{exc.code}"
        if pending_exists:
            recovery_state = await run_sync_cancellation_safe(
                lambda: _recoverable_pending_fork_state(
                    user_id,
                    session_id,
                )
            )
            if recovery_state is True:
                return (
                    "pending",
                    False,
                    "deferred:recoverable_pending_fork",
                )
            if recovery_state is None:
                return (
                    "pending",
                    False,
                    "deferred:pending_recovery_inspection",
                )

        fenced = False
        marker_exists = await run_sync_cancellation_safe(
            lambda: workspace_store.session_deletion_tombstone_exists(
                user_id,
                session_id,
            )
        )
        if not marker_exists:
            await workspace_store.publish_session_deletion_tombstone_async(
                user_id,
                session_id,
            )
            fenced = True
        outcome = await cleanup_deleted_session_workspace(
            user_id,
            session_id,
        )
        return "orphan", fenced, outcome


async def reconcile_orphan_session_workspaces(
    db: AsyncSession | None = None,
    *,
    clear_live_tombstones: bool = True,
) -> dict[str, int]:
    """Reconcile a bounded startup cohort using the database as authority."""

    discovered, cycle_complete, inspected = await run_sync_cancellation_safe(
        _next_session_candidate_batch
    )
    owns_session = db is None
    if db is None:
        db = async_session()
    try:
        snapshot_live = await _live_conversation_keys(db, discovered)
    finally:
        if owns_session:
            await db.close()

    counts = {
        "candidates": 0,
        "scanned": len(discovered),
        "inspected_entries": inspected,
        "cycle_complete": int(cycle_complete),
        "snapshot_live": len(snapshot_live),
        "snapshot_owner_drift": 0,
        "live": 0,
        "recoverable_pending": 0,
        "removed": 0,
        "deferred": 0,
        "fenced": 0,
        "stale_tombstones_cleared": 0,
    }
    # ``snapshot_live`` is deliberately not authoritative. Every discovered
    # coordinate enters the same lifecycle lock used by fork/delete/install,
    # then opens a fresh exact DB snapshot before a fence can be published or
    # cleared. This closes filesystem-publish -> DB-commit races in both
    # directions while retaining the bounded batch query for observability.
    for user_id, session_id in discovered:
        state, fenced, outcome = await _reconcile_session_candidate(
            user_id,
            session_id,
            clear_live_tombstones=clear_live_tombstones,
        )
        if ((user_id, session_id) in snapshot_live) != (state == "live"):
            counts["snapshot_owner_drift"] += 1
        if state == "live":
            counts["live"] += 1
            if fenced:
                counts["stale_tombstones_cleared"] += 1
            continue
        if state == "pending":
            counts["recoverable_pending"] += int(
                outcome == "deferred:recoverable_pending_fork"
            )

        counts["candidates"] += 1
        if fenced:
            counts["fenced"] += 1
        if outcome == "removed":
            counts["removed"] += 1
        else:
            counts["deferred"] += 1
            await run_sync_cancellation_safe(
                lambda user_id=user_id, session_id=session_id: (
                    _queue_deferred_candidate(user_id, session_id)
                )
            )
    return counts


def _configured_interval_seconds() -> float:
    try:
        value = float(
            os.environ.get(
                "WORKSPACE_ORPHAN_RECONCILE_INTERVAL_SECONDS",
                str(DEFAULT_ORPHAN_RECONCILE_INTERVAL_SECONDS),
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_ORPHAN_RECONCILE_INTERVAL_SECONDS
    return max(
        MIN_ORPHAN_RECONCILE_INTERVAL_SECONDS,
        min(value, MAX_ORPHAN_RECONCILE_INTERVAL_SECONDS),
    )


async def periodic_workspace_reconciler(
    stop_event: asyncio.Event,
    *,
    interval_seconds: float | None = None,
) -> None:
    """Low-frequency single-flight reconciliation until shutdown."""

    interval = (
        _configured_interval_seconds()
        if interval_seconds is None
        else max(
            MIN_ORPHAN_RECONCILE_INTERVAL_SECONDS,
            min(float(interval_seconds), MAX_ORPHAN_RECONCILE_INTERVAL_SECONDS),
        )
    )
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            continue
        except asyncio.TimeoutError:
            pass
        try:
            result = await reconcile_orphan_session_workspaces(
                # Only pre-boot markers may be repaired as stale. A marker
                # published by an in-flight delete in this process is never
                # cleared while its DB row is still visible.
                clear_live_tombstones=True,
            )
            logger.info("Periodic workspace reconcile: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic workspace reconcile failed")
