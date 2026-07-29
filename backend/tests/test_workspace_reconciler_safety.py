import asyncio
import multiprocessing
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import workspace
import workspace_reconciler
from models import Base, Conversation, User
from routers import skill_router
from workspace_lock import (
    WorkspaceMutationLockError,
    workspace_mutation_guard,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _SnapshotDb:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        return _Rows(self._rows)


@dataclass(frozen=True)
class _Storage:
    workspace_root: Path
    skill_root: Path
    lock_root: Path


@pytest.fixture
def isolated_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _Storage:
    """Use only per-test storage and a private local flock plane."""

    storage_root = tmp_path / "reconciler-safety"
    workspace_root = storage_root / "workspaces"
    skill_root = storage_root / "skills"
    lock_root = storage_root / "locks"
    storage_root.mkdir(mode=0o700)
    lock_root.mkdir(mode=0o700)

    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(skill_router, "SKILLS_DATA_DIR", skill_root)
    monkeypatch.setenv("WORKSPACE_MUTATION_LOCK_ROOT", str(lock_root))
    monkeypatch.delenv(
        "WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT",
        raising=False,
    )
    monkeypatch.setenv(
        "WORKSPACE_ORPHAN_LOCK_TIMEOUT_SECONDS",
        "0.05",
    )

    assert workspace_root.is_relative_to(tmp_path)
    assert skill_root.is_relative_to(tmp_path)
    assert lock_root.is_relative_to(tmp_path)
    assert workspace_root != Path("/nfs/temp/chat_ds")
    return _Storage(
        workspace_root=workspace_root,
        skill_root=skill_root,
        lock_root=lock_root,
    )


def _hold_workspace_lock(
    workspace_path: str,
    acquired,
    release,
) -> None:
    with workspace_mutation_guard(
        Path(workspace_path),
        timeout_seconds=2.0,
        allow_deleted=True,
        allow_pending=True,
    ):
        acquired.set()
        if not release.wait(timeout=10):
            raise RuntimeError("Workspace-lock test holder was not released.")


def _absent_owner() -> AsyncMock:
    return AsyncMock(return_value=False)


def _create_skill_tree(
    storage: _Storage,
    user_id: str,
    session_id: str,
) -> Path:
    skill_file = (
        storage.skill_root
        / user_id
        / session_id
        / "fixture-skill"
        / "SKILL.md"
    )
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("# Fixture skill\n", encoding="utf-8")
    return skill_file


def test_tombstoned_cleanup_retries_deferred_flock_without_rescanning(
    isolated_storage: _Storage,
) -> None:
    user_id = "deferred-user"
    session_id = "deferred-session"
    workspace_path = workspace.ensure_workspace(user_id, session_id)
    keep_file = workspace_path / "remove-after-lock.md"
    keep_file.write_text("held", encoding="utf-8")
    operation_id = "a" * 64
    workspace.claim_session_pending_fence(
        user_id,
        session_id,
        operation_id,
    )
    workspace.publish_session_deletion_tombstone(user_id, session_id)

    context = multiprocessing.get_context("fork")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_workspace_lock,
        args=(str(workspace_path), acquired, release),
    )
    holder.start()
    try:
        assert acquired.wait(timeout=5)
        with patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_absent_owner(),
        ):
            first = asyncio.run(
                workspace_reconciler.reconcile_orphan_session_workspaces(
                    _SnapshotDb([])
                )
            )

        assert first["removed"] == 0
        assert first["deferred"] == 1
        assert first["inspected_entries"] > 0
        assert keep_file.is_file()
        assert workspace.session_pending_fence_path(
            user_id,
            session_id,
        ).is_file()

        root = workspace._workspace_root_directory()
        root_stat = root.stat()
        root_key = (str(root), root_stat.st_dev, root_stat.st_ino)
        with workspace_reconciler._DISCOVERY_LOCK:
            assert workspace_reconciler._DEFERRED_RETRY[root_key] == [
                (user_id, session_id)
            ]
    finally:
        release.set()
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)

    assert holder.exitcode == 0
    with patch.object(
        workspace_reconciler,
        "_conversation_owner_is_active",
        new=_absent_owner(),
    ):
        second = asyncio.run(
            workspace_reconciler.reconcile_orphan_session_workspaces(
                _SnapshotDb([])
            )
        )

    assert second["scanned"] == 1
    assert second["inspected_entries"] == 0
    assert second["removed"] == 1
    assert second["deferred"] == 0
    assert second["tombstoned_orphans"] == 1
    assert not (
        isolated_storage.workspace_root / user_id / session_id
    ).exists()
    assert not workspace.session_pending_fence_path(
        user_id,
        session_id,
    ).exists()
    assert workspace.session_deletion_tombstone_authorizes_cleanup(
        user_id,
        session_id,
    )
    with patch.object(
        workspace_reconciler,
        "_conversation_owner_is_active",
        new=_absent_owner(),
    ):
        third = asyncio.run(
            workspace_reconciler.reconcile_orphan_session_workspaces(
                _SnapshotDb([])
            )
        )
    assert third["scanned"] == 0
    assert third["removed"] == 0
    assert third["deferred"] == 0


