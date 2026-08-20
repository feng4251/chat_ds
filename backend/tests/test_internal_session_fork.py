import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import workspace
import workspace_reconciler
import session_lifecycle
from config import settings
from models import Base, Conversation, Message, SkillPackage, User
from routers import (
    chat_router,
    internal_session_router,
    skill_router,
    workspace_router,
)
from workspace_lock import WorkspaceMutationLockError


class _FailCommit:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def commit(self):
        raise RuntimeError("injected fork commit failure")


class _PausedCommit:
    def __init__(
        self,
        inner,
        *,
        commit_entered: asyncio.Event,
        permit_commit: asyncio.Event,
    ):
        self.inner = inner
        self.commit_entered = commit_entered
        self.permit_commit = permit_commit

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def commit(self):
        self.commit_entered.set()
        await self.permit_commit.wait()
        await self.inner.commit()


class InternalSessionForkTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace_root = self.root / "workspace-root"
        self.skills_root = self.root / "skills"
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.root / 'test.db'}"
        )
        self.sessions = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add(User(
                id="user-1",
                username="internal-fork-user",
                hashed_password="test",
            ))
            db.add(Conversation(
                id="source-session",
                user_id="user-1",
                title="Source",
                model_id="AgentModel",
            ))
            db.add(Message(
                id="source-message",
                conversation_id="source-session",
                role="user",
                content="source transcript",
            ))
            await db.commit()

        self.patches = (
            patch.object(workspace, "WORKSPACE_ROOT", self.workspace_root),
            patch.object(skill_router, "SKILLS_DATA_DIR", self.skills_root),
            patch.object(
                workspace_router,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                internal_session_router,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
            # Transaction tests exercise the fork/journal boundary, not model
            # catalog admission. Bind the renamed local wire model to the
            # same deployment-owned native profile used in production.
            patch.object(
                settings,
                "claude_code_provider_profiles",
                ["shaiengine", "local_agentmodel"],
            ),
            patch.object(
                session_lifecycle,
                "async_session",
                self.sessions,
            ),
        )
        for active_patch in self.patches:
            active_patch.start()
        source_workspace = workspace.ensure_workspace(
            "user-1",
            "source-session",
        )
        workspace.atomic_write_text(
            source_workspace / "source.txt",
            "immutable source artifact",
        )

    async def asyncTearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def _fork(self, target_id: str, db):
        return await internal_session_router.fork_session(
            "source-session",
            internal_session_router.InternalFork(
                title="Transactional fork",
                include_messages=True,
                fork_id=target_id,
            ),
            user_id="user-1",
            source_session_id="source-session",
            x_internal_token=settings.internal_api_token,
            db=db,
        )

    async def _install_source_skill(self) -> None:
        async with self.sessions() as db:
            db.add(SkillPackage(
                id="source-skill",
                user_id="user-1",
                session_id="source-session",
                name="fixture-skill",
            ))
            await db.commit()
        source_skill = (
            self.skills_root
            / "user-1"
            / "source-session"
            / "fixture-skill"
        )
        source_skill.mkdir(parents=True)
        (source_skill / "SKILL.md").write_text(
            "# Fixture Skill",
            encoding="utf-8",
        )

    @staticmethod
    def _mcp_success_result() -> dict:
        return {
            "mcp": {
                "registered": [],
                "skipped": [],
                "errors": [],
                "runtime": [],
                "status": "reconciled",
                "reason": "test",
            },
            "mcp_by_skill": {},
            "runtime": [],
        }

    async def test_concurrent_exact_retry_publishes_one_complete_snapshot(self):
        target_id = "a" * 32
        async with self.sessions() as first_db, self.sessions() as second_db:
            first, second = await asyncio.gather(
                self._fork(target_id, first_db),
                self._fork(target_id, second_db),
            )

        self.assertEqual(target_id, first["id"])
        self.assertEqual(target_id, second["id"])
        self.assertEqual(
            {False, True},
            {first["idempotent"], second["idempotent"]},
        )
        target_file = (
            self.workspace_root
            / "user-1"
            / target_id
            / "workspace"
            / "source.txt"
        )
        self.assertEqual("immutable source artifact", target_file.read_text())
        async with self.sessions() as db:
            conversation_count = (await db.execute(
                select(func.count(Conversation.id)).where(
                    Conversation.id == target_id,
                    Conversation.user_id == "user-1",
                )
            )).scalar_one()
            message_count = (await db.execute(
                select(func.count(Message.id)).where(
                    Message.conversation_id == target_id,
                )
            )).scalar_one()
        self.assertEqual(1, conversation_count)
        self.assertEqual(1, message_count)

    async def test_preexisting_target_artifact_is_never_overwritten(self):
        target_id = "b" * 32
        target_workspace = (
            self.workspace_root / "user-1" / target_id / "workspace"
        )
        target_workspace.mkdir(parents=True)
        sentinel = target_workspace / "source.txt"
        sentinel.write_text("unowned target artifact", encoding="utf-8")

        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as rejected:
                await self._fork(target_id, db)
        self.assertEqual(409, rejected.exception.status_code)
        self.assertEqual("unowned target artifact", sentinel.read_text())
        async with self.sessions() as db:
            self.assertIsNone(await db.get(Conversation, target_id))

    async def test_conversation_is_not_visible_until_snapshot_is_complete(self):
        target_id = "e" * 32
        commit_entered = asyncio.Event()
        permit_commit = asyncio.Event()
        async with self.sessions() as inner:
            task = asyncio.create_task(self._fork(
                target_id,
                _PausedCommit(
                    inner,
                    commit_entered=commit_entered,
                    permit_commit=permit_commit,
                ),
            ))
            try:
                await asyncio.wait_for(commit_entered.wait(), timeout=5)
                target_file = (
                    self.workspace_root
                    / "user-1"
                    / target_id
                    / "workspace"
                    / "source.txt"
                )
                self.assertEqual(
                    "immutable source artifact",
                    target_file.read_text(),
                )
                self.assertTrue(
                    (self.skills_root / "user-1" / target_id).is_dir()
                )
                async with self.sessions() as observer:
                    self.assertIsNone(
                        await observer.get(Conversation, target_id)
                    )
            finally:
                permit_commit.set()
            result = await task

        self.assertEqual(target_id, result["id"])
        async with self.sessions() as observer:
            self.assertIsNotNone(await observer.get(Conversation, target_id))

    async def test_reconciler_waits_for_paused_fork_then_preserves_target(self):
        target_id = "f" * 32
        commit_entered = asyncio.Event()
        permit_commit = asyncio.Event()
        snapshot_checked = asyncio.Event()
        original_live_query = workspace_reconciler._live_conversation_keys

        async def observed_snapshot(db, candidates):
            result = await original_live_query(db, candidates)
            snapshot_checked.set()
            return result

        async with self.sessions() as fork_db, self.sessions() as snapshot_db:
            with (
                patch.object(
                    workspace_reconciler,
                    "_next_session_candidate_batch",
                    return_value=([("user-1", target_id)], True, 1),
                ),
                patch.object(
                    workspace_reconciler,
                    "_live_conversation_keys",
                    new=observed_snapshot,
                ),
                patch.object(
                    workspace_reconciler,
                    "async_session",
                    self.sessions,
                ),
            ):
                fork_task = asyncio.create_task(
                    self._fork(
                        target_id,
                        _PausedCommit(
                            fork_db,
                            commit_entered=commit_entered,
                            permit_commit=permit_commit,
                        ),
                    ),
                    name="paused-fork",
                )
                await asyncio.wait_for(commit_entered.wait(), timeout=5)
                target_file = (
                    self.workspace_root
                    / "user-1"
                    / target_id
                    / "workspace"
                    / "source.txt"
                )
                self.assertEqual(
                    "immutable source artifact",
                    target_file.read_text(),
                )

                reconcile_task = asyncio.create_task(
                    workspace_reconciler
                    .reconcile_orphan_session_workspaces(snapshot_db),
                    name="fork-race-reconciler",
                )
                try:
                    await asyncio.wait_for(
                        snapshot_checked.wait(),
                        timeout=5,
                    )
                    # The stale batch snapshot saw no Conversation. The target
                    # lifecycle lock is still held by fork, so cleanup cannot
                    # publish a tombstone or remove either target tree.
                    self.assertFalse(reconcile_task.done())
                    self.assertFalse(
                        workspace.session_deletion_tombstone_exists(
                            "user-1",
                            target_id,
                        )
                    )
                    self.assertTrue(target_file.is_file())
                finally:
                    permit_commit.set()

                fork_result, reconcile_result = await asyncio.gather(
                    fork_task,
                    reconcile_task,
                )

        self.assertEqual(target_id, fork_result["id"])
        self.assertEqual(0, reconcile_result["snapshot_live"])
        self.assertEqual(1, reconcile_result["snapshot_owner_drift"])
        self.assertEqual(1, reconcile_result["live"])
        self.assertEqual(0, reconcile_result["candidates"])
        self.assertEqual(0, reconcile_result["fenced"])
        self.assertEqual(0, reconcile_result["removed"])
        self.assertTrue(target_file.is_file())
        self.assertTrue(
            (self.skills_root / "user-1" / target_id).is_dir()
        )
        self.assertFalse(
            workspace.session_deletion_tombstone_exists(
                "user-1",
                target_id,
            )
        )
        async with self.sessions() as observer:
            target = await observer.get(Conversation, target_id)
        self.assertIsNotNone(target)
        self.assertEqual("source-session", target.forked_from_conversation_id)

    async def test_reconciler_removes_previously_tombstoned_orphan(self):
        orphan_id = "9" * 32
        orphan_workspace = workspace.ensure_workspace(
            "user-1",
            orphan_id,
        )
        (orphan_workspace / "orphan.txt").write_text(
            "remove me",
            encoding="utf-8",
        )
        orphan_skill = (
            self.skills_root
            / "user-1"
            / orphan_id
            / "orphan-skill"
        )
        orphan_skill.mkdir(parents=True)
        (orphan_skill / "SKILL.md").write_text(
            "# Orphan",
            encoding="utf-8",
        )
        workspace.publish_session_deletion_tombstone(
            "user-1",
            orphan_id,
        )

        async with self.sessions() as snapshot_db:
            with (
                patch.object(
                    workspace_reconciler,
                    "_next_session_candidate_batch",
                    return_value=([("user-1", orphan_id)], True, 1),
                ),
                patch.object(
                    workspace_reconciler,
                    "async_session",
                    self.sessions,
                ),
            ):
                result = await (
                    workspace_reconciler
                    .reconcile_orphan_session_workspaces(snapshot_db)
                )

        self.assertEqual(0, result["snapshot_live"])
        self.assertEqual(0, result["snapshot_owner_drift"])
        self.assertEqual(0, result["live"])
        self.assertEqual(1, result["candidates"])
        self.assertEqual(0, result["fenced"])
        self.assertEqual(1, result["removed"])
        self.assertFalse(
            (self.workspace_root / "user-1" / orphan_id).exists()
        )
        self.assertFalse(
            (self.skills_root / "user-1" / orphan_id).exists()
        )
        self.assertTrue(
            workspace.session_deletion_tombstone_exists(
                "user-1",
                orphan_id,
            )
        )
        self.assertTrue(
            (
                self.workspace_root
                / "user-1"
                / "source-session"
                / "workspace"
                / "source.txt"
            ).is_file()
        )

    async def test_target_chat_waits_for_post_commit_mcp_and_journal_tail(
        self,
    ):
        target_id = "8" * 32
        await self._install_source_skill()

        mcp_entered = asyncio.Event()
        release_mcp = asyncio.Event()

        async def paused_mcp_rebuild(**_kwargs):
            mcp_entered.set()
            await release_mcp.wait()
            return self._mcp_success_result()

        async with self.sessions() as fork_db:
            with patch.object(
                skill_router,
                "_rebuild_mcp_for_skills",
                new=paused_mcp_rebuild,
            ):
                fork_task = asyncio.create_task(
                    self._fork(target_id, fork_db),
                    name="post-commit-mcp-fork",
                )
                try:
                    await asyncio.wait_for(mcp_entered.wait(), timeout=5)
                    # The row is visible, but the target maintenance lease
                    # remains held until MCP reconciliation and journal
                    # completion are durable.
                    async with self.sessions() as observer:
                        self.assertIsNotNone(
                            await observer.get(Conversation, target_id)
                        )

                    chat_attempted = asyncio.Event()
                    chat_entered = asyncio.Event()

                    async def target_chat_turn() -> None:
                        chat_attempted.set()
                        async with (
                            chat_router.conversation_maintenance_lease(
                                target_id
                            )
                        ):
                            chat_entered.set()

                    chat_task = asyncio.create_task(
                        target_chat_turn(),
                        name="target-chat-after-fork-commit",
                    )
                    await asyncio.wait_for(
                        chat_attempted.wait(),
                        timeout=1,
                    )
                    self.assertFalse(chat_entered.is_set())
                finally:
                    release_mcp.set()

                fork_result = await asyncio.wait_for(fork_task, timeout=5)
                await asyncio.wait_for(chat_task, timeout=5)

        self.assertEqual(target_id, fork_result["id"])
        self.assertTrue(chat_entered.is_set())
        operation_dir = skill_router._skill_operation_dir(
            user_id="user-1",
            kind="fork",
            identity_parts=["source-session", target_id],
        )
        journal = skill_router._read_operation_journal(
            operation_dir / "journal.json"
        )
        self.assertEqual("completed", journal["state"])

    async def test_post_commit_failure_stays_fenced_until_exact_retry(self):
        target_id = "7" * 32
        await self._install_source_skill()

        async def failed_mcp_rebuild(**_kwargs):
            raise RuntimeError("injected post-commit MCP failure")

        async with self.sessions() as first_db:
            with (
                patch.object(
                    skill_router,
                    "_rebuild_mcp_for_skills",
                    new=failed_mcp_rebuild,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "post-commit MCP failure",
                ),
            ):
                await self._fork(target_id, first_db)

        operation_dir = skill_router._skill_operation_dir(
            user_id="user-1",
            kind="fork",
            identity_parts=["source-session", target_id],
        )
        journal = skill_router._read_operation_journal(
            operation_dir / "journal.json"
        )
        self.assertEqual("committed", journal["state"])
        async with self.sessions() as observer:
            self.assertIsNotNone(
                await observer.get(Conversation, target_id)
            )
            user = await observer.get(User, "user-1")
            with self.assertRaises(WorkspaceMutationLockError) as skill_block:
                await skill_router.list_skills(
                    session_id=target_id,
                    enabled_user_skills=None,
                    user=user,
                    db=observer,
                )
            self.assertEqual(
                "workspace_session_pending",
                skill_block.exception.code,
            )
        with self.assertRaises(WorkspaceMutationLockError) as workspace_block:
            workspace.ensure_workspace("user-1", target_id)
        self.assertEqual(
            "workspace_session_pending",
            workspace_block.exception.code,
        )
        with self.assertRaises(WorkspaceMutationLockError) as chat_block:
            async with chat_router.registered_conversation_execution(
                "user-1",
                target_id,
            ):
                self.fail("pending target must not admit chat execution")
        self.assertEqual(
            "workspace_session_pending",
            chat_block.exception.code,
        )
        async with self.sessions() as request_db:
            with self.assertRaises(WorkspaceMutationLockError) as message_block:
                await internal_session_router.send_session_message(
                    target_id,
                    internal_session_router.InternalSessionMessage(
                        content="must not enter a pending target",
                    ),
                    user_id="user-1",
                    source_session_id="source-session",
                    x_internal_token=settings.internal_api_token,
                    db=request_db,
                )
        self.assertEqual(
            "workspace_session_pending",
            message_block.exception.code,
        )
        async with self.sessions() as observer:
            pending_messages = (
                await observer.execute(
                    select(func.count(Message.id)).where(
                        Message.conversation_id == target_id,
                        Message.source == "session",
                    )
                )
            ).scalar_one()
        self.assertEqual(0, pending_messages)

        async def successful_mcp_rebuild(**_kwargs):
            return self._mcp_success_result()

        async with self.sessions() as retry_db:
            with patch.object(
                skill_router,
                "_rebuild_mcp_for_skills",
                new=successful_mcp_rebuild,
            ):
                result = await self._fork(target_id, retry_db)
        self.assertTrue(result["idempotent"])
        self.assertEqual("completed", result["operation_status"])
        workspace.require_session_workspace_active("user-1", target_id)
        self.assertFalse(
            workspace.session_pending_fence_path(
                "user-1",
                target_id,
            ).exists()
        )
        journal = skill_router._read_operation_journal(
            operation_dir / "journal.json"
        )
        self.assertEqual("completed", journal["state"])
        async with self.sessions() as request_db:
            delivered = await internal_session_router.send_session_message(
                target_id,
                internal_session_router.InternalSessionMessage(
                    content="accepted after exact recovery",
                ),
                user_id="user-1",
                source_session_id="source-session",
                x_internal_token=settings.internal_api_token,
                db=request_db,
            )
        self.assertTrue(delivered["ok"])
        async with self.sessions() as observer:
            delivered_messages = (
                await observer.execute(
                    select(func.count(Message.id)).where(
                        Message.conversation_id == target_id,
                        Message.source == "session",
                    )
                )
            ).scalar_one()
        self.assertEqual(1, delivered_messages)

    async def test_post_commit_cancellation_stays_fenced_until_retry(self):
        target_id = "6" * 32
        await self._install_source_skill()

        async def cancelled_mcp_rebuild(**_kwargs):
            raise asyncio.CancelledError()

        async with self.sessions() as first_db:
            with (
                patch.object(
                    skill_router,
                    "_rebuild_mcp_for_skills",
                    new=cancelled_mcp_rebuild,
                ),
                self.assertRaises(asyncio.CancelledError),
            ):
                await self._fork(target_id, first_db)

        async with self.sessions() as observer:
            self.assertIsNotNone(
                await observer.get(Conversation, target_id)
            )
        with self.assertRaises(WorkspaceMutationLockError) as blocked:
            workspace.require_session_workspace_active(
                "user-1",
                target_id,
            )
        self.assertEqual(
            "workspace_session_pending",
            blocked.exception.code,
        )

        async def successful_mcp_rebuild(**_kwargs):
            return self._mcp_success_result()

        async with self.sessions() as retry_db:
            with patch.object(
                skill_router,
                "_rebuild_mcp_for_skills",
                new=successful_mcp_rebuild,
            ):
                result = await self._fork(target_id, retry_db)
        self.assertTrue(result["idempotent"])
        workspace.require_session_workspace_active("user-1", target_id)
        operation_dir = skill_router._skill_operation_dir(
            user_id="user-1",
            kind="fork",
            identity_parts=["source-session", target_id],
        )
        journal = skill_router._read_operation_journal(
            operation_dir / "journal.json"
        )
        self.assertEqual("completed", journal["state"])

    async def test_precommit_crash_state_is_recoverable_by_exact_retry(self):
        target_id = "5" * 32

        def simulate_process_loss(**_kwargs):
            # A real SIGKILL cannot run request cleanup. Preserve the durable
            # published trees, journal, and pending marker exactly as a
            # restarted Backend would observe them.
            return None

        async with self.sessions() as inner:
            with (
                patch.object(
                    workspace_router,
                    "_cleanup_uncommitted_fork_transaction",
                    new=simulate_process_loss,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "injected fork commit failure",
                ),
            ):
                await self._fork(target_id, _FailCommit(inner))

        async with self.sessions() as observer:
            self.assertIsNone(await observer.get(Conversation, target_id))
        operation_dir = skill_router._skill_operation_dir(
            user_id="user-1",
            kind="fork",
            identity_parts=["source-session", target_id],
        )
        journal = skill_router._read_operation_journal(
            operation_dir / "journal.json"
        )
        self.assertEqual("published", journal["state"])
        self.assertTrue(
            workspace.session_pending_fence_path(
                "user-1",
                target_id,
            ).is_file()
        )

        # Startup orphan repair may classify the exact pending fork for
        # observability, but the pending fence alone must retain a DB-absent
        # target and preserve retry.
        async with self.sessions() as reconcile_db:
            with patch.object(
                workspace_reconciler,
                "async_session",
                self.sessions,
            ):
                reconcile = (
                    await workspace_reconciler
                    .reconcile_orphan_session_workspaces(reconcile_db)
                )
        self.assertEqual(1, reconcile["recoverable_pending"])
        self.assertEqual(0, reconcile["removed"])
        self.assertTrue(
            (self.workspace_root / "user-1" / target_id).is_dir()
        )
        self.assertFalse(
            workspace.session_deletion_tombstone_exists(
                "user-1",
                target_id,
            )
        )
        with self.assertRaises(WorkspaceMutationLockError) as blocked:
            workspace.ensure_workspace("user-1", target_id)
        self.assertEqual(
            "workspace_session_pending",
            blocked.exception.code,
        )

        async with self.sessions() as retry_db:
            result = await self._fork(target_id, retry_db)
        self.assertEqual(target_id, result["id"])
        self.assertEqual("completed", result["operation_status"])
        workspace.require_session_workspace_active("user-1", target_id)
        async with self.sessions() as observer:
            self.assertIsNotNone(
                await observer.get(Conversation, target_id)
            )

    async def test_deletion_fence_wins_over_waiting_internal_mutations(self):
        from routers.chat_router import conversation_maintenance_lease

        async with self.sessions() as request_db:
            async with conversation_maintenance_lease("source-session"):
                delivery = asyncio.create_task(
                    internal_session_router.send_session_message(
                        "source-session",
                        internal_session_router.InternalSessionMessage(
                            content="must lose to deletion",
                        ),
                        user_id="user-1",
                        source_session_id="source-session",
                        x_internal_token=settings.internal_api_token,
                        db=request_db,
                    )
                )
                await asyncio.sleep(0)
                workspace.publish_session_deletion_tombstone(
                    "user-1",
                    "source-session",
                )
            outcome = (await asyncio.gather(
                delivery,
                return_exceptions=True,
            ))[0]
        self.assertIsInstance(outcome, WorkspaceMutationLockError)
        self.assertEqual("workspace_session_deleted", outcome.code)
        async with self.sessions() as observer:
            count = (
                await observer.execute(
                    select(func.count(Message.id)).where(
                        Message.conversation_id == "source-session",
                        Message.source == "session",
                    )
                )
            ).scalar_one()
        self.assertEqual(0, count)

    async def test_self_message_and_goal_mutations_do_not_nest_leases(self):
        from routers.chat_router import conversation_maintenance_lease

        async with self.sessions() as request_db:
            async with conversation_maintenance_lease("source-session"):
                delivered = await asyncio.wait_for(
                    internal_session_router.send_session_message(
                        "source-session",
                        internal_session_router.InternalSessionMessage(
                            content="self-directed note",
                        ),
                        user_id="user-1",
                        source_session_id="source-session",
                        x_internal_token=settings.internal_api_token,
                        db=request_db,
                    ),
                    timeout=1,
                )
                goal = await asyncio.wait_for(
                    internal_session_router.internal_update_goal(
                        "source-session",
                        internal_session_router.InternalGoalUpdate(
                            action="create",
                            objective="finish the session",
                        ),
                        user_id="user-1",
                        x_internal_token=settings.internal_api_token,
                        db=request_db,
                    ),
                    timeout=1,
                )
        self.assertTrue(delivered["ok"])
        self.assertEqual("active", goal["status"])

    async def test_internal_fork_reuses_already_held_source_turn(self):
        from routers.chat_router import conversation_maintenance_lease

        target_id = "3" * 32
        async with conversation_maintenance_lease("source-session"):
            async with self.sessions() as db:
                result = await asyncio.wait_for(
                    self._fork(target_id, db),
                    timeout=2,
                )
        self.assertEqual(target_id, result["id"])
        self.assertEqual("completed", result["operation_status"])

    async def test_internal_fork_rejects_mismatched_source_claim(self):
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as rejected:
                await internal_session_router.fork_session(
                    "source-session",
                    internal_session_router.InternalFork(
                        fork_id="0" * 32,
                    ),
                    user_id="user-1",
                    source_session_id="different-session",
                    x_internal_token=settings.internal_api_token,
                    db=db,
                )
        self.assertEqual(409, rejected.exception.status_code)
        self.assertFalse(
            (self.workspace_root / "user-1" / ("0" * 32)).exists()
        )

    async def test_internal_fork_failure_releases_already_held_source_turn(
        self,
    ):
        from routers.chat_router import conversation_maintenance_lease

        target_id = "2" * 32
        async with conversation_maintenance_lease("source-session"):
            async with self.sessions() as inner:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected fork commit failure",
                ):
                    await asyncio.wait_for(
                        self._fork(target_id, _FailCommit(inner)),
                        timeout=2,
                    )
        async with conversation_maintenance_lease("source-session"):
            pass
        self.assertFalse(
            (self.workspace_root / "user-1" / target_id).exists()
        )

    async def test_internal_fork_source_skill_contention_fails_retryably(
        self,
    ):
        from routers.chat_router import conversation_maintenance_lease

        target_id = "1" * 32
        source_skill_acquired = asyncio.Event()

        async def opposing_rest_mutation():
            async with skill_router._skill_install_lock(
                "user-1",
                "source-session",
            ):
                source_skill_acquired.set()
                async with conversation_maintenance_lease(
                    "source-session"
                ):
                    pass

        async with conversation_maintenance_lease("source-session"):
            opposing = asyncio.create_task(opposing_rest_mutation())
            await asyncio.wait_for(
                source_skill_acquired.wait(),
                timeout=1,
            )
            async with self.sessions() as db:
                with self.assertRaises(
                    WorkspaceMutationLockError,
                ) as busy:
                    await asyncio.wait_for(
                        self._fork(target_id, db),
                        timeout=1,
                    )
            self.assertEqual(
                "workspace_lock_timeout",
                busy.exception.code,
            )
        await asyncio.wait_for(opposing, timeout=1)
        self.assertFalse(
            (self.workspace_root / "user-1" / target_id).exists()
        )

    async def test_completed_journal_with_uncleared_fence_is_reentrant(self):
        target_id = "4" * 32
        original_clear = workspace.clear_session_pending_fence
        clear_calls = 0

        def fail_first_clear(*args, **kwargs):
            nonlocal clear_calls
            clear_calls += 1
            if clear_calls == 1:
                raise RuntimeError("injected lifecycle clear failure")
            return original_clear(*args, **kwargs)

        async with self.sessions() as first_db:
            with (
                patch.object(
                    workspace,
                    "clear_session_pending_fence",
                    new=fail_first_clear,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "lifecycle clear failure",
                ),
            ):
                await self._fork(target_id, first_db)

        operation_dir = skill_router._skill_operation_dir(
            user_id="user-1",
            kind="fork",
            identity_parts=["source-session", target_id],
        )
        journal = skill_router._read_operation_journal(
            operation_dir / "journal.json"
        )
        self.assertEqual("completed", journal["state"])
        with self.assertRaises(WorkspaceMutationLockError) as blocked:
            workspace.require_session_workspace_active(
                "user-1",
                target_id,
            )
        self.assertEqual(
            "workspace_session_pending",
            blocked.exception.code,
        )

        async with self.sessions() as retry_db:
            result = await self._fork(target_id, retry_db)
        self.assertTrue(result["idempotent"])
        workspace.require_session_workspace_active("user-1", target_id)
        self.assertFalse(
            workspace.session_pending_fence_path(
                "user-1",
                target_id,
            ).exists()
        )

    async def test_commit_failure_removes_database_and_published_filesystem(self):
        target_id = "c" * 32
        async with self.sessions() as inner:
            with self.assertRaisesRegex(
                RuntimeError,
                "injected fork commit failure",
            ):
                await self._fork(target_id, _FailCommit(inner))

        async with self.sessions() as db:
            self.assertIsNone(await db.get(Conversation, target_id))
        self.assertFalse(
            (self.workspace_root / "user-1" / target_id).exists()
        )
        self.assertFalse(
            (self.skills_root / "user-1" / target_id).exists()
        )
        self.assertFalse(
            workspace.session_pending_fence_path(
                "user-1",
                target_id,
            ).exists()
        )
        operation_dir = skill_router._skill_operation_dir(
            user_id="user-1",
            kind="fork",
            identity_parts=["source-session", target_id],
        )
        self.assertFalse(operation_dir.exists())

    async def test_source_workspace_root_symlink_is_rejected_without_following(self):
        target_id = "d" * 32
        source_workspace = (
            self.workspace_root
            / "user-1"
            / "source-session"
            / "workspace"
        )
        shutil.rmtree(source_workspace)
        external = self.root / "external"
        external.mkdir()
        secret = external / "outside.txt"
        secret.write_text("must stay outside", encoding="utf-8")
        os.symlink(external, source_workspace, target_is_directory=True)

        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as rejected:
                await self._fork(target_id, db)
        self.assertEqual(409, rejected.exception.status_code)
        self.assertEqual("must stay outside", secret.read_text())
        self.assertFalse(
            (self.workspace_root / "user-1" / target_id).exists()
        )
        async with self.sessions() as db:
            self.assertIsNone(await db.get(Conversation, target_id))


if __name__ == "__main__":
    unittest.main()
