import asyncio
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import database
import scheduler
import workspace
from workspace_lock import WorkspaceMutationLockError
from models import (
    AgentRun,
    AgentRunEvent,
    Artifact,
    Base,
    Conversation,
    CustomModelConfig,
    EventHook,
    Message,
    ScheduledJob,
    ScheduledJobRun,
    SkillPackage,
    TaskItem,
    User,
)
from routers import chat_router, conv_router, skill_router


class _PausedHarnessClient:
    def __init__(self, entered: asyncio.Event) -> None:
        self.entered = entered

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **_kwargs):
        self.entered.set()
        await asyncio.Future()


class _SuccessfulHarnessResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {
            "choices": [{
                "message": {"content": "scheduled result"},
            }],
            "usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
            "model": "model",
        }


class _CountingHarnessClient:
    def __init__(self, calls: list[dict]) -> None:
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **kwargs):
        self.calls.append(dict(kwargs))
        return _SuccessfulHarnessResponse()


class ConversationDeletionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.root / 'delete.db'}"
        )
        self.sessions = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.workspace_patch = patch.object(
            workspace,
            "WORKSPACE_ROOT",
            self.root / "workspaces",
        )
        self.skills_patch = patch.object(
            skill_router,
            "SKILLS_DATA_DIR",
            self.root / "skills",
        )
        self.workspace_patch.start()
        self.skills_patch.start()

    async def asyncTearDown(self):
        await scheduler.shutdown_scheduled_job_executions(
            timeout_seconds=2,
        )
        self.skills_patch.stop()
        self.workspace_patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def _seed_full_session(self) -> None:
        async with self.sessions() as db:
            db.add(User(
                id="user",
                username="delete-user",
                hashed_password="hash",
            ))
            db.add(Conversation(
                id="session",
                user_id="user",
                title="delete me",
                model_id="model",
            ))
            db.add(Conversation(
                id="fork-child",
                user_id="user",
                title="child",
                model_id="model",
                forked_from_conversation_id="session",
            ))
            db.add(Message(
                id="message",
                conversation_id="session",
                role="user",
                content="prompt",
            ))
            db.add(SkillPackage(
                id="skill",
                user_id="user",
                session_id="session",
                name="fixture-skill",
            ))
            db.add(AgentRun(
                id="run",
                user_id="user",
                conversation_id="session",
                root_run_id="run",
                requested_model_id="model",
                status="running",
            ))
            db.add(AgentRunEvent(
                id="event",
                run_id="run",
                conversation_id="session",
                user_id="user",
                seq=1,
                event_type="run.started",
            ))
            db.add(Artifact(
                id="artifact",
                user_id="user",
                conversation_id="session",
                run_id="run",
                kind="file",
            ))
            db.add(TaskItem(
                id="task",
                user_id="user",
                conversation_id="session",
                run_id="run",
                root_run_id="run",
                task_key="run:run",
                kind="run",
                status="running",
            ))
            db.add(ScheduledJob(
                id="job",
                user_id="user",
                conversation_id="session",
                name="job",
                prompt="do work",
                schedule_kind="once",
                schedule_value="now",
                timezone="UTC",
                enabled=True,
            ))
            db.add(ScheduledJobRun(
                id="job-run",
                job_id="job",
                conversation_id="session",
                status="running",
            ))
            db.add(EventHook(
                id="hook",
                user_id="user",
                conversation_id="session",
                name="hook",
                events="[]",
                url="https://example.invalid/hook",
            ))
            await db.commit()
        workspace.ensure_workspace("user", "session")
        skill_dir = self.root / "skills" / "user" / "session" / "fixture-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# fixture", encoding="utf-8")

    async def _delete(self) -> dict:
        async with self.sessions() as db:
            with (
                patch.object(
                    conv_router,
                    "emit_event",
                    new=AsyncMock(return_value=None),
                ),
                patch.object(
                    conv_router,
                    "_cleanup_harness_session",
                    new=AsyncMock(return_value={
                        "success": True,
                        "execution_revocation": {"success": True},
                    }),
                ),
            ):
                return await conv_router.delete_conv(
                    "session",
                    SimpleNamespace(id="user"),
                    db,
                )

    @staticmethod
    def _provider_config() -> dict:
        return {
            "id": "model",
            "base_url": "http://provider.invalid",
            "api_key": "",
            "api_model": "model",
            "provider": "openai",
            "protocol": "openai",
            "is_multimodal": False,
            "context_length": 4096,
        }

    async def _new_scheduled_run(self) -> ScheduledJobRun:
        async with self.sessions() as db:
            rows = list((await db.execute(
                select(ScheduledJobRun).where(
                    ScheduledJobRun.job_id == "job",
                    ScheduledJobRun.id != "job-run",
                )
            )).scalars().all())
        self.assertEqual(1, len(rows))
        return rows[0]

    async def test_delete_explicitly_purges_every_session_projection(self):
        await self._seed_full_session()
        result = await self._delete()
        self.assertTrue(result["ok"])

        async with self.sessions() as db:
            for model, predicate in (
                (Message, Message.conversation_id == "session"),
                (SkillPackage, SkillPackage.session_id == "session"),
                (AgentRun, AgentRun.conversation_id == "session"),
                (
                    AgentRunEvent,
                    AgentRunEvent.conversation_id == "session",
                ),
                (Artifact, Artifact.conversation_id == "session"),
                (TaskItem, TaskItem.conversation_id == "session"),
                (
                    ScheduledJob,
                    ScheduledJob.conversation_id == "session",
                ),
                (
                    ScheduledJobRun,
                    ScheduledJobRun.conversation_id == "session",
                ),
                (EventHook, EventHook.conversation_id == "session"),
                (Conversation, Conversation.id == "session"),
            ):
                count = (await db.execute(
                    select(func.count()).select_from(model).where(predicate)
                )).scalar_one()
                self.assertEqual(0, count, model.__tablename__)
            child = await db.get(Conversation, "fork-child")
            self.assertIsNotNone(child)
            self.assertIsNone(child.forked_from_conversation_id)

    async def test_missing_conversation_rejects_late_immediate_event(self):
        async with self.sessions() as db:
            db.add(User(
                id="user",
                username="late-event-user",
                hashed_password="hash",
            ))
            await db.commit()
        event = {
            "event_type": "run.started",
            "run_id": "late-run",
            "root_run_id": "late-run",
            "seq": 1,
            "payload": {"model_id": "model"},
        }
        with patch.object(chat_router, "async_session", self.sessions):
            persisted = await chat_router._persist_agent_event_once(
                conv_id="missing",
                user_id="user",
                root_run_id="late-run",
                requested_model_id="model",
                resolved_model_id="model",
                event=event,
            )
        self.assertFalse(persisted)
        async with self.sessions() as db:
            self.assertEqual(
                0,
                (await db.execute(
                    select(func.count()).select_from(AgentRunEvent)
                )).scalar_one(),
            )
            self.assertEqual(
                0,
                (await db.execute(
                    select(func.count()).select_from(AgentRun)
                )).scalar_one(),
            )

    async def test_legacy_orphan_repair_preserves_live_session_rows(self):
        await self._seed_full_session()
        async with self.sessions() as db:
            db.add(Conversation(
                id="missing-user-session",
                user_id="missing-user",
                title="orphan owner",
                model_id="model",
            ))
            db.add(Message(
                id="missing-user-message",
                conversation_id="missing-user-session",
                role="user",
                content="orphan owner",
            ))
            db.add(CustomModelConfig(
                id="missing-user-model",
                user_id="missing-user",
                model_id="orphan-model",
                model_name="orphan",
                provider="openai",
                base_url="http://provider.invalid",
                api_key="not-a-secret-fixture",
            ))
            db.add(Message(
                id="orphan-message",
                conversation_id="missing",
                role="user",
                content="orphan",
            ))
            db.add(SkillPackage(
                id="orphan-skill",
                user_id="user",
                session_id="missing",
                name="orphan-skill",
            ))
            db.add(AgentRun(
                id="orphan-run",
                user_id="user",
                conversation_id="missing",
                root_run_id="orphan-run",
                requested_model_id="model",
            ))
            db.add(AgentRunEvent(
                id="orphan-event",
                run_id="orphan-run",
                conversation_id="missing",
                user_id="user",
                seq=1,
                event_type="run.started",
            ))
            db.add(Artifact(
                id="orphan-artifact",
                user_id="user",
                conversation_id="missing",
                run_id="orphan-run",
            ))
            db.add(TaskItem(
                id="orphan-task",
                user_id="user",
                conversation_id="missing",
                run_id="orphan-run",
                task_key="orphan:run",
            ))
            db.add(EventHook(
                id="orphan-hook",
                user_id="user",
                conversation_id="missing",
                name="orphan",
                events="[]",
                url="https://example.invalid/orphan",
            ))
            db.add(ScheduledJob(
                id="orphan-job",
                user_id="user",
                conversation_id="missing",
                name="orphan",
                prompt="orphan",
                schedule_kind="once",
                schedule_value="now",
            ))
            db.add(ScheduledJobRun(
                id="orphan-job-run",
                job_id="orphan-job",
                conversation_id="missing",
            ))
            await db.commit()

        async with self.engine.begin() as connection:
            repaired = await database._repair_legacy_foreign_key_orphans(
                connection
            )
            await database._assert_sqlite_foreign_key_integrity(connection)
        self.assertGreaterEqual(repaired["agent_run_events"], 1)
        self.assertGreaterEqual(repaired["scheduled_job_runs"], 1)

        async with self.sessions() as db:
            self.assertIsNotNone(await db.get(Conversation, "session"))
            self.assertIsNotNone(await db.get(Message, "message"))
            self.assertIsNone(await db.get(Message, "orphan-message"))
            self.assertIsNone(await db.get(AgentRun, "orphan-run"))
            self.assertIsNone(await db.get(ScheduledJob, "orphan-job"))
            self.assertIsNone(
                await db.get(Conversation, "missing-user-session")
            )
            self.assertIsNone(
                await db.get(CustomModelConfig, "missing-user-model")
            )
            self.assertIsNotNone(await db.get(ScheduledJob, "job"))

    async def test_delete_cancels_and_drains_paused_scheduled_execution(self):
        await self._seed_full_session()
        entered = asyncio.Event()
        with (
            patch.object(scheduler, "async_session", self.sessions),
            patch.object(
                scheduler,
                "_resolve_job_model",
                new=AsyncMock(return_value={
                    "id": "model",
                    "base_url": "http://provider.invalid",
                    "api_key": "",
                    "api_model": "model",
                    "provider": "openai",
                    "protocol": "openai",
                    "is_multimodal": False,
                    "context_length": 4096,
                }),
            ),
            patch.object(
                scheduler,
                "_harness_client",
                new=lambda: _PausedHarnessClient(entered),
            ),
            patch.object(
                scheduler,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
        ):
            scheduled = asyncio.create_task(
                scheduler.execute_job("job", force=True)
            )
            await asyncio.wait_for(entered.wait(), timeout=3)
            result = await asyncio.wait_for(self._delete(), timeout=5)
            self.assertTrue(result["ok"])
            outcomes = await asyncio.gather(
                scheduled,
                return_exceptions=True,
            )
            self.assertIsInstance(outcomes[0], asyncio.CancelledError)

        async with self.sessions() as db:
            self.assertIsNone(await db.get(Conversation, "session"))
            self.assertEqual(
                0,
                (await db.execute(
                    select(func.count()).select_from(ScheduledJob)
                    .where(ScheduledJob.conversation_id == "session")
                )).scalar_one(),
            )
            self.assertEqual(
                0,
                (await db.execute(
                    select(func.count()).select_from(ScheduledJobRun)
                    .where(ScheduledJobRun.conversation_id == "session")
                )).scalar_one(),
            )

    async def test_due_ticks_and_force_trigger_share_one_scheduled_flight(
        self,
    ):
        await self._seed_full_session()
        async with self.sessions() as db:
            job = await db.get(ScheduledJob, "job")
            job.next_run_at = scheduler._utcnow() - timedelta(seconds=1)
            await db.commit()

        harness_calls: list[dict] = []
        with (
            patch.object(scheduler, "async_session", self.sessions),
            patch.object(
                scheduler,
                "_resolve_job_model",
                new=AsyncMock(return_value={
                    "id": "model",
                    "base_url": "http://provider.invalid",
                    "api_key": "",
                    "api_model": "model",
                    "provider": "openai",
                    "protocol": "openai",
                    "is_multimodal": False,
                    "context_length": 4096,
                }),
            ),
            patch.object(
                scheduler,
                "_harness_client",
                new=lambda: _CountingHarnessClient(harness_calls),
            ),
            patch.object(
                scheduler,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
        ):
            async with chat_router.conversation_maintenance_lease("session"):
                first_tick = await scheduler.enqueue_due_jobs_once()
                self.assertEqual(
                    {"due": 1, "started": 1, "coalesced": 0},
                    first_tick,
                )
                first_flight = scheduler._JOB_EXECUTION_FLIGHTS["job"]
                first_task = first_flight.task
                await asyncio.sleep(0)

                for _index in range(3):
                    repeated = await scheduler.enqueue_due_jobs_once()
                    self.assertEqual(1, repeated["due"])
                    self.assertEqual(0, repeated["started"])
                    self.assertEqual(1, repeated["coalesced"])
                    self.assertIs(
                        first_task,
                        scheduler._JOB_EXECUTION_FLIGHTS["job"].task,
                    )

                forced_task, forced_started = (
                    scheduler.enqueue_job_execution("job", force=True)
                )
                self.assertFalse(forced_started)
                self.assertIs(first_task, forced_task)
                self.assertTrue(first_flight.force_requested)
                self.assertEqual([], harness_calls)

            self.assertIsNotNone(first_task)
            await asyncio.wait_for(first_task, timeout=5)
            await asyncio.sleep(0)

        self.assertEqual(1, len(harness_calls))
        self.assertNotIn("job", scheduler._JOB_EXECUTION_FLIGHTS)
        async with self.sessions() as db:
            new_runs = list((await db.execute(
                select(ScheduledJobRun).where(
                    ScheduledJobRun.job_id == "job",
                    ScheduledJobRun.id != "job-run",
                )
            )).scalars().all())
            self.assertEqual(1, len(new_runs))
            self.assertEqual("succeeded", new_runs[0].status)
            cron_messages = list((await db.execute(
                select(Message).where(
                    Message.conversation_id == "session",
                    Message.source == "cron",
                )
            )).scalars().all())
            self.assertEqual(2, len(cron_messages))

    async def test_scheduler_shutdown_cancels_long_run_with_terminal_state(
        self,
    ):
        await self._seed_full_session()
        entered = asyncio.Event()
        with (
            patch.object(scheduler, "async_session", self.sessions),
            patch.object(
                scheduler,
                "_resolve_job_model",
                new=AsyncMock(return_value={
                    "id": "model",
                    "base_url": "http://provider.invalid",
                    "api_key": "",
                    "api_model": "model",
                    "provider": "openai",
                    "protocol": "openai",
                    "is_multimodal": False,
                    "context_length": 4096,
                }),
            ),
            patch.object(
                scheduler,
                "_harness_client",
                new=lambda: _PausedHarnessClient(entered),
            ),
            patch.object(
                scheduler,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
        ):
            scheduled, started = scheduler.enqueue_job_execution(
                "job",
                force=True,
            )
            self.assertTrue(started)
            await asyncio.wait_for(entered.wait(), timeout=3)
            shutdown = await scheduler.shutdown_scheduled_job_executions(
                timeout_seconds=5,
            )
            self.assertTrue(shutdown["success"])
            self.assertEqual(1, shutdown["cancelled_count"])
            outcomes = await asyncio.gather(
                scheduled,
                return_exceptions=True,
            )
            self.assertIsInstance(outcomes[0], asyncio.CancelledError)

        async with self.sessions() as db:
            new_runs = list((await db.execute(
                select(ScheduledJobRun).where(
                    ScheduledJobRun.job_id == "job",
                    ScheduledJobRun.id != "job-run",
                )
            )).scalars().all())
            self.assertEqual(1, len(new_runs))
            self.assertEqual("cancelled", new_runs[0].status)
            self.assertIsNotNone(new_runs[0].ended_at)
            job = await db.get(ScheduledJob, "job")
            self.assertEqual("cancelled", job.last_status)
            self.assertFalse(job.enabled)

    async def test_provider_resolution_failure_terminalizes_running_cron(
        self,
    ):
        await self._seed_full_session()
        with (
            patch.object(scheduler, "async_session", self.sessions),
            patch.object(
                scheduler,
                "_resolve_job_model",
                new=AsyncMock(side_effect=RuntimeError(
                    "provider metadata failed"
                )),
            ),
            patch.object(
                scheduler,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
        ):
            scheduled, _started = scheduler.enqueue_job_execution(
                "job",
                force=True,
            )
            outcome = (await asyncio.gather(
                scheduled,
                return_exceptions=True,
            ))[0]
        self.assertIsInstance(outcome, RuntimeError)
        run = await self._new_scheduled_run()
        self.assertEqual("failed", run.status)
        self.assertIsNotNone(run.ended_at)
        self.assertIn("provider metadata failed", run.error)

    async def test_workspace_failure_terminalizes_running_cron(self):
        await self._seed_full_session()
        with (
            patch.object(scheduler, "async_session", self.sessions),
            patch.object(
                scheduler,
                "ensure_workspace_async",
                new=AsyncMock(side_effect=WorkspaceMutationLockError(
                    "injected workspace failure",
                    code="workspace_lock_io",
                )),
            ),
            patch.object(
                scheduler,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
        ):
            scheduled, _started = scheduler.enqueue_job_execution(
                "job",
                force=True,
            )
            outcome = (await asyncio.gather(
                scheduled,
                return_exceptions=True,
            ))[0]
        self.assertIsInstance(outcome, WorkspaceMutationLockError)
        run = await self._new_scheduled_run()
        self.assertEqual("failed", run.status)
        self.assertIsNotNone(run.ended_at)

    async def test_tombstone_revocation_cancels_running_cron(self):
        await self._seed_full_session()
        with (
            patch.object(scheduler, "async_session", self.sessions),
            patch.object(
                scheduler,
                "ensure_workspace_async",
                new=AsyncMock(side_effect=WorkspaceMutationLockError(
                    "injected deletion fence",
                    code="workspace_session_deleted",
                )),
            ),
            patch.object(
                scheduler,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
        ):
            scheduled, _started = scheduler.enqueue_job_execution(
                "job",
                force=True,
            )
            outcome = (await asyncio.gather(
                scheduled,
                return_exceptions=True,
            ))[0]
        self.assertIsInstance(outcome, WorkspaceMutationLockError)
        run = await self._new_scheduled_run()
        self.assertEqual("cancelled", run.status)
        self.assertIsNone(run.error)
        self.assertIsNotNone(run.ended_at)

    async def test_event_failure_terminalizes_running_cron(self):
        await self._seed_full_session()
        with (
            patch.object(scheduler, "async_session", self.sessions),
            patch.object(
                scheduler,
                "emit_event",
                new=AsyncMock(side_effect=RuntimeError(
                    "event projection failed"
                )),
            ),
        ):
            scheduled, _started = scheduler.enqueue_job_execution(
                "job",
                force=True,
            )
            outcome = (await asyncio.gather(
                scheduled,
                return_exceptions=True,
            ))[0]
        self.assertIsInstance(outcome, RuntimeError)
        run = await self._new_scheduled_run()
        self.assertEqual("failed", run.status)
        self.assertIsNotNone(run.ended_at)
        self.assertIn("event projection failed", run.error)

    async def test_final_commit_failure_terminalizes_running_cron(self):
        await self._seed_full_session()
        original_commit = scheduler._commit_scheduled_session_state
        commit_calls = 0

        async def fail_final_commit(db, **kwargs):
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 3:
                raise RuntimeError("injected final commit failure")
            return await original_commit(db, **kwargs)

        harness_calls: list[dict] = []
        with (
            patch.object(scheduler, "async_session", self.sessions),
            patch.object(
                scheduler,
                "_resolve_job_model",
                new=AsyncMock(return_value=self._provider_config()),
            ),
            patch.object(
                scheduler,
                "_harness_client",
                new=lambda: _CountingHarnessClient(harness_calls),
            ),
            patch.object(
                scheduler,
                "_commit_scheduled_session_state",
                new=fail_final_commit,
            ),
            patch.object(
                scheduler,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
        ):
            scheduled, _started = scheduler.enqueue_job_execution(
                "job",
                force=True,
            )
            outcome = (await asyncio.gather(
                scheduled,
                return_exceptions=True,
            ))[0]
        self.assertIsInstance(outcome, RuntimeError)
        self.assertEqual(1, len(harness_calls))
        self.assertEqual(3, commit_calls)
        run = await self._new_scheduled_run()
        self.assertEqual("failed", run.status)
        self.assertIsNotNone(run.ended_at)
        self.assertIn("final commit failure", run.error)

    async def test_post_terminal_event_failure_never_rewrites_success(self):
        await self._seed_full_session()
        emit_calls = 0

        async def fail_completion_event(*_args, **_kwargs):
            nonlocal emit_calls
            emit_calls += 1
            if emit_calls == 3:
                raise RuntimeError("completion event failed")

        harness_calls: list[dict] = []
        with (
            patch.object(scheduler, "async_session", self.sessions),
            patch.object(
                scheduler,
                "_resolve_job_model",
                new=AsyncMock(return_value=self._provider_config()),
            ),
            patch.object(
                scheduler,
                "_harness_client",
                new=lambda: _CountingHarnessClient(harness_calls),
            ),
            patch.object(
                scheduler,
                "emit_event",
                new=fail_completion_event,
            ),
        ):
            scheduled, _started = scheduler.enqueue_job_execution(
                "job",
                force=True,
            )
            outcome = (await asyncio.gather(
                scheduled,
                return_exceptions=True,
            ))[0]
        self.assertIsInstance(outcome, RuntimeError)
        run = await self._new_scheduled_run()
        self.assertEqual("succeeded", run.status)
        self.assertIsNotNone(run.ended_at)
        self.assertIsNone(run.error)

    async def test_chat_shutdown_cancels_registered_non_chat_execution(self):
        entered = asyncio.Event()

        async def registered_execution() -> None:
            async with chat_router.registered_conversation_execution(
                "user",
                "registered-shutdown-session",
            ):
                entered.set()
                await asyncio.Future()

        task = asyncio.create_task(registered_execution())
        await asyncio.wait_for(entered.wait(), timeout=1)
        await chat_router.shutdown_chat_background_tasks(
            producer_cancel_seconds=1,
            projection_grace_seconds=1,
            best_effort_cancel_seconds=1,
        )
        outcomes = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(outcomes[0], asyncio.CancelledError)
        self.assertNotIn(
            "registered-shutdown-session",
            chat_router._detached_chat_producers_by_conversation,
        )

    async def test_startup_repairs_orphaned_scheduled_run(self):
        await self._seed_full_session()
        async with self.engine.begin() as connection:
            repaired = (
                await database._reconcile_orphaned_scheduled_job_runs(
                    connection
                )
            )
        self.assertEqual(1, repaired)
        async with self.sessions() as db:
            run = await db.get(ScheduledJobRun, "job-run")
            self.assertEqual("cancelled", run.status)
            self.assertEqual("backend_process_restart", run.error)
            self.assertIsNotNone(run.ended_at)
            job = await db.get(ScheduledJob, "job")
            self.assertEqual("cancelled", job.last_status)
            self.assertFalse(job.enabled)

    async def test_startup_repair_does_not_rewrite_newer_job_terminal(self):
        await self._seed_full_session()
        async with self.sessions() as db:
            job = await db.get(ScheduledJob, "job")
            job.last_status = "succeeded"
            job.enabled = True
            db.add(ScheduledJobRun(
                id="newer-job-run",
                job_id="job",
                conversation_id="session",
                status="succeeded",
                ended_at=scheduler._utcnow(),
            ))
            await db.commit()

        async with self.engine.begin() as connection:
            repaired = (
                await database._reconcile_orphaned_scheduled_job_runs(
                    connection
                )
            )
        self.assertEqual(1, repaired)
        async with self.sessions() as db:
            stale = await db.get(ScheduledJobRun, "job-run")
            self.assertEqual("cancelled", stale.status)
            self.assertIsNotNone(stale.ended_at)
            newer = await db.get(ScheduledJobRun, "newer-job-run")
            self.assertEqual("succeeded", newer.status)
            job = await db.get(ScheduledJob, "job")
            self.assertEqual("succeeded", job.last_status)
            self.assertTrue(job.enabled)

    async def test_delete_consumes_already_failed_projection(self):
        async def failed_projection() -> bool:
            raise RuntimeError("expected tombstone rejection")

        projection = chat_router._track_conversation_projection(
            "projection-failed-session",
            failed_projection(),
        )
        await asyncio.gather(projection, return_exceptions=True)
        await asyncio.sleep(0)
        state = chat_router._conversation_turn_states[
            "projection-failed-session"
        ]
        self.assertIn(projection, state.projection_tasks)

        result = await chat_router.cancel_conversation_producers(
            "projection-failed-session",
            grace_seconds=1,
        )
        self.assertTrue(result["success"])
        self.assertEqual(1, result["projection_drained_count"])
        self.assertEqual(0, result["projection_residual_count"])
        self.assertNotIn(
            "projection-failed-session",
            chat_router._conversation_turn_states,
        )
        async with chat_router.conversation_maintenance_lease(
            "projection-failed-session"
        ):
            pass

    async def test_cancel_waits_for_projection_spawned_during_producer_exit(
        self,
    ):
        projection_started = asyncio.Event()
        release_projection = asyncio.Event()

        async def projection() -> bool:
            projection_started.set()
            await release_projection.wait()
            return True

        async def producer() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                chat_router._track_conversation_projection(
                    "projection-session",
                    projection(),
                )
                raise

        producer_task = asyncio.create_task(producer())
        chat_router._detached_chat_producers_by_conversation.setdefault(
            "projection-session",
            set(),
        ).add(producer_task)
        await asyncio.sleep(0)
        cancel_task = asyncio.create_task(
            chat_router.cancel_conversation_producers(
                "projection-session",
                grace_seconds=2,
            )
        )
        await asyncio.wait_for(projection_started.wait(), timeout=1)
        await asyncio.sleep(0)
        self.assertFalse(cancel_task.done())
        release_projection.set()
        result = await asyncio.wait_for(cancel_task, timeout=1)
        self.assertTrue(result["success"])
        self.assertEqual(1, result["projection_drained_count"])
        chat_router._detached_chat_producers_by_conversation.pop(
            "projection-session",
            None,
        )


def test_sqlite_connection_hook_enables_foreign_keys():
    connection = sqlite3.connect(":memory:")
    try:
        database._enable_sqlite_foreign_keys(connection, None)
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        assert enabled == 1
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