@pytest.mark.parametrize("marker_mutation", ["removed", "malformed"])
def test_destructive_boundary_revalidates_marker_before_skill_cleanup(
    isolated_storage: _Storage,
    marker_mutation: str,
) -> None:
    user_id = "boundary-user"
    session_id = f"boundary-{marker_mutation}"
    workspace_path = workspace.ensure_workspace(user_id, session_id)
    keep_file = workspace_path / "keep.md"
    keep_file.write_text("preserve", encoding="utf-8")
    skill_file = _create_skill_tree(
        isolated_storage,
        user_id,
        session_id,
    )
    marker = workspace.publish_session_deletion_tombstone(
        user_id,
        session_id,
    )
    original_authorizer = (
        workspace.session_deletion_tombstone_authorizes_cleanup
    )
    authorization_calls = 0

    def authorize_then_race(
        candidate_user_id: str,
        candidate_session_id: str,
    ) -> bool:
        nonlocal authorization_calls
        authorization_calls += 1
        authorized = original_authorizer(
            candidate_user_id,
            candidate_session_id,
        )
        if authorization_calls == 1:
            if marker_mutation == "removed":
                marker.unlink()
            else:
                marker.write_bytes(b"invalid-delete-record\n")
        return authorized

    with (
        patch.object(
            workspace_reconciler,
            "_next_session_candidate_batch",
            return_value=([(user_id, session_id)], True, 1),
        ),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_absent_owner(),
        ),
        patch.object(
            workspace,
            "session_deletion_tombstone_authorizes_cleanup",
            side_effect=authorize_then_race,
        ),
        patch.object(
            workspace_reconciler,
            "_remove_orphan_session_skills",
        ) as skill_cleanup,
        patch.object(
            workspace_reconciler,
            "_queue_deferred_candidate",
        ) as deferred_queue,
    ):
        result = asyncio.run(
            workspace_reconciler.reconcile_orphan_session_workspaces(
                _SnapshotDb([])
            )
        )
        assert result["removed"] == 0
        assert result["deferred"] == 1
        deferred_queue.assert_called_once_with(user_id, session_id)

    assert authorization_calls == 2
    skill_cleanup.assert_not_called()
    assert keep_file.is_file()
    assert skill_file.is_file()
    if marker_mutation == "removed":
        assert not marker.exists()
    else:
        assert marker.is_file()
        with pytest.raises(WorkspaceMutationLockError):
            original_authorizer(user_id, session_id)


def test_pending_recovery_runtime_error_propagates_without_mutation(
    isolated_storage: _Storage,
) -> None:
    user_id = "pending-user"
    session_id = "pending-runtime-error"
    workspace_path = workspace.ensure_workspace(user_id, session_id)
    keep_file = workspace_path / "keep.md"
    keep_file.write_text("preserve", encoding="utf-8")
    skill_file = _create_skill_tree(
        isolated_storage,
        user_id,
        session_id,
    )
    workspace.claim_session_pending_fence(
        user_id,
        session_id,
        "b" * 64,
    )

    with (
        patch.object(
            workspace_reconciler,
            "_next_session_candidate_batch",
            return_value=([(user_id, session_id)], True, 1),
        ),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_absent_owner(),
        ),
        patch.object(
            workspace_reconciler,
            "_recoverable_pending_fork_state",
            side_effect=RuntimeError("injected pending inspection failure"),
        ) as pending_inspection,
        patch.object(
            workspace_reconciler,
            "_remove_orphan_session_tree",
        ) as destructive_cleanup,
        patch.object(
            workspace_reconciler,
            "_queue_deferred_candidate",
        ) as deferred_queue,
    ):
        with pytest.raises(
            RuntimeError,
            match="injected pending inspection failure",
        ):
            asyncio.run(
                workspace_reconciler.reconcile_orphan_session_workspaces(
                    _SnapshotDb([])
                )
            )

    pending_inspection.assert_called_once_with(user_id, session_id)
    destructive_cleanup.assert_not_called()
    deferred_queue.assert_not_called()
    assert keep_file.is_file()
    assert skill_file.is_file()
    assert workspace.session_pending_fence_path(
        user_id,
        session_id,
    ).is_file()
    assert not workspace.session_deletion_tombstone_exists(
        user_id,
        session_id,
    )


