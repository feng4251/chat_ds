import asyncio
import hashlib
import importlib.util
import json
import logging
import multiprocessing
import os
import queue
import stat
import time
import traceback
import unicodedata
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import main as backend_main
import workspace
import workspace_lock
import workspace_reconciler
import stream_observability
from main import workspace_mutation_lock_error_handler
from routers import conv_router, workspace_router
from schemas import WorkspaceFileWrite
from workspace_reconciler import (
    _live_conversation_keys,
    cleanup_deleted_session_workspace,
    reconcile_orphan_session_workspaces,
)
from workspace_lock import (
    WORKSPACE_MUTATION_LOCK_IDENTITY_DOMAIN,
    WorkspaceMutationLockError,
    workspace_mutation_guard,
    workspace_mutation_lock_path,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_pytest_storage_defaults_are_isolated_from_production() -> None:
    from routers import skill_router

    production_root = Path("/nfs/temp/chat_ds")
    assert workspace.WORKSPACE_ROOT != production_root
    assert conv_router.SANDBOX_BASE == workspace.WORKSPACE_ROOT
    assert skill_router.SKILLS_DATA_DIR != REPOSITORY_ROOT / "data" / "skills"
    assert workspace_reconciler._DISCOVERY_CURSOR is None
    assert workspace_reconciler._DEFERRED_RETRY == {}
    assert workspace_reconciler._DEFERRED_RETRY_TURN == {}


@pytest.fixture(autouse=True)
def local_workspace_lock_plane(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Exercise Backend tests on the production-style local lock plane."""

    lock_root = tmp_path_factory.mktemp("workspace-mutation-locks")
    lock_root.chmod(0o700)
    monkeypatch.setenv("WORKSPACE_MUTATION_LOCK_ROOT", str(lock_root))
    return lock_root


def _acquire_harness_workspace_lock(
    workspace_path: str,
    events,
    release=None,
) -> None:
    """Use the real Harness helper in a separate process."""

    module_path = REPOSITORY_ROOT / "harness" / "tools" / "workspace_lock.py"
    spec = importlib.util.spec_from_file_location(
        "chatds_harness_workspace_lock",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Harness workspace lock module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    harness_guard = module.workspace_mutation_guard

    events.put("waiting")
    with harness_guard(Path(workspace_path)):
        events.put("acquired")
        if release is not None:
            release.wait(timeout=5)


def _harness_compare_and_swap(
    workspace_path: str,
    relative_path: str,
    replacement: str,
    events,
    start,
) -> None:
    """Model the Harness digest revalidation inside its real mutation guard."""

    module_path = REPOSITORY_ROOT / "harness" / "tools" / "workspace_lock.py"
    spec = importlib.util.spec_from_file_location(
        "chatds_harness_workspace_lock_cas",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Harness workspace lock module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    workspace_root = Path(workspace_path)
    target = workspace_root / relative_path
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    events.put("snapshot")
    if not start.wait(timeout=5):
        raise RuntimeError("CAS test was not started")
    events.put("waiting")
    with module.workspace_mutation_guard(workspace_root):
        current = hashlib.sha256(target.read_bytes()).hexdigest()
        if current != expected:
            events.put("conflict")
            return
        target.write_text(replacement, encoding="utf-8")
        events.put("committed")


def _harness_wait_then_write(
    workspace_path: str,
    events,
) -> None:
    module_path = REPOSITORY_ROOT / "harness" / "tools" / "workspace_lock.py"
    spec = importlib.util.spec_from_file_location(
        "chatds_harness_workspace_lock_waiter",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Harness workspace lock module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workspace_root = Path(workspace_path)
    events.put("waiting")
    try:
        with module.workspace_mutation_guard(
            workspace_root,
            timeout_seconds=2,
        ):
            (workspace_root / "revived.md").write_text(
                "must-not-commit",
                encoding="utf-8",
            )
            events.put("committed")
    except module.WorkspaceMutationLockError as exc:
        events.put(f"rejected:{exc.code}")


@pytest.fixture
def workspace_path(tmp_path: Path) -> Path:
    path = tmp_path / "user" / "session" / "workspace"
    path.mkdir(parents=True)
    return path


def test_backend_lock_is_private_and_rejects_symlink(
    workspace_path: Path,
) -> None:
    lock_path = workspace_mutation_lock_path(workspace_path)
    with workspace_mutation_guard(workspace_path):
        lock_stat = lock_path.stat()
        assert stat.S_ISREG(lock_stat.st_mode)
        assert stat.S_IMODE(lock_stat.st_mode) == 0o600
        assert lock_path.parent != workspace_path.parent
        assert not (
            workspace_path.parent / ".chatds-workspace-mutation.lock"
        ).exists()

    lock_path.unlink()
    target = lock_path.parent / "attacker-file"
    target.write_text("x", encoding="utf-8")
    lock_path.symlink_to(target)
    with pytest.raises(WorkspaceMutationLockError) as exc_info:
        with workspace_mutation_guard(workspace_path):
            raise AssertionError("unsafe lock must not be entered")
    assert exc_info.value.code == "workspace_lock_unsafe"


def test_local_lock_identity_is_content_addressed_and_nfc_stable(
    tmp_path: Path,
    local_workspace_lock_plane: Path,
) -> None:
    composed = tmp_path / "usér" / "session" / "workspace"
    decomposed = (
        tmp_path
        / unicodedata.normalize("NFD", "usér")
        / "session"
        / "workspace"
    )
    composed.mkdir(parents=True)
    decomposed.mkdir(parents=True)

    user_bytes = "usér".encode("utf-8")
    session_bytes = b"session"
    expected = hashlib.sha256(
        WORKSPACE_MUTATION_LOCK_IDENTITY_DOMAIN
        + len(user_bytes).to_bytes(4, "big")
        + user_bytes
        + len(session_bytes).to_bytes(4, "big")
        + session_bytes
    ).hexdigest()
    expected_path = local_workspace_lock_plane / f"v1-{expected}.lock"
    assert workspace_mutation_lock_path(composed) == expected_path
    assert workspace_mutation_lock_path(decomposed) == expected_path
    module_path = REPOSITORY_ROOT / "harness" / "tools" / "workspace_lock.py"
    spec = importlib.util.spec_from_file_location(
        "chatds_harness_workspace_lock_identity",
        module_path,
    )
    assert spec is not None and spec.loader is not None
    harness_lock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness_lock)
    assert harness_lock.workspace_mutation_lock_path(composed) == expected_path
    assert (
        harness_lock.workspace_mutation_lock_path(decomposed)
        == expected_path
    )


def test_local_lock_root_rejects_relative_symlink_and_writable_roots(
    workspace_path: Path,
    tmp_path: Path,
) -> None:
    with (
        patch.dict(
            os.environ,
            {"WORKSPACE_MUTATION_LOCK_ROOT": "relative/locks"},
        ),
        pytest.raises(WorkspaceMutationLockError) as relative_error,
    ):
        workspace_mutation_lock_path(workspace_path)
    assert relative_error.value.code == "workspace_lock_unsafe"

    real_root = tmp_path / "real-lock-root"
    real_root.mkdir(mode=0o700)
    linked_root = tmp_path / "linked-lock-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with (
        patch.dict(
            os.environ,
            {"WORKSPACE_MUTATION_LOCK_ROOT": str(linked_root)},
        ),
        pytest.raises(WorkspaceMutationLockError) as symlink_error,
    ):
        workspace_mutation_lock_path(workspace_path)
    assert symlink_error.value.code == "workspace_lock_unsafe"

    writable_root = tmp_path / "writable-lock-root"
    writable_root.mkdir(mode=0o777)
    writable_root.chmod(0o777)
    with (
        patch.dict(
            os.environ,
            {"WORKSPACE_MUTATION_LOCK_ROOT": str(writable_root)},
        ),
        pytest.raises(WorkspaceMutationLockError) as writable_error,
    ):
        workspace_mutation_lock_path(workspace_path)
    assert writable_error.value.code == "workspace_lock_unsafe"

    with (
        patch.dict(
            os.environ,
            {
                "WORKSPACE_MUTATION_LOCK_ROOT": str(
                    tmp_path / "missing-parent" / "locks"
                )
            },
        ),
        pytest.raises(WorkspaceMutationLockError) as missing_parent_error,
    ):
        workspace_mutation_lock_path(workspace_path)
    assert missing_parent_error.value.code == "workspace_lock_unsafe"

    with (
        patch.dict(
            os.environ,
            {
                "WORKSPACE_MUTATION_LOCK_ROOT": str(real_root),
                "WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT": "1",
            },
        ),
        pytest.raises(WorkspaceMutationLockError) as mountpoint_error,
    ):
        workspace_mutation_lock_path(workspace_path)
    assert mountpoint_error.value.code == "workspace_lock_unsafe"

    with (
        patch.dict(
            os.environ,
            {"WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT": "1"},
        ),
        patch.dict(
            os.environ,
            {"WORKSPACE_MUTATION_LOCK_ROOT": ""},
        ),
        pytest.raises(WorkspaceMutationLockError) as missing_root_error,
    ):
        workspace_mutation_lock_path(workspace_path)
    assert missing_root_error.value.code == "workspace_lock_unsafe"


def test_local_plane_flock_descriptor_never_targets_workspace_storage(
    workspace_path: Path,
    local_workspace_lock_plane: Path,
) -> None:
    observed: list[Path] = []
    real_flock = workspace_lock.fcntl.flock

    def _tracking_flock(descriptor: int, operation: int) -> None:
        observed.append(
            Path(os.readlink(f"/proc/self/fd/{descriptor}")).resolve()
        )
        real_flock(descriptor, operation)

    with patch.object(
        workspace_lock.fcntl,
        "flock",
        side_effect=_tracking_flock,
    ):
        with workspace_mutation_guard(workspace_path):
            pass

    assert observed
    assert all(path.parent == local_workspace_lock_plane for path in observed)
    assert all(workspace_path.parent not in path.parents for path in observed)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX flock")
def test_backend_and_harness_helpers_serialize_across_processes(
    workspace_path: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    events = context.Queue()
    process = None

    with workspace_mutation_guard(workspace_path):
        process = context.Process(
            target=_acquire_harness_workspace_lock,
            args=(str(workspace_path), events),
        )
        process.start()
        assert events.get(timeout=2) == "waiting"
        with pytest.raises(queue.Empty):
            events.get(timeout=0.15)

    assert events.get(timeout=2) == "acquired"
    process.join(timeout=2)
    assert not process.is_alive()
    assert process.exitcode == 0


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX flock")
def test_backend_mutation_becomes_harness_cas_conflict_not_lost_update(
    workspace_path: Path,
) -> None:
    target = workspace_path / "report.md"
    target.write_text("baseline", encoding="utf-8")
    context = multiprocessing.get_context("fork")
    events = context.Queue()
    start = context.Event()
    process = context.Process(
        target=_harness_compare_and_swap,
        args=(
            str(workspace_path),
            "report.md",
            "harness-overwrite",
            events,
            start,
        ),
    )
    process.start()
    assert events.get(timeout=2) == "snapshot"

    with workspace_mutation_guard(workspace_path):
        workspace.atomic_write_text(target, "backend-save")
        start.set()
        assert events.get(timeout=2) == "waiting"
        with pytest.raises(queue.Empty):
            events.get(timeout=0.15)

    assert events.get(timeout=2) == "conflict"
    process.join(timeout=2)
    assert not process.is_alive()
    assert process.exitcode == 0
    assert target.read_text(encoding="utf-8") == "backend-save"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX flock")
def test_backend_wait_is_bounded_while_harness_holds_lock(
    workspace_path: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    events = context.Queue()
    release = context.Event()
    process = context.Process(
        target=_acquire_harness_workspace_lock,
        args=(str(workspace_path), events, release),
    )
    process.start()
    assert events.get(timeout=2) == "waiting"
    assert events.get(timeout=2) == "acquired"

    started = time.monotonic()
    try:
        with pytest.raises(WorkspaceMutationLockError) as exc_info:
            with workspace_mutation_guard(
                workspace_path,
                timeout_seconds=0.05,
            ):
                raise AssertionError("contended lock must not be entered")
        assert exc_info.value.code == "workspace_lock_timeout"
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
    assert process.exitcode == 0


def test_bootstrap_creation_uses_shared_lock(
    tmp_path: Path,
) -> None:
    with patch.object(workspace, "WORKSPACE_ROOT", tmp_path):
        root = workspace.workspace_dir("user", "session")
        context = multiprocessing.get_context("fork")
        events = context.Queue()
        release = context.Event()
        process = context.Process(
            target=_acquire_harness_workspace_lock,
            args=(str(root), events, release),
        )
        process.start()
        assert events.get(timeout=2) == "waiting"
        assert events.get(timeout=2) == "acquired"
        try:
            with (
                patch.dict(
                    os.environ,
                    {"WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS": "0.05"},
                ),
                pytest.raises(WorkspaceMutationLockError) as exc_info,
            ):
                workspace.ensure_workspace("user", "session")
            assert exc_info.value.code == "workspace_lock_timeout"
            assert not (root / "AGENTS.md").exists()
        finally:
            release.set()
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        assert process.exitcode == 0

        completed = workspace.ensure_workspace("user", "session")
        assert (completed / "AGENTS.md").is_file()
        assert (completed / ".gitignore").is_file()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX flock")
def test_atomic_workspace_write_cannot_bypass_harness_lock(
    tmp_path: Path,
) -> None:
    with patch.object(workspace, "WORKSPACE_ROOT", tmp_path):
        root = workspace.ensure_workspace("user", "session")
        context = multiprocessing.get_context("fork")
        events = context.Queue()
        release = context.Event()
        process = context.Process(
            target=_acquire_harness_workspace_lock,
            args=(str(root), events, release),
        )
        process.start()
        assert events.get(timeout=2) == "waiting"
        assert events.get(timeout=2) == "acquired"
        try:
            with (
                patch.dict(
                    os.environ,
                    {"WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS": "0.05"},
                ),
                pytest.raises(WorkspaceMutationLockError) as exc_info,
            ):
                workspace.atomic_write_text(root / "atomic.md", "unsafe")
            assert exc_info.value.code == "workspace_lock_timeout"
            assert not (root / "atomic.md").exists()
        finally:
            release.set()
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        assert process.exitcode == 0

        workspace.atomic_write_text(root / "atomic.md", "serialized")
        assert (root / "atomic.md").read_text(encoding="utf-8") == "serialized"


def test_lock_errors_map_to_bounded_retryable_http_responses() -> None:
    timeout_error = WorkspaceMutationLockError(
        "internal path must not leak",
        code="workspace_lock_timeout",
    )
    timeout_response = asyncio.run(
        workspace_mutation_lock_error_handler(None, timeout_error)
    )
    assert timeout_response.status_code == 423
    assert timeout_response.headers["retry-after"] == "1"
    timeout_payload = json.loads(timeout_response.body)
    assert timeout_payload == {
        "detail": "Session workspace is busy; retry the mutation shortly.",
        "code": "workspace_lock_timeout",
    }
    assert "internal path" not in timeout_response.body.decode("utf-8")

    pending_error = WorkspaceMutationLockError(
        "pending operation details must not leak",
        code="workspace_session_pending",
    )
    pending_response = asyncio.run(
        workspace_mutation_lock_error_handler(None, pending_error)
    )
    assert pending_response.status_code == 423
    assert pending_response.headers["retry-after"] == "1"
    pending_payload = json.loads(pending_response.body)
    assert pending_payload["code"] == "workspace_session_pending"
    assert "pending operation details" not in pending_response.body.decode(
        "utf-8"
    )

    unsafe_error = WorkspaceMutationLockError(
        "unsafe internal detail",
        code="workspace_lock_unsafe",
    )
    unsafe_response = asyncio.run(
        workspace_mutation_lock_error_handler(None, unsafe_error)
    )
    assert unsafe_response.status_code == 503
    assert "unsafe internal detail" not in unsafe_response.body.decode("utf-8")


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX flock")
def test_ui_write_and_delete_participate_in_shared_lock(
    tmp_path: Path,
) -> None:
    user = SimpleNamespace(id="user")
    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(
            workspace_router,
            "_conversation",
            new=AsyncMock(return_value=SimpleNamespace(id="session")),
        ),
    ):
        root = workspace.ensure_workspace("user", "session")
        context = multiprocessing.get_context("fork")
        events = context.Queue()
        release = context.Event()
        process = context.Process(
            target=_acquire_harness_workspace_lock,
            args=(str(root), events, release),
        )
        process.start()
        assert events.get(timeout=2) == "waiting"
        assert events.get(timeout=2) == "acquired"
        try:
            with (
                patch.dict(
                    os.environ,
                    {"WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS": "0.05"},
                ),
                pytest.raises(WorkspaceMutationLockError) as exc_info,
            ):
                asyncio.run(
                    workspace_router.write_workspace_file(
                        "session",
                        WorkspaceFileWrite(content="first"),
                        "report.md",
                        user,
                        object(),
                    )
                )
            assert exc_info.value.code == "workspace_lock_timeout"
            assert not (root / "report.md").exists()
        finally:
            release.set()
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        assert process.exitcode == 0

        result = asyncio.run(
            workspace_router.write_workspace_file(
                "session",
                WorkspaceFileWrite(content="complete"),
                "report.md",
                user,
                object(),
            )
        )
        assert result == {"ok": True, "path": "report.md", "size": 8}
        assert (root / "report.md").read_text(encoding="utf-8") == "complete"

        deleted = asyncio.run(
            workspace_router.delete_workspace_file(
                "session",
                "report.md",
                user,
                object(),
            )
        )
        assert deleted == {"ok": True}
        assert not (root / "report.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX flock")
def test_async_ui_lock_wait_keeps_event_loop_responsive(
    tmp_path: Path,
) -> None:
    user = SimpleNamespace(id="user")
    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(
            workspace_router,
            "_conversation",
            new=AsyncMock(return_value=SimpleNamespace(id="session")),
        ),
    ):
        root = workspace.ensure_workspace("user", "session")
        context = multiprocessing.get_context("fork")
        events = context.Queue()
        release = context.Event()
        process = context.Process(
            target=_acquire_harness_workspace_lock,
            args=(str(root), events, release),
        )
        process.start()
        assert events.get(timeout=2) == "waiting"
        assert events.get(timeout=2) == "acquired"

        async def _scenario() -> int:
            ticks = 0
            running = True

            async def _ticker() -> None:
                nonlocal ticks
                while running:
                    ticks += 1
                    await asyncio.sleep(0.005)

            ticker = asyncio.create_task(_ticker())
            try:
                with (
                    patch.dict(
                        os.environ,
                        {"WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS": "0.12"},
                    ),
                    pytest.raises(WorkspaceMutationLockError) as exc_info,
                ):
                    await workspace_router.write_workspace_file(
                        "session",
                        WorkspaceFileWrite(content="blocked"),
                        "report.md",
                        user,
                        object(),
                    )
                assert exc_info.value.code == "workspace_lock_timeout"
            finally:
                running = False
                await ticker
            return ticks

        try:
            assert asyncio.run(_scenario()) >= 8
        finally:
            release.set()
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        assert process.exitcode == 0


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX flock")
def test_deletion_tombstone_rejects_waiter_and_prevents_workspace_revival(
    tmp_path: Path,
) -> None:
    with patch.object(workspace, "WORKSPACE_ROOT", tmp_path):
        root = workspace.ensure_workspace("user", "session")
        local_lock_path = workspace_mutation_lock_path(root)
        context = multiprocessing.get_context("fork")
        events = context.Queue()
        process = None
        with workspace_mutation_guard(root):
            process = context.Process(
                target=_harness_wait_then_write,
                args=(str(root), events),
            )
            process.start()
            assert events.get(timeout=2) == "waiting"
            workspace.publish_session_deletion_tombstone(
                "user",
                "session",
            )
            with pytest.raises(queue.Empty):
                events.get(timeout=0.15)

        assert events.get(timeout=2) == "rejected:workspace_session_deleted"
        process.join(timeout=2)
        assert not process.is_alive()
        assert process.exitcode == 0
        assert not (root / "revived.md").exists()

        outcome = asyncio.run(
            cleanup_deleted_session_workspace("user", "session")
        )
        assert outcome == "removed"
        assert not root.parent.exists()
        # Local lock objects are intentionally permanent. Unlinking one would
        # let an old waiter and a new opener hold different inodes for the
        # same logical session (the classic lock-file ABA race).
        assert local_lock_path.is_file()
        assert not (
            root.parent / ".chatds-workspace-mutation.lock"
        ).exists()
        with pytest.raises(WorkspaceMutationLockError) as exc_info:
            workspace.ensure_workspace("user", "session")
        assert exc_info.value.code == "workspace_session_deleted"
        assert not root.exists()


def test_pending_lifecycle_fence_is_exact_and_released_atomically(
    tmp_path: Path,
) -> None:
    operation_id = "a" * 64
    with patch.object(workspace, "WORKSPACE_ROOT", tmp_path):
        root = workspace.ensure_workspace("user", "session")
        assert workspace.claim_session_pending_fence(
            "user",
            "session",
            operation_id,
        )
        assert workspace.session_pending_fence_matches(
            "user",
            "session",
            operation_id,
        )
        with pytest.raises(WorkspaceMutationLockError) as blocked:
            workspace.ensure_workspace("user", "session")
        assert blocked.value.code == "workspace_session_pending"
        with pytest.raises(WorkspaceMutationLockError) as conflict:
            workspace.clear_session_pending_fence(
                "user",
                "session",
                "b" * 64,
            )
        assert conflict.value.code == "workspace_session_pending"
        assert workspace.clear_session_pending_fence(
            "user",
            "session",
            operation_id,
        )
        assert workspace.ensure_workspace("user", "session") == root
        assert not workspace.session_pending_fence_path(
            "user",
            "session",
        ).exists()


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _LiveConversationDb:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        return _Rows(self._rows)


class _CountingConversationDb:
    def __init__(self):
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return _Rows([])


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _active_owner_resolver(rows):
    live = {
        (str(user_id), str(session_id))
        for user_id, session_id in rows
    }

    async def resolve(user_id: str, session_id: str) -> bool:
        return (str(user_id), str(session_id)) in live

    return resolve


def test_restart_reconciler_uses_db_authority_and_cleans_tombstoned_orphan(
    tmp_path: Path,
) -> None:
    live_rows = [("user", "live-session")]
    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_active_owner_resolver(live_rows),
        ),
    ):
        live = workspace.ensure_workspace("user", "live-session")
        orphan = workspace.ensure_workspace("user", "orphan-session")
        (live / "keep.md").write_text("live", encoding="utf-8")
        (orphan / "remove.md").write_text("orphan", encoding="utf-8")

        # A crash after fence publication but before DB commit must be repaired
        # as live, while a DB-absent, already-fenced tree is removed.
        workspace.publish_session_deletion_tombstone(
            "user",
            "live-session",
        )
        workspace.publish_session_deletion_tombstone(
            "user",
            "orphan-session",
        )
        # Simulate a fence inherited from a crashed earlier Backend process.
        stale_marker = workspace.session_tombstone_path(
            "user",
            "live-session",
        )
        stale_marker.write_bytes(b"chatds-session-deletion-v1\n")
        stale_marker.chmod(0o600)
        first = asyncio.run(
            reconcile_orphan_session_workspaces(
                _LiveConversationDb(live_rows)
            )
        )
        assert first["stale_tombstones_cleared"] == 1
        assert first["removed"] == 1
        assert first["fenced"] == 0
        assert live.is_dir()
        assert (live / "keep.md").read_text(encoding="utf-8") == "live"
        assert not orphan.parent.exists()
        assert not workspace.session_deletion_tombstone_exists(
            "user",
            "live-session",
        )
        assert workspace.session_deletion_tombstone_exists(
            "user",
            "orphan-session",
        )

        # Re-running after a restart is idempotent and never deletes the live
        # conversation even though the permanent orphan fence remains.
        second = asyncio.run(
            reconcile_orphan_session_workspaces(
                _LiveConversationDb(live_rows)
            )
        )
        assert second["candidates"] == 0
        assert second["live"] == 1
        assert second["removed"] == 0
        assert live.is_dir()


def test_current_delete_marker_is_never_cleared_as_stale(
    tmp_path: Path,
) -> None:
    live_rows = [("user", "active-delete")]
    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_active_owner_resolver(live_rows),
        ),
    ):
        workspace.ensure_workspace("user", "active-delete")
        workspace.publish_session_deletion_tombstone(
            "user",
            "active-delete",
        )
        result = asyncio.run(
            reconcile_orphan_session_workspaces(
                _LiveConversationDb(live_rows)
            )
        )
        assert result["stale_tombstones_cleared"] == 0
        assert workspace.session_deletion_tombstone_is_current_process(
            "user",
            "active-delete",
        )


def test_delete_retry_claims_old_marker_before_periodic_repair(
    tmp_path: Path,
) -> None:
    live_rows = [("user", "retry-delete")]
    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_active_owner_resolver(live_rows),
        ),
    ):
        workspace.ensure_workspace("user", "retry-delete")
        workspace.publish_session_deletion_tombstone(
            "user",
            "retry-delete",
        )
        marker = workspace.session_tombstone_path(
            "user",
            "retry-delete",
        )
        marker.write_bytes(b"chatds-session-deletion-v1\n")
        marker.chmod(0o600)
        assert not workspace.session_deletion_tombstone_is_current_process(
            "user",
            "retry-delete",
        )

        workspace.publish_session_deletion_tombstone(
            "user",
            "retry-delete",
        )
        result = asyncio.run(
            reconcile_orphan_session_workspaces(
                _LiveConversationDb(live_rows)
            )
        )
        assert result["stale_tombstones_cleared"] == 0
        assert workspace.session_deletion_tombstone_is_current_process(
            "user",
            "retry-delete",
        )


def test_live_conversation_query_is_chunked_below_sqlite_limits() -> None:
    db = _CountingConversationDb()
    candidates = [
        ("user", f"session-{index}")
        for index in range(1001)
    ]
    live = asyncio.run(_live_conversation_keys(db, candidates))
    assert live == set()
    assert db.calls == 3


def test_empty_database_retains_unfenced_workspaces_without_deferred_queue(
    tmp_path: Path,
) -> None:
    candidates = [
        ("possibly-sensitive-user", f"unfenced-{index}")
        for index in range(3)
    ]
    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(
            workspace_reconciler,
            "_next_session_candidate_batch",
            return_value=(candidates, True, len(candidates)),
        ),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_active_owner_resolver([]),
        ),
        patch(
            "routers.skill_router.SKILLS_DATA_DIR",
            tmp_path / "skills",
        ),
        patch.object(
            workspace_reconciler,
            "_queue_deferred_candidate",
        ) as deferred_queue,
    ):
        for user_id, session_id in candidates:
            workspace_path = workspace.ensure_workspace(
                user_id,
                session_id,
            )
            (workspace_path / "keep.md").write_text(
                session_id,
                encoding="utf-8",
            )
            skill_path = (
                tmp_path
                / "skills"
                / user_id
                / session_id
                / "fixture-skill"
            )
            skill_path.mkdir(parents=True)
            (skill_path / "SKILL.md").write_text(
                "# Preserve me",
                encoding="utf-8",
            )

        for _round in range(2):
            result = asyncio.run(
                reconcile_orphan_session_workspaces(
                    _LiveConversationDb([])
                )
            )

            assert result["candidates"] == 3
            assert result["unfenced_orphans_retained"] == 3
            assert result["unresolved_pending_retained"] == 0
            assert result["removed"] == 0
            assert result["deferred"] == 0
            assert result["fenced"] == 0
        deferred_queue.assert_not_called()
        for user_id, session_id in candidates:
            assert (
                tmp_path
                / user_id
                / session_id
                / "workspace"
                / "keep.md"
            ).read_text(encoding="utf-8") == session_id
            assert not workspace.session_deletion_tombstone_exists(
                user_id,
                session_id,
            )
            assert (
                tmp_path
                / "skills"
                / user_id
                / session_id
                / "fixture-skill"
                / "SKILL.md"
            ).read_text(encoding="utf-8") == "# Preserve me"


def test_database_query_error_retains_all_discovered_workspaces(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidates = [
        ("possibly-sensitive-user", f"db-error-{index}")
        for index in range(3)
    ]

    class _FailingConversationDb:
        async def execute(self, _statement):
            raise RuntimeError("database contents must not enter logs")

    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(
            workspace_reconciler,
            "_next_session_candidate_batch",
            return_value=(candidates, True, len(candidates)),
        ),
    ):
        for user_id, session_id in candidates:
            workspace_path = workspace.ensure_workspace(
                user_id,
                session_id,
            )
            (workspace_path / "keep.md").write_text(
                session_id,
                encoding="utf-8",
            )

        with (
            caplog.at_level(
                logging.WARNING,
                logger="workspace_reconciler",
            ),
            pytest.raises(RuntimeError),
        ):
            asyncio.run(
                reconcile_orphan_session_workspaces(
                    _FailingConversationDb()
                )
            )

        assert len(caplog.records) == 1
        assert all(record.exc_info is None for record in caplog.records)
        audit_text = "\n".join(
            record.getMessage() for record in caplog.records
        )
        assert "possibly-sensitive-user" not in audit_text
        assert "database contents must not enter logs" not in audit_text
        assert "snapshot_query_failed" in audit_text
        for user_id, session_id in candidates:
            assert session_id not in audit_text
            session_digest = hashlib.sha256(
                (
                    "chatds-workspace-reconcile-session-v1\0"
                    + session_id
                ).encode("utf-8")
            ).hexdigest()
            assert session_digest in audit_text
            assert (
                tmp_path
                / user_id
                / session_id
                / "workspace"
                / "keep.md"
            ).is_file()
            assert not workspace.session_deletion_tombstone_exists(
                user_id,
                session_id,
            )


@pytest.mark.parametrize(
    "journal_contents",
    [None, "{not valid json"],
    ids=["missing-journal", "malformed-journal"],
)
def test_pending_with_bad_journal_is_retained_without_deferred_retry(
    tmp_path: Path,
    journal_contents: str | None,
) -> None:
    user_id = "user"
    session_id = "pending-bad-journal"
    operation_id = "a" * 64
    skill_root = tmp_path / "skills"
    operation_dir = (
        skill_root
        / user_id
        / ".chatds_operations"
        / f"fork-{'b' * 64}"
    )
    operation_dir.mkdir(parents=True)
    if journal_contents is not None:
        (operation_dir / "journal.json").write_text(
            journal_contents,
            encoding="utf-8",
        )
    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path / "workspaces"),
        patch(
            "routers.skill_router.SKILLS_DATA_DIR",
            skill_root,
        ),
        patch.object(
            workspace_reconciler,
            "_next_session_candidate_batch",
            return_value=([(user_id, session_id)], True, 1),
        ),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_active_owner_resolver([]),
        ),
        patch.object(
            workspace_reconciler,
            "_queue_deferred_candidate",
        ) as deferred_queue,
    ):
        workspace_path = workspace.ensure_workspace(user_id, session_id)
        (workspace_path / "keep.md").write_text(
            "pending",
            encoding="utf-8",
        )
        workspace.claim_session_pending_fence(
            user_id,
            session_id,
            operation_id,
        )

        result = asyncio.run(
            reconcile_orphan_session_workspaces(
                _LiveConversationDb([])
            )
        )

        assert result["candidates"] == 1
        assert result["recoverable_pending"] == 0
        assert result["unresolved_pending_retained"] == 1
        assert result["unfenced_orphans_retained"] == 0
        assert result["removed"] == 0
        assert result["deferred"] == 0
        deferred_queue.assert_not_called()
        assert (workspace_path / "keep.md").is_file()
        assert workspace.session_pending_fence_path(
            user_id,
            session_id,
        ).is_file()
        assert not workspace.session_deletion_tombstone_exists(
            user_id,
            session_id,
        )


def test_existing_deletion_tombstone_authorizes_orphan_cleanup(
    tmp_path: Path,
) -> None:
    user_id = "user"
    session_id = "already-tombstoned"
    skill_root = tmp_path / "skills"
    workspace_root = tmp_path / "workspaces"
    with (
        patch.object(workspace, "WORKSPACE_ROOT", workspace_root),
        patch(
            "routers.skill_router.SKILLS_DATA_DIR",
            skill_root,
        ),
        patch.object(
            workspace_reconciler,
            "_next_session_candidate_batch",
            return_value=([(user_id, session_id)], True, 1),
        ),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_active_owner_resolver([]),
        ),
    ):
        workspace_path = workspace.ensure_workspace(user_id, session_id)
        (workspace_path / "remove.md").write_text(
            "delete",
            encoding="utf-8",
        )
        workspace.publish_session_deletion_tombstone(user_id, session_id)

        result = asyncio.run(
            reconcile_orphan_session_workspaces(
                _LiveConversationDb([])
            )
        )

        assert result["candidates"] == 1
        assert result["removed"] == 1
        assert result["fenced"] == 0
        assert result["unfenced_orphans_retained"] == 0
        assert result["tombstoned_orphans"] == 1
        assert result["deletion_fence_unresolved"] == 0
        assert not (workspace_root / user_id / session_id).exists()
        assert workspace.session_deletion_tombstone_exists(
            user_id,
            session_id,
        )


@pytest.mark.parametrize(
    "marker_payload",
    [
        b"",
        b"chatds-session-deletion-v1\n",
        b"chatds-session-deletion-v2\nboot_id=abc\n",
        (
            b"chatds-session-deletion-v2\nboot_id="
            + b"g" * 32
            + b"\n"
        ),
        (
            b"chatds-session-deletion-v2\nboot_id="
            + b"a" * 32
            + b"\ntrailing-data"
        ),
        b"x" * 257,
    ],
    ids=[
        "empty",
        "legacy-version",
        "truncated-boot-id",
        "non-hex-boot-id",
        "trailing-data",
        "overlong",
    ],
)
def test_unrecognized_deletion_marker_never_authorizes_cleanup(
    tmp_path: Path,
    marker_payload: bytes,
) -> None:
    user_id = "user"
    session_id = "invalid-delete-record"
    workspace_root = tmp_path / "workspaces"
    skill_root = tmp_path / "skills"
    with (
        patch.object(workspace, "WORKSPACE_ROOT", workspace_root),
        patch(
            "routers.skill_router.SKILLS_DATA_DIR",
            skill_root,
        ),
        patch.object(
            workspace_reconciler,
            "_next_session_candidate_batch",
            return_value=([(user_id, session_id)], True, 1),
        ),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_active_owner_resolver([]),
        ),
        patch.object(
            workspace_reconciler,
            "_queue_deferred_candidate",
        ) as deferred_queue,
    ):
        workspace_path = workspace.ensure_workspace(user_id, session_id)
        (workspace_path / "keep.md").write_text(
            "preserve",
            encoding="utf-8",
        )
        skill_path = skill_root / user_id / session_id / "fixture-skill"
        skill_path.mkdir(parents=True)
        (skill_path / "SKILL.md").write_text(
            "# Preserve me",
            encoding="utf-8",
        )
        workspace.claim_session_pending_fence(
            user_id,
            session_id,
            "d" * 64,
        )
        marker = workspace.session_tombstone_path(user_id, session_id)
        marker.parent.mkdir(mode=0o700)
        marker.write_bytes(marker_payload)
        marker.chmod(0o600)

        result = asyncio.run(
            reconcile_orphan_session_workspaces(
                _LiveConversationDb([])
            )
        )

        assert result["candidates"] == 1
        assert result["removed"] == 0
        assert result["deferred"] == 1
        assert result["fenced"] == 0
        assert result["tombstoned_orphans"] == 0
        assert result["deletion_fence_unresolved"] == 1
        deferred_queue.assert_called_once_with(user_id, session_id)
        assert (workspace_path / "keep.md").is_file()
        assert (skill_path / "SKILL.md").is_file()
        assert workspace.session_pending_fence_path(
            user_id,
            session_id,
        ).is_file()
        assert marker.is_file()
        with pytest.raises(WorkspaceMutationLockError) as invalid:
            workspace.session_deletion_tombstone_authorizes_cleanup(
                user_id,
                session_id,
            )
        assert invalid.value.code == "workspace_lock_unsafe"


@pytest.mark.parametrize(
    "metadata_mutation",
    [
        "file-mode",
        "directory-mode",
        "hardlink",
        "symlink",
        "owner-mismatch",
    ],
)
def test_unsafe_deletion_marker_metadata_never_authorizes_cleanup(
    tmp_path: Path,
    metadata_mutation: str,
) -> None:
    user_id = "user"
    session_id = "unsafe-delete-record"
    workspace_root = tmp_path / "workspaces"
    skill_root = tmp_path / "skills"
    with (
        patch.object(workspace, "WORKSPACE_ROOT", workspace_root),
        patch(
            "routers.skill_router.SKILLS_DATA_DIR",
            skill_root,
        ),
    ):
        workspace_path = workspace.ensure_workspace(user_id, session_id)
        (workspace_path / "keep.md").write_text(
            "preserve",
            encoding="utf-8",
        )
        skill_path = skill_root / user_id / session_id / "fixture-skill"
        skill_path.mkdir(parents=True)
        (skill_path / "SKILL.md").write_text(
            "# Preserve me",
            encoding="utf-8",
        )
        marker = workspace.publish_session_deletion_tombstone(
            user_id,
            session_id,
        )
        actual_euid = os.geteuid()
        owner_patch = None
        if metadata_mutation == "file-mode":
            marker.chmod(0o640)
        elif metadata_mutation == "directory-mode":
            marker.parent.chmod(0o750)
        elif metadata_mutation == "hardlink":
            os.link(marker, marker.parent / "second-link")
        elif metadata_mutation == "symlink":
            payload = marker.read_bytes()
            target = marker.parent / "alternate-marker"
            target.write_bytes(payload)
            target.chmod(0o600)
            marker.unlink()
            marker.symlink_to(target.name)
        else:
            owner_patch = patch.object(
                workspace.os,
                "geteuid",
                return_value=actual_euid + 1,
            )

        context = owner_patch if owner_patch is not None else nullcontext()
        with context:
            outcome = asyncio.run(
                cleanup_deleted_session_workspace(user_id, session_id)
            )

        assert outcome == "deferred:workspace_lock_unsafe"
        assert (workspace_path / "keep.md").is_file()
        assert (skill_path / "SKILL.md").is_file()


def test_periodic_reconcile_log_omits_exception_contents(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "database-password-must-not-be-logged"

    async def scenario() -> None:
        stop_event = asyncio.Event()

        async def failing_reconcile(**_kwargs):
            stop_event.set()
            raise RuntimeError(sentinel)

        async def immediate_timeout(awaitable, *, timeout):
            del timeout
            awaitable.close()
            raise asyncio.TimeoutError

        with (
            patch.object(
                workspace_reconciler,
                "reconcile_orphan_session_workspaces",
                new=failing_reconcile,
            ),
            patch.object(
                workspace_reconciler.asyncio,
                "wait_for",
                new=immediate_timeout,
            ),
        ):
            await workspace_reconciler.periodic_workspace_reconciler(
                stop_event,
                interval_seconds=1.0,
            )

    with caplog.at_level(logging.WARNING, logger="workspace_reconciler"):
        asyncio.run(scenario())

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert sentinel not in rendered
    assert "error_type=RuntimeError" in rendered
    assert all(record.exc_info is None for record in caplog.records)


def test_startup_reconcile_failure_is_safely_wrapped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "database-password-must-not-be-logged"

    async def scenario() -> None:
        with (
            patch.object(
                backend_main,
                "init_db",
                new=AsyncMock(),
            ),
            patch.object(
                backend_main,
                "reconcile_orphan_session_workspaces",
                new=AsyncMock(side_effect=RuntimeError(sentinel)),
            ),
        ):
            async with backend_main.lifespan(backend_main.app):
                raise AssertionError("lifespan must not start")

    with (
        caplog.at_level(logging.ERROR, logger="main"),
        pytest.raises(RuntimeError) as failure,
    ):
        asyncio.run(scenario())

    assert str(failure.value) == (
        "Startup workspace reconciliation failed safely."
    )
    rendered_exception = "".join(
        traceback.format_exception(failure.value)
    )
    rendered_logs = "\n".join(
        record.getMessage() for record in caplog.records
    )
    assert sentinel not in rendered_exception
    assert sentinel not in rendered_logs
    assert "error_type=RuntimeError" in rendered_logs
    assert all(record.exc_info is None for record in caplog.records)


def test_existing_deletion_tombstone_wins_over_pending_fence(
    tmp_path: Path,
) -> None:
    user_id = "user"
    session_id = "delete-wins"
    workspace_root = tmp_path / "workspaces"
    with (
        patch.object(workspace, "WORKSPACE_ROOT", workspace_root),
        patch(
            "routers.skill_router.SKILLS_DATA_DIR",
            tmp_path / "skills",
        ),
        patch.object(
            workspace_reconciler,
            "_next_session_candidate_batch",
            return_value=([(user_id, session_id)], True, 1),
        ),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_active_owner_resolver([]),
        ),
    ):
        workspace_path = workspace.ensure_workspace(user_id, session_id)
        (workspace_path / "remove.md").write_text(
            "delete",
            encoding="utf-8",
        )
        workspace.claim_session_pending_fence(
            user_id,
            session_id,
            "c" * 64,
        )
        workspace.publish_session_deletion_tombstone(user_id, session_id)

        result = asyncio.run(
            reconcile_orphan_session_workspaces(
                _LiveConversationDb([])
            )
        )

        assert result["candidates"] == 1
        assert result["removed"] == 1
        assert result["recoverable_pending"] == 0
        assert result["unresolved_pending_retained"] == 0
        assert not (workspace_root / user_id / session_id).exists()
        assert not workspace.session_pending_fence_path(
            user_id,
            session_id,
        ).exists()
        assert workspace.session_deletion_tombstone_exists(
            user_id,
            session_id,
        )


def test_tombstoned_orphans_are_cleaned_with_bounded_limit(
    tmp_path: Path,
) -> None:
    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.dict(
            os.environ,
            {"WORKSPACE_ORPHAN_RECONCILE_LIMIT": "1"},
        ),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_active_owner_resolver([]),
        ),
    ):
        for index in range(4):
            workspace.publish_session_deletion_tombstone(
                "user",
                f"historical-{index}",
            )
        orphan = workspace.ensure_workspace("user", "real-orphan")
        (orphan / "remove.md").write_text("orphan", encoding="utf-8")
        workspace.publish_session_deletion_tombstone(
            "user",
            "real-orphan",
        )

        results = []
        for _index in range(12):
            result = asyncio.run(
                reconcile_orphan_session_workspaces(
                    _LiveConversationDb([])
                )
            )
            results.append(result)
            assert result["scanned"] <= 1
            if result["cycle_complete"]:
                break
        assert results[-1]["cycle_complete"] == 1
        # Marker containers are never discovered as session coordinates. Each
        # of the five tombstone publications did create a real session tree,
        # and every tree must be visited without exceeding the batch limit.
        assert sum(item["removed"] for item in results) == 5
        assert not orphan.parent.exists()
        for index in range(4):
            assert workspace.session_deletion_tombstone_exists(
                "user",
                f"historical-{index}",
            )
            assert not (
                tmp_path / "user" / f"historical-{index}"
            ).exists()


def test_pending_marker_container_is_never_discovered_as_session_tree(
    tmp_path: Path,
) -> None:
    live_rows = [
        ("user", "pending-live-a"),
        ("user", "pending-live-b"),
    ]
    operation_ids = {
        session_id: hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        for _user_id, session_id in live_rows
    }
    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_active_owner_resolver(live_rows),
        ),
    ):
        for _user_id, session_id in live_rows:
            workspace.ensure_workspace("user", session_id)
            workspace.claim_session_pending_fence(
                "user",
                session_id,
                operation_ids[session_id],
            )

        result = asyncio.run(
            reconcile_orphan_session_workspaces(
                _LiveConversationDb(live_rows)
            )
        )

        assert result["live"] == 2
        assert result["removed"] == 0
        pending_directory = (
            tmp_path / "user" / workspace.SESSION_PENDING_DIRECTORY
        )
        assert pending_directory.is_dir()
        for _user_id, session_id in live_rows:
            assert workspace.session_pending_fence_path(
                "user",
                session_id,
            ).is_file()
            with pytest.raises(WorkspaceMutationLockError) as blocked:
                workspace.ensure_workspace("user", session_id)
            assert blocked.value.code == "workspace_session_pending"


def test_live_sessions_do_not_starve_tombstoned_cleanup_limit(
    tmp_path: Path,
) -> None:
    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.dict(
            os.environ,
            {"WORKSPACE_ORPHAN_RECONCILE_LIMIT": "1"},
        ),
    ):
        live_rows = []
        for index in range(4):
            session_id = f"aaa-live-{index}"
            live_rows.append(("user", session_id))
            workspace.ensure_workspace("user", session_id)
        orphan = workspace.ensure_workspace("user", "zzz-real-orphan")
        (orphan / "remove.md").write_text("orphan", encoding="utf-8")
        workspace.publish_session_deletion_tombstone(
            "user",
            "zzz-real-orphan",
        )

        results = []
        with patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_active_owner_resolver(live_rows),
        ):
            for _index in range(8):
                result = asyncio.run(
                    reconcile_orphan_session_workspaces(
                        _LiveConversationDb(live_rows)
                    )
                )
                results.append(result)
                assert result["scanned"] <= 1
                assert result["inspected_entries"] <= 4
                if not orphan.parent.exists():
                    break
        assert sum(item["removed"] for item in results) == 1
        assert not orphan.parent.exists()


def test_delete_cancellation_after_marker_publish_keeps_durable_fence() -> None:
    conversation = SimpleNamespace(id="session", title="title")
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[
            _ScalarResult(conversation),
            _ScalarResult("session"),
        ]),
        rollback=AsyncMock(),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    marker_publish = AsyncMock(side_effect=asyncio.CancelledError())

    async def _scenario() -> None:
        with (
            patch.object(
                conv_router,
                "emit_event",
                new=AsyncMock(),
            ),
            patch.object(
                conv_router,
                "_cleanup_harness_session",
                new=AsyncMock(return_value={
                    "success": True,
                    "execution_revocation": {"success": True},
                }),
            ),
            patch(
                "routers.chat_router.cancel_conversation_producers",
                new=AsyncMock(return_value={"success": True}),
            ),
            patch.object(
                conv_router,
                "publish_session_deletion_tombstone_async",
                new=marker_publish,
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                await conv_router.delete_conv(
                    "session",
                    SimpleNamespace(id="user"),
                    db,
                )

    asyncio.run(_scenario())
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("symlink_level", ["user", "session", "workspace"])
def test_session_workspace_creation_rejects_preexisting_symlink_chain(
    tmp_path: Path,
    symlink_level: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with patch.object(workspace, "WORKSPACE_ROOT", tmp_path / "storage"):
        (tmp_path / "storage").mkdir()
        if symlink_level == "user":
            (tmp_path / "storage" / "user").symlink_to(
                outside,
                target_is_directory=True,
            )
        else:
            user = tmp_path / "storage" / "user"
            user.mkdir()
            if symlink_level == "session":
                (user / "session").symlink_to(
                    outside,
                    target_is_directory=True,
                )
            else:
                session = user / "session"
                session.mkdir()
                (session / "workspace").symlink_to(
                    outside,
                    target_is_directory=True,
                )
        with pytest.raises(WorkspaceMutationLockError):
            workspace.ensure_workspace("user", "session")
        assert list(outside.iterdir()) == []


def test_atomic_write_rejects_static_and_live_parent_symlink_swap(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with patch.object(workspace, "WORKSPACE_ROOT", tmp_path / "storage"):
        (tmp_path / "storage").mkdir()
        root = workspace.ensure_workspace("user", "session")
        (root / "static").symlink_to(outside, target_is_directory=True)
        with pytest.raises((ValueError, NotADirectoryError)):
            workspace.atomic_write_text(
                root / "static" / "escaped.md",
                "unsafe",
            )
        assert not (outside / "escaped.md").exists()

        original = workspace.require_open_workspace_directory_current
        swapped = False

        def swap_parent(descriptor, parent, workspace_root):
            nonlocal swapped
            if not swapped and parent.name == "live":
                swapped = True
                backup = root / "live-opened"
                parent.rename(backup)
                parent.symlink_to(outside, target_is_directory=True)
            return original(descriptor, parent, workspace_root)

        with patch.object(
            workspace,
            "require_open_workspace_directory_current",
            side_effect=swap_parent,
        ):
            with pytest.raises(ValueError):
                workspace.atomic_write_text(
                    root / "live" / "escaped.md",
                    "unsafe",
                )
        assert swapped
        assert not (outside / "escaped.md").exists()


def test_backend_debug_append_rejects_static_and_live_parent_swap(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with patch.object(workspace, "WORKSPACE_ROOT", tmp_path / "storage"):
        (tmp_path / "storage").mkdir()
        root = workspace.ensure_workspace("user", "session")
        (root / "debug").symlink_to(outside, target_is_directory=True)
        stream_observability._append_backend_stream_debug_file_sync(
            user_id="user",
            session_id="session",
            run_id="static",
            event={"event_type": "debug"},
        )
        assert not (outside / "backend_streams").exists()
        (root / "debug").unlink()

        original = workspace.require_open_workspace_directory_current
        swapped = False

        def swap_parent(descriptor, parent, workspace_root):
            nonlocal swapped
            if not swapped and parent.name == "backend_streams":
                swapped = True
                backup = root / "backend-streams-opened"
                parent.rename(backup)
                parent.symlink_to(outside, target_is_directory=True)
            return original(descriptor, parent, workspace_root)

        with patch.object(
            stream_observability,
            "require_open_workspace_directory_current",
            side_effect=swap_parent,
        ):
            stream_observability._append_backend_stream_debug_file_sync(
                user_id="user",
                session_id="session",
                run_id="live",
                event={"event_type": "debug"},
            )
        assert swapped
        assert not (outside / "live.jsonl").exists()
