import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import session_lifecycle
import workspace
import workspace_reconciler
from config import settings
from models import (
    Base,
    Conversation,
    EventHook,
    Message,
    ScheduledJob,
    User,
)
from routers import (
    conv_router,
    hook_router,
    internal_session_router,
    schedule_router,
    skill_router,
)
from schemas import (
    EventHookCreate,
    EventHookUpdate,
    ScheduledJobCreate,
    ScheduledJobUpdate,
)
from workspace_lock import WorkspaceMutationLockError


class _PausedCommitSession:
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


class SessionManagementLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.root / 'management.db'}"
        )
        self.sessions = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add_all([
                User(
                    id="user-1",
                    username="management-owner",
                    hashed_password="test",
                ),
                User(
                    id="user-2",
                    username="management-other",
                    hashed_password="test",
                ),
                Conversation(
                    id="session-1",
                    user_id="user-1",
                    title="Original",
                    model_id="AgentModel",
                ),
                ScheduledJob(
                    id="job-1",
                    user_id="user-1",
                    conversation_id="session-1",
                    name="Original job",
                    prompt="do bounded work",
                    schedule_kind="interval",
                    schedule_value="3600",
                    timezone="UTC",
                    enabled=True,
                ),
                EventHook(
                    id="hook-1",
                    user_id="user-1",
                    conversation_id="session-1",
                    name="Original hook",
                    events=json.dumps(["run.completed"]),
                    url="https://hooks.example.invalid/fixture",
                    enabled=True,
                ),
            ])
            await db.commit()
        self.enqueue = Mock(return_value=(None, True))
        self.patches = (
            patch.object(workspace, "WORKSPACE_ROOT", self.root / "workspace"),
            patch.object(
                session_lifecycle,
                "async_session",
                self.sessions,
            ),
            patch.object(
                schedule_router,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                internal_session_router,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                schedule_router,
                "enqueue_job_execution",
                new=self.enqueue,
            ),
            patch.object(
                hook_router,
                "_url_allowed",
                new=lambda _url: True,
            ),
        )
        for active_patch in self.patches:
            active_patch.start()
        workspace.ensure_workspace("user-1", "session-1")
        self.owner = SimpleNamespace(id="user-1")
        self.other = SimpleNamespace(id="user-2")

    async def asyncTearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def _schedule_create() -> ScheduledJobCreate:
        return ScheduledJobCreate(
            name="New job",
            prompt="do another bounded task",
            schedule="in 1h",
            conversation_id="session-1",
        )

    @staticmethod
    def _hook_create() -> EventHookCreate:
        return EventHookCreate(
            name="New hook",
            events=["run.completed"],
            url="https://hooks.example.invalid/new",
            conversation_id="session-1",
        )

    async def test_pending_session_blocks_every_management_write(self):
        operation_id = "b" * 64
        workspace.claim_session_pending_fence(
            "user-1",
            "session-1",
            operation_id,
        )

        async def rename(db):
            return await conv_router.rename_conv(
                "session-1",
                SimpleNamespace(title="Pending rename"),
                cur_user=self.owner,
                db=db,
            )

        async def create_schedule(db):
            return await schedule_router.create_job(
                self._schedule_create(),
                user=self.owner,
                db=db,
            )

        async def update_schedule(db):
            return await schedule_router.update_job(
                "job-1",
                ScheduledJobUpdate(name="Pending job"),
                user=self.owner,
                db=db,
            )

        async def delete_schedule(db):
            return await schedule_router.delete_job(
                "job-1",
                user=self.owner,
                db=db,
            )

        async def run_schedule(db):
            return await schedule_router.trigger_job(
                "job-1",
                user=self.owner,
                db=db,
            )

        async def create_hook(db):
            return await hook_router.create_hook(
                self._hook_create(),
                user=self.owner,
                db=db,
            )

        async def update_hook(db):
            return await hook_router.update_hook(
                "hook-1",
                EventHookUpdate(name="Pending hook"),
                user=self.owner,
                db=db,
            )

        async def delete_hook(db):
            return await hook_router.delete_hook(
                "hook-1",
                user=self.owner,
                db=db,
            )

        for operation in (
            rename,
            create_schedule,
            update_schedule,
            delete_schedule,
            run_schedule,
            create_hook,
            update_hook,
            delete_hook,
        ):
            async with self.sessions() as request_db:
                with self.assertRaises(
                    WorkspaceMutationLockError,
                    msg=operation.__name__,
                ) as blocked:
                    await operation(request_db)
            self.assertEqual(
                "workspace_session_pending",
                blocked.exception.code,
            )

        self.enqueue.assert_not_called()
        async with self.sessions() as observer:
            conversation = await observer.get(Conversation, "session-1")
            job = await observer.get(ScheduledJob, "job-1")
            hook = await observer.get(EventHook, "hook-1")
            job_count = (
                await observer.execute(
                    select(func.count(ScheduledJob.id))
                )
            ).scalar_one()
            hook_count = (
                await observer.execute(
                    select(func.count(EventHook.id))
                )
            ).scalar_one()
        self.assertEqual("Original", conversation.title)
        self.assertEqual("Original job", job.name)
        self.assertEqual("Original hook", hook.name)
        self.assertEqual(1, job_count)
        self.assertEqual(1, hook_count)

    async def test_completed_session_admits_all_management_write_types(self):
        async with self.sessions() as request_db:
            renamed = await conv_router.rename_conv(
                "session-1",
                SimpleNamespace(title="Recovered"),
                cur_user=self.owner,
                db=request_db,
            )
            created_job = await schedule_router.create_job(
                self._schedule_create(),
                user=self.owner,
                db=request_db,
            )
            updated_job = await schedule_router.update_job(
                "job-1",
                ScheduledJobUpdate(name="Updated job"),
                user=self.owner,
                db=request_db,
            )
            triggered = await schedule_router.trigger_job(
                "job-1",
                user=self.owner,
                db=request_db,
            )
            created_hook = await hook_router.create_hook(
                self._hook_create(),
                user=self.owner,
                db=request_db,
            )
            updated_hook = await hook_router.update_hook(
                "hook-1",
                EventHookUpdate(name="Updated hook"),
                user=self.owner,
                db=request_db,
            )
        self.assertTrue(renamed["ok"])
        self.assertEqual("session-1", created_job["conversation_id"])
        self.assertEqual("Updated job", updated_job["name"])
        self.assertEqual("queued", triggered["status"])
        self.assertTrue(created_hook["ok"])
        self.assertTrue(updated_hook["ok"])
        self.enqueue.assert_called_once_with("job-1", force=True)

    async def test_wrong_owner_is_rejected_before_session_mutation(self):
        operations = []
        async with self.sessions() as request_db:
            operations.extend([
                conv_router.rename_conv(
                    "session-1",
                    SimpleNamespace(title="Wrong owner"),
                    cur_user=self.other,
                    db=request_db,
                ),
                schedule_router.create_job(
                    self._schedule_create(),
                    user=self.other,
                    db=request_db,
                ),
                schedule_router.update_job(
                    "job-1",
                    ScheduledJobUpdate(name="Wrong owner"),
                    user=self.other,
                    db=request_db,
                ),
                hook_router.create_hook(
                    self._hook_create(),
                    user=self.other,
                    db=request_db,
                ),
                hook_router.update_hook(
                    "hook-1",
                    EventHookUpdate(name="Wrong owner"),
                    user=self.other,
                    db=request_db,
                ),
            ])
            for operation in operations:
                with self.assertRaises(HTTPException) as rejected:
                    await operation
                self.assertEqual(404, rejected.exception.status_code)
        self.enqueue.assert_not_called()

    async def test_deletion_fence_wins_over_waiting_management_write(self):
        from routers.chat_router import conversation_maintenance_lease

        async with self.sessions() as request_db:
            async with conversation_maintenance_lease("session-1"):
                rename = asyncio.create_task(
                    conv_router.rename_conv(
                        "session-1",
                        SimpleNamespace(title="Must not persist"),
                        cur_user=self.owner,
                        db=request_db,
                    )
                )
                await asyncio.sleep(0)
                workspace.publish_session_deletion_tombstone(
                    "user-1",
                    "session-1",
                )
            outcome = (await asyncio.gather(
                rename,
                return_exceptions=True,
            ))[0]
        self.assertIsInstance(outcome, WorkspaceMutationLockError)
        self.assertEqual("workspace_session_deleted", outcome.code)
        async with self.sessions() as observer:
            conversation = await observer.get(Conversation, "session-1")
        self.assertEqual("Original", conversation.title)

    async def test_internal_cron_self_calls_reuse_the_active_turn(self):
        from routers.chat_router import conversation_maintenance_lease

        async with self.sessions() as request_db:
            async with conversation_maintenance_lease("session-1"):
                created = await asyncio.wait_for(
                    schedule_router.internal_create_schedule(
                        self._schedule_create(),
                        x_internal_token=settings.internal_api_token,
                        user_id="user-1",
                        source_session_id="session-1",
                        db=request_db,
                    ),
                    timeout=1,
                )
                updated = await asyncio.wait_for(
                    schedule_router.internal_update_schedule(
                        "job-1",
                        ScheduledJobUpdate(name="Self-updated job"),
                        x_internal_token=settings.internal_api_token,
                        user_id="user-1",
                        source_session_id="session-1",
                        db=request_db,
                    ),
                    timeout=1,
                )
                triggered = await asyncio.wait_for(
                    schedule_router.internal_trigger_schedule(
                        "job-1",
                        x_internal_token=settings.internal_api_token,
                        user_id="user-1",
                        source_session_id="session-1",
                        db=request_db,
                    ),
                    timeout=1,
                )
                removed = await asyncio.wait_for(
                    schedule_router.internal_delete_schedule(
                        created["id"],
                        x_internal_token=settings.internal_api_token,
                        user_id="user-1",
                        source_session_id="session-1",
                        db=request_db,
                    ),
                    timeout=1,
                )
        self.assertEqual("session-1", created["conversation_id"])
        self.assertEqual("Self-updated job", updated["name"])
        self.assertEqual("queued", triggered["status"])
        self.assertTrue(removed["ok"])

    async def test_bidirectional_cross_session_message_and_cron_never_abba(
        self,
    ):
        from routers.chat_router import conversation_maintenance_lease

        async with self.sessions() as db:
            db.add(Conversation(
                id="session-2",
                user_id="user-1",
                title="Second",
                model_id="AgentModel",
            ))
            db.add(ScheduledJob(
                id="job-2",
                user_id="user-1",
                conversation_id="session-2",
                name="Second job",
                prompt="do bounded work",
                schedule_kind="interval",
                schedule_value="3600",
                timezone="UTC",
                enabled=True,
            ))
            await db.commit()
        workspace.ensure_workspace("user-1", "session-2")

        message_ready = 0
        both_messages_ready = asyncio.Event()

        async def send_cross(source: str, target: str, content: str):
            nonlocal message_ready
            async with conversation_maintenance_lease(source):
                message_ready += 1
                if message_ready == 2:
                    both_messages_ready.set()
                await both_messages_ready.wait()
                async with self.sessions() as request_db:
                    return await internal_session_router.send_session_message(
                        target,
                        internal_session_router.InternalSessionMessage(
                            content=content,
                        ),
                        user_id="user-1",
                        source_session_id=source,
                        x_internal_token=settings.internal_api_token,
                        db=request_db,
                    )

        first_message, second_message = await asyncio.wait_for(
            asyncio.gather(
                send_cross("session-1", "session-2", "A to B"),
                send_cross("session-2", "session-1", "B to A"),
            ),
            timeout=2,
        )
        self.assertTrue(first_message["ok"])
        self.assertTrue(second_message["ok"])

        update_ready = 0
        both_updates_ready = asyncio.Event()

        async def update_cross(
            source: str,
            target_job: str,
            name: str,
        ):
            nonlocal update_ready
            async with conversation_maintenance_lease(source):
                update_ready += 1
                if update_ready == 2:
                    both_updates_ready.set()
                await both_updates_ready.wait()
                async with self.sessions() as request_db:
                    return await (
                        schedule_router.internal_update_schedule(
                            target_job,
                            ScheduledJobUpdate(name=name),
                            x_internal_token=settings.internal_api_token,
                            user_id="user-1",
                            source_session_id=source,
                            db=request_db,
                        )
                    )

        first_update, second_update = await asyncio.wait_for(
            asyncio.gather(
                update_cross(
                    "session-1",
                    "job-2",
                    "Updated from session 1",
                ),
                update_cross(
                    "session-2",
                    "job-1",
                    "Updated from session 2",
                ),
            ),
            timeout=2,
        )
        self.assertEqual("Updated from session 1", first_update["name"])
        self.assertEqual("Updated from session 2", second_update["name"])

        async with self.sessions() as observer:
            cross_messages = (
                await observer.execute(
                    select(func.count(Message.id)).where(
                        Message.source == "session",
                        Message.conversation_id.in_([
                            "session-1",
                            "session-2",
                        ]),
                    )
                )
            ).scalar_one()
        self.assertEqual(2, cross_messages)

    async def test_commit_is_linearized_before_delete_marker_and_restart(
        self,
    ):
        commit_entered = asyncio.Event()
        permit_commit = asyncio.Event()
        deletion_waiting = asyncio.Event()
        deletion_acquired = asyncio.Event()

        @asynccontextmanager
        async def paused_session_factory():
            async with self.sessions() as inner:
                yield _PausedCommitSession(
                    inner,
                    commit_entered=commit_entered,
                    permit_commit=permit_commit,
                )

        async def publish_competing_delete():
            deletion_waiting.set()
            async with skill_router._skill_install_lock(
                "user-1",
                "session-1",
            ):
                deletion_acquired.set()
                workspace.publish_session_deletion_tombstone(
                    "user-1",
                    "session-1",
                )

        async with self.sessions() as request_db:
            with patch.object(
                session_lifecycle,
                "async_session",
                new=paused_session_factory,
            ):
                rename = asyncio.create_task(
                    conv_router.rename_conv(
                        "session-1",
                        SimpleNamespace(
                            title="Linearized before deletion",
                        ),
                        cur_user=self.owner,
                        db=request_db,
                    )
                )
                await asyncio.wait_for(commit_entered.wait(), timeout=1)
                deletion = asyncio.create_task(
                    publish_competing_delete()
                )
                await asyncio.wait_for(deletion_waiting.wait(), timeout=1)
                await asyncio.sleep(0.05)
                self.assertFalse(deletion_acquired.is_set())
                self.assertFalse(
                    workspace.session_deletion_tombstone_exists(
                        "user-1",
                        "session-1",
                    )
                )
                permit_commit.set()
                await asyncio.wait_for(rename, timeout=1)
                await asyncio.wait_for(deletion, timeout=1)

        # Simulate a process restart after deletion intent was published but
        # before its DB projection. Reconciliation may clear that stale marker
        # only because the management commit provably linearized first.
        marker = workspace.session_tombstone_path(
            "user-1",
            "session-1",
        )
        marker.write_bytes(b"chatds-session-deletion-v1\n")
        marker.chmod(0o600)
        async with self.sessions() as reconcile_db:
            with patch.object(
                workspace_reconciler,
                "async_session",
                self.sessions,
            ):
                result = await (
                    workspace_reconciler
                    .reconcile_orphan_session_workspaces(reconcile_db)
                )
        self.assertEqual(1, result["stale_tombstones_cleared"])
        self.assertFalse(
            workspace.session_deletion_tombstone_exists(
                "user-1",
                "session-1",
            )
        )
        async with self.sessions() as observer:
            conversation = await observer.get(Conversation, "session-1")
        self.assertEqual(
            "Linearized before deletion",
            conversation.title,
        )


if __name__ == "__main__":
    unittest.main()