def test_pending_journal_inspection_bound_retains_without_retry(
    isolated_storage: _Storage,
) -> None:
    user_id = "bounded-user"
    session_id = "bounded-pending"
    workspace_path = workspace.ensure_workspace(user_id, session_id)
    keep_file = workspace_path / "keep.md"
    keep_file.write_text("preserve", encoding="utf-8")
    workspace.claim_session_pending_fence(
        user_id,
        session_id,
        "c" * 64,
    )
    operations_root = (
        isolated_storage.skill_root
        / user_id
        / ".chatds_operations"
    )
    for index in range(2):
        (
            operations_root
            / f"fork-{index:064x}"
        ).mkdir(parents=True)

    with (
        patch.object(
            workspace_reconciler,
            "_next_session_candidate_batch",
            return_value=([(user_id, session_id)], True, 1),
        ),
        patch.object(
            workspace_reconciler,
            "_conversation_owner_is_active",
            new=_absent_owner(),
        ),
        patch.object(
            workspace_reconciler,
            "MAX_PENDING_FORK_JOURNALS_PER_CANDIDATE",
            1,
        ),
        patch.object(
            workspace_reconciler,
            "_queue_deferred_candidate",
        ) as deferred_queue,
    ):
        result = asyncio.run(
            workspace_reconciler.reconcile_orphan_session_workspaces(
                _SnapshotDb([])
            )
        )

    assert result["recoverable_pending"] == 0
    assert result["unresolved_pending_retained"] == 1
    assert result["deferred"] == 0
    deferred_queue.assert_not_called()
    assert keep_file.is_file()
    assert workspace.session_pending_fence_path(
        user_id,
        session_id,
    ).is_file()
    assert not workspace.session_deletion_tombstone_exists(
        user_id,
        session_id,
    )


def test_pending_clear_failure_remains_discoverable_after_restart(
    isolated_storage: _Storage,
) -> None:
    user_id = "tail-window-user"
    session_id = "tail-window-session"
    workspace_path = workspace.ensure_workspace(user_id, session_id)
    (workspace_path / "remove.md").write_text("delete", encoding="utf-8")
    _create_skill_tree(isolated_storage, user_id, session_id)
    workspace.claim_session_pending_fence(
        user_id,
        session_id,
        "e" * 64,
    )
    workspace.publish_session_deletion_tombstone(user_id, session_id)

    with patch.object(
        workspace,
        "clear_session_pending_fence_after_deletion",
        side_effect=WorkspaceMutationLockError(
            "injected pending clear failure",
            code="workspace_lock_io",
        ),
    ):
        first_outcome = asyncio.run(
            workspace_reconciler.cleanup_deleted_session_workspace(
                user_id,
                session_id,
            )
        )

    session_dir = (
        isolated_storage.workspace_root / user_id / session_id
    )
    assert first_outcome == "deferred:workspace_lock_io"
    assert session_dir.is_dir()
    assert not workspace_path.exists()
    assert workspace.session_pending_fence_path(
        user_id,
        session_id,
    ).is_file()

    with workspace_reconciler._DISCOVERY_LOCK:
        if workspace_reconciler._DISCOVERY_CURSOR is not None:
            workspace_reconciler._DISCOVERY_CURSOR.close()
        workspace_reconciler._DISCOVERY_CURSOR = None
        workspace_reconciler._DEFERRED_RETRY.clear()
        workspace_reconciler._DEFERRED_RETRY_TURN.clear()

    with patch.object(
        workspace_reconciler,
        "_conversation_owner_is_active",
        new=_absent_owner(),
    ):
        second = asyncio.run(
            workspace_reconciler.reconcile_orphan_session_workspaces(
                _SnapshotDb([])
            )
        )

    assert second["removed"] == 1
    assert second["deferred"] == 0
    assert not session_dir.exists()
    assert not workspace.session_pending_fence_path(
        user_id,
        session_id,
    ).exists()


def test_snapshot_live_then_delete_commit_is_linearized_by_skill_lock(
    isolated_storage: _Storage,
    tmp_path: Path,
) -> None:
    user_id = "linear-user"
    session_id = "linear-delete"
    workspace_path = workspace.ensure_workspace(user_id, session_id)
    keep_file = workspace_path / "remove.md"
    keep_file.write_text("delete after commit", encoding="utf-8")
    skill_file = _create_skill_tree(
        isolated_storage,
        user_id,
        session_id,
    )

    async def scenario() -> dict[str, int]:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'linearization.db'}"
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        marker_published = asyncio.Event()
        permit_commit = asyncio.Event()
        snapshot_checked = asyncio.Event()
        delete_task = None
        reconcile_task = None
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                db.add(
                    User(
                        id=user_id,
                        username="linear-delete-user",
                        hashed_password="test",
                    )
                )
                db.add(
                    Conversation(
                        id=session_id,
                        user_id=user_id,
                        title="Linearized delete",
                        model_id="AgentModel",
                    )
                )
                await db.commit()

            async def delete_transaction() -> None:
                async with skill_router._skill_install_lock(
                    user_id,
                    session_id,
                ):
                    await workspace.publish_session_deletion_tombstone_async(
                        user_id,
                        session_id,
                    )
                    marker_published.set()
                    await permit_commit.wait()
                    async with sessions() as delete_db:
                        deleted = await delete_db.execute(
                            delete(Conversation).where(
                                Conversation.id == session_id,
                                Conversation.user_id == user_id,
                            )
                        )
                        assert deleted.rowcount == 1
                        await delete_db.commit()

            original_snapshot = workspace_reconciler._live_conversation_keys

            async def observed_snapshot(db, candidates):
                rows = await original_snapshot(db, candidates)
                snapshot_checked.set()
                return rows

            delete_task = asyncio.create_task(
                delete_transaction(),
                name="linearized-delete",
            )
            await asyncio.wait_for(marker_published.wait(), timeout=5)
            async with sessions() as snapshot_db:
                with (
                    patch.object(
                        workspace_reconciler,
                        "_next_session_candidate_batch",
                        return_value=([(user_id, session_id)], True, 1),
                    ),
                    patch.object(
                        workspace_reconciler,
                        "_live_conversation_keys",
                        new=observed_snapshot,
                    ),
                    patch.object(
                        workspace_reconciler,
                        "async_session",
                        sessions,
                    ),
                ):
                    reconcile_task = asyncio.create_task(
                        workspace_reconciler
                        .reconcile_orphan_session_workspaces(snapshot_db),
                        name="linearized-delete-reconcile",
                    )
                    await asyncio.wait_for(
                        snapshot_checked.wait(),
                        timeout=5,
                    )
                    await asyncio.sleep(0)
                    assert not reconcile_task.done()
                    assert keep_file.is_file()
                    assert skill_file.is_file()
                    permit_commit.set()
                    _delete_result, reconcile_result = await (
                        asyncio.wait_for(
                            asyncio.gather(delete_task, reconcile_task),
                            timeout=10,
                        )
                    )

            async with sessions() as observer:
                assert (
                    await observer.get(Conversation, session_id)
                ) is None
            return reconcile_result
        finally:
            permit_commit.set()
            pending_tasks = [
                task
                for task in (delete_task, reconcile_task)
                if task is not None and not task.done()
            ]
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                await asyncio.gather(
                    *pending_tasks,
                    return_exceptions=True,
                )
            await engine.dispose()

    result = asyncio.run(scenario())

    assert result["snapshot_live"] == 1
    assert result["snapshot_owner_drift"] == 1
    assert result["live"] == 0
    assert result["candidates"] == 1
    assert result["removed"] == 1
    assert result["deferred"] == 0
    assert result["tombstoned_orphans"] == 1
    assert not (
        isolated_storage.workspace_root / user_id / session_id
    ).exists()
    assert not (
        isolated_storage.skill_root / user_id / session_id
    ).exists()
    assert workspace.session_deletion_tombstone_authorizes_cleanup(
        user_id,
        session_id,
    )
