import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import (
    AgentEngineSession,
    AgentRun,
    AgentRunEvent,
    Base,
    Conversation,
    Message,
    TaskItem,
    User,
)
from routers import chat_router


def _event(event_type: str, seq: int, *, run_id: str = "child") -> dict:
    payload: dict = {}
    if event_type == "agent.spawned":
        payload = {
            "goal": "bounded child task",
            "requested_tools": ["skill_view"],
            "effective_tools": ["skill_view"],
            "delegation_batch_id": "batch-1",
            "delegation_slot": 1,
            "delegation_batch_size": 3,
        }
    elif event_type == "run.started":
        payload = {
            "model_id": "model",
            "enabled_tools": ["skill_view"],
        }
    elif event_type == "tool_surface.resolved":
        payload = {
            "mode": "skill_bounded",
            "mcp_policy": "exact",
            "effective_tools": ["skill_view", "mcp_catalog_lookup"],
            "required_tool_groups": [["mcp_catalog_lookup"]],
            "missing_tool_requirements": [],
        }
    elif event_type == "run.completed":
        payload = {
            "finish_reason": "stop",
            "authoritative": True,
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
            },
        }
    elif event_type == "run.failed":
        payload = {
            "error": "bounded fixture failure",
            "finish_reason": "fixture_failure",
            "usage": {
                "input_tokens": 13,
                "output_tokens": 5,
                "total_tokens": 18,
            },
        }
    elif event_type.startswith("tool."):
        payload = {
            "tool_name": "skill_view",
            "tool_call_id": "call-1",
            "outcome": "success",
        }
    return {
        "type": "agent_event",
        "event_type": event_type,
        "run_id": run_id,
        "root_run_id": "root",
        "parent_run_id": None if run_id == "root" else "root",
        "agent_kind": "primary" if run_id == "root" else "delegate",
        "agent_name": "primary" if run_id == "root" else "child",
        "depth": 0 if run_id == "root" else 1,
        "workspace_scope": "shared_session",
        "seq": seq,
        "payload": payload,
    }


class AgentEventPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def test_stream_terminal_finish_reason_uses_authoritative_root_event(self):
        failed = _event("run.failed", 7, run_id="root")
        self.assertEqual(
            "fixture_failure",
            chat_router._authoritative_root_finish_reason(
                [failed],
                run_id="root",
                transport_finish_reason="stop",
            ),
        )
        self.assertEqual(
            "stop",
            chat_router._authoritative_root_finish_reason(
                [],
                run_id="root",
                transport_finish_reason="stop",
            ),
        )

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "events.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add(User(
                id="user",
                username="event-user",
                hashed_password="hash",
            ))
            session.add(Conversation(
                id="conversation",
                user_id="user",
                model_id="model",
            ))
            session.add(AgentRun(
                id="root",
                user_id="user",
                conversation_id="conversation",
                root_run_id="root",
                source="chat",
                requested_model_id="model",
                resolved_model_id="model",
                status="running",
            ))
            await session.commit()

    async def asyncTearDown(self):
        pending = [
            task
            for (conv_id, _), tasks in chat_router._agent_event_persist_tasks.items()
            if conv_id == "conversation"
            for task in tasks
            if not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        chat_router._agent_event_persist_tasks.pop(("conversation", "root"), None)
        chat_router._agent_event_persist_locks.pop("conversation", None)
        best_effort = [
            task
            for task in chat_router._best_effort_tasks
            if not task.done()
        ]
        for task in best_effort:
            task.cancel()
        if best_effort:
            await asyncio.gather(*best_effort, return_exceptions=True)
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_concurrent_burst_is_ordered_complete_and_idempotent(self):
        events = [
            _event("agent.spawned", 0),
            _event("run.started", 1),
            _event("tool.started", 2),
            _event("tool.completed", 3),
            _event("debug.fixture", 4),
            _event("usage.updated", 5),
            _event("run.completed", 6),
        ]
        kwargs = {
            "conv_id": "conversation",
            "user_id": "user",
            "root_run_id": "root",
            "requested_model_id": "model",
            "resolved_model_id": "model",
        }
        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(
                chat_router.settings,
                "agent_event_immediate_persist",
                True,
            ),
            patch.object(chat_router.settings, "agent_debug_trace", True),
        ):
            results = await asyncio.gather(*(
                chat_router._persist_agent_event_immediate(
                    **kwargs,
                    event=event,
                )
                for event in events
            ))
            self.assertTrue(all(results))
            # Replaying in reverse order exercises the natural event-key
            # idempotence without regressing the already-terminal projections.
            replay = await asyncio.gather(*(
                chat_router._persist_agent_event_immediate(
                    **kwargs,
                    event=event,
                )
                for event in reversed(events)
            ))
            self.assertTrue(all(replay))

        async with self.sessions() as session:
            persisted = (await session.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == "child")
                .order_by(AgentRunEvent.seq)
            )).scalars().all()
            self.assertEqual(
                [(event.event_type, event.seq) for event in persisted],
                [(event["event_type"], event["seq"]) for event in events],
            )
            child = await session.get(AgentRun, "child")
            self.assertEqual(child.status, "succeeded")
            self.assertEqual(child.total_tokens, 18)
            self.assertEqual(child.delegation_tool_call_id, "batch-1")
            task = (await session.execute(
                select(TaskItem).where(TaskItem.run_id == "child")
            )).scalar_one()
            self.assertEqual(task.status, "succeeded")
            self.assertIsNotNone(task.ended_at)
            duplicate_count = (await session.execute(
                select(func.count(AgentRunEvent.id)).where(
                    AgentRunEvent.run_id == "child"
                )
            )).scalar_one()
            self.assertEqual(duplicate_count, len(events))

    async def test_child_run_inherits_the_selected_native_engine(self):
        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            root.engine_id = "deepseek_harness"
            await session.commit()
        with patch.object(chat_router, "async_session", self.sessions):
            await chat_router._persist_agent_event_immediate(
                conv_id="conversation",
                user_id="user",
                root_run_id="root",
                requested_model_id="renamed-model",
                resolved_model_id="renamed-model",
                event=_event("run.started", 1, run_id="renamed-worker"),
            )
        async with self.sessions() as session:
            child = await session.get(AgentRun, "renamed-worker")
        self.assertEqual(child.engine_id, "deepseek_harness")

    async def test_internal_reducer_attempts_rehydrate_as_closed_nested_runs(self):
        def reducer_event(
            run_id: str,
            event_type: str,
            seq: int,
            *,
            attempt: int,
        ) -> dict:
            payload = {
                "fan_in_reducer_attempt": attempt,
                "fan_in_reducer_max_attempts": 2,
            }
            if event_type == "run.started":
                payload.update({"model_id": "model", "enabled_tools": []})
            elif event_type == "run.failed":
                payload.update({
                    "error": "bounded output rejected",
                    "finish_reason": "length",
                    "terminal_reason": "model_hit_max_output_tokens",
                    "provisional_terminal": False,
                    "authoritative": True,
                })
            elif event_type == "run.completed":
                payload.update({
                    "finish_reason": "stop",
                    "provisional_terminal": False,
                    "authoritative": True,
                })
            return {
                "type": "agent_event",
                "event_type": event_type,
                "run_id": run_id,
                "root_run_id": "root",
                "parent_run_id": "delegated-worker",
                "agent_kind": "delegate_reducer",
                "agent_name": f"Evidence fan-in attempt {attempt}",
                "depth": 2,
                "workspace_scope": "shared_session",
                "seq": seq,
                "payload": payload,
            }

        events = [
            reducer_event("reducer-attempt-1", "run.started", 0, attempt=1),
            reducer_event("reducer-attempt-1", "run.failed", 1, attempt=1),
            reducer_event("reducer-attempt-2", "run.started", 0, attempt=2),
            reducer_event("reducer-attempt-2", "run.completed", 1, attempt=2),
        ]
        async with self.sessions() as session:
            await chat_router._persist_agent_events(
                session,
                conv_id="conversation",
                user_id="user",
                root_run_id="root",
                requested_model_id="model",
                resolved_model_id="model",
                events=events,
            )
            await session.commit()

        async with self.sessions() as session:
            failed = await session.get(AgentRun, "reducer-attempt-1")
            completed = await session.get(AgentRun, "reducer-attempt-2")
            root = await session.get(AgentRun, "root")
            self.assertEqual("failed", failed.status)
            self.assertEqual("succeeded", completed.status)
            self.assertIsNotNone(failed.ended_at)
            self.assertIsNotNone(completed.ended_at)
            self.assertEqual("delegate", failed.source)
            self.assertEqual("delegate", completed.source)
            self.assertEqual("delegate_reducer", failed.agent_kind)
            self.assertEqual("delegated-worker", failed.parent_run_id)
            self.assertEqual("running", root.status)
            tasks = (await session.execute(
                select(TaskItem)
                .where(TaskItem.run_id.in_([
                    "reducer-attempt-1", "reducer-attempt-2",
                ]))
                .order_by(TaskItem.run_id)
            )).scalars().all()
            self.assertEqual(2, len(tasks))
            self.assertEqual(["delegate", "delegate"], [task.kind for task in tasks])
            self.assertEqual(
                ["failed", "succeeded"],
                [task.status for task in tasks],
            )
            running_reducers = (await session.execute(
                select(func.count(AgentRun.id)).where(
                    AgentRun.agent_kind == "delegate_reducer",
                    AgentRun.status == "running",
                )
            )).scalar_one()
            self.assertEqual(0, running_reducers)

    async def test_fan_in_progress_is_durable_before_root_terminal(self):
        fan_in_event = _event(
            "fan_in.reducer_attempt_started",
            17,
            run_id="root",
        )
        fan_in_event["payload"] = {
            "plan_id": "plan-generic",
            "step_id": "merge-generic",
            "attempt": 2,
            "replacement_input_mode": "previous_complete_output_compaction",
        }
        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(
                chat_router.settings,
                "agent_event_immediate_persist",
                True,
            ),
            patch.object(chat_router.settings, "agent_debug_trace", False),
        ):
            persisted = await chat_router._persist_agent_event_immediate(
                conv_id="conversation",
                user_id="user",
                root_run_id="root",
                requested_model_id="model",
                resolved_model_id="model",
                event=fan_in_event,
            )
        self.assertTrue(persisted)

        async with self.sessions() as session:
            row = (await session.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == "root",
                    AgentRunEvent.event_type
                    == "fan_in.reducer_attempt_started",
                    AgentRunEvent.seq == 17,
                )
            )).scalar_one()
            self.assertEqual(
                "previous_complete_output_compaction",
                json.loads(row.payload)["replacement_input_mode"],
            )
            root = await session.get(AgentRun, "root")
            self.assertEqual("running", root.status)

    async def test_final_tool_surface_and_terminal_projection_are_monotonic(self):
        started = _event("run.started", 1, run_id="root")
        surface = _event("tool_surface.resolved", 2, run_id="root")
        provisional = _event("run.failed", 3, run_id="root")
        provisional["payload"]["authoritative"] = False
        completed = _event("run.completed", 4, run_id="root")
        events = [completed, started, provisional, surface]
        kwargs = {
            "conv_id": "conversation",
            "user_id": "user",
            "root_run_id": "root",
            "requested_model_id": "model",
            "resolved_model_id": "model",
        }

        async with self.sessions() as session:
            await chat_router._persist_agent_events(
                session,
                **kwargs,
                events=events,
            )
            await session.commit()

        # Replaying an old start in a later transaction must not reopen the
        # already committed root lifecycle.
        async with self.sessions() as session:
            await chat_router._persist_agent_events(
                session,
                **kwargs,
                events=[started],
            )
            await session.commit()

        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            self.assertEqual("succeeded", root.status)
            self.assertEqual(
                ["skill_view", "mcp_catalog_lookup"],
                json.loads(root.effective_tools),
            )
            policy = json.loads(root.policy)
            self.assertEqual("skill_bounded", policy["mode"])
            self.assertEqual("exact", policy["mcp_policy"])
            task = (await session.execute(
                select(TaskItem).where(TaskItem.run_id == "root")
            )).scalar_one()
            self.assertEqual("succeeded", task.status)

    async def test_top_level_terminal_authority_matches_harness_precedence(self):
        provisional = _event("run.failed", 2, run_id="root")
        provisional["authoritative"] = False
        provisional["payload"]["authoritative"] = True
        completed = _event("run.completed", 3, run_id="root")

        top_level_authoritative = _event(
            "run.completed",
            2,
            run_id="child-authority",
        )
        top_level_authoritative["authoritative"] = True
        top_level_authoritative["payload"]["authoritative"] = False
        ignored_later_failure = _event(
            "run.failed",
            3,
            run_id="child-authority",
        )

        async with self.sessions() as session:
            await chat_router._persist_agent_events(
                session,
                conv_id="conversation",
                user_id="user",
                root_run_id="root",
                requested_model_id="model",
                resolved_model_id="model",
                events=[
                    provisional,
                    completed,
                    top_level_authoritative,
                    ignored_later_failure,
                ],
            )
            await session.commit()

        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            child = await session.get(AgentRun, "child-authority")
            self.assertEqual("succeeded", root.status)
            self.assertEqual("succeeded", child.status)
            provisional_row = (await session.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == "root",
                    AgentRunEvent.event_type == "run.failed",
                    AgentRunEvent.seq == 2,
                )
            )).scalar_one()
            self.assertFalse(
                json.loads(provisional_row.payload)["authoritative"]
            )
            authoritative_row = (await session.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == "child-authority",
                    AgentRunEvent.event_type == "run.completed",
                    AgentRunEvent.seq == 2,
                )
            )).scalar_one()
            self.assertTrue(
                json.loads(authoritative_row.payload)["authoritative"]
            )

    async def test_conflicting_terminal_at_same_sequence_cannot_replace_first(self):
        completed = _event("run.completed", 4, run_id="root")
        failed = _event("run.failed", 4, run_id="root")
        failed["payload"]["error"] = "late conflicting terminal"
        kwargs = {
            "conv_id": "conversation",
            "user_id": "user",
            "root_run_id": "root",
            "requested_model_id": "model",
            "resolved_model_id": "model",
        }

        async with self.sessions() as session:
            await chat_router._persist_agent_events(
                session,
                **kwargs,
                events=[completed, failed],
            )
            await session.commit()

        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            self.assertEqual("succeeded", root.status)
            task = (await session.execute(
                select(TaskItem).where(TaskItem.run_id == "root")
            )).scalar_one()
            self.assertEqual("succeeded", task.status)

    async def test_higher_sequence_terminal_cannot_replace_first_terminal(self):
        completed = _event("run.completed", 4, run_id="root")
        failed = _event("run.failed", 5, run_id="root")
        failed["payload"]["error"] = "later contradictory terminal"
        kwargs = {
            "conv_id": "conversation",
            "user_id": "user",
            "root_run_id": "root",
            "requested_model_id": "model",
            "resolved_model_id": "model",
        }

        async with self.sessions() as session:
            await chat_router._persist_agent_events(
                session,
                **kwargs,
                events=[completed],
            )
            await session.commit()
        async with self.sessions() as session:
            await chat_router._persist_agent_events(
                session,
                **kwargs,
                events=[failed],
            )
            await session.commit()

        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            self.assertEqual("succeeded", root.status)
            task = (await session.execute(
                select(TaskItem).where(TaskItem.run_id == "root")
            )).scalar_one()
            self.assertEqual("succeeded", task.status)

    async def test_conflicting_same_key_replay_cannot_rewrite_tool_surface(self):
        surface = _event("tool_surface.resolved", 2, run_id="root")
        conflicting = _event("tool_surface.resolved", 2, run_id="root")
        conflicting["payload"]["effective_tools"] = ["execute_code"]
        conflicting["payload"]["mode"] = "conflicting"
        kwargs = {
            "conv_id": "conversation",
            "user_id": "user",
            "root_run_id": "root",
            "requested_model_id": "model",
            "resolved_model_id": "model",
        }

        async with self.sessions() as session:
            await chat_router._persist_agent_events(
                session,
                **kwargs,
                events=[surface],
            )
            await session.commit()
        async with self.sessions() as session:
            await chat_router._persist_agent_events(
                session,
                **kwargs,
                events=[conflicting],
            )
            await session.commit()

        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            self.assertEqual(
                ["skill_view", "mcp_catalog_lookup"],
                json.loads(root.effective_tools),
            )
            self.assertEqual("skill_bounded", json.loads(root.policy)["mode"])
            count = (await session.execute(
                select(func.count(AgentRunEvent.id)).where(
                    AgentRunEvent.run_id == "root",
                    AgentRunEvent.event_type == "tool_surface.resolved",
                    AgentRunEvent.seq == 2,
                )
            )).scalar_one()
            self.assertEqual(1, count)

    async def test_passed_verifier_cannot_be_reopened_by_late_request(self):
        completed = _event("verifier.completed", 2, run_id="root")
        completed["payload"] = {
            "verifier_kind": "run_contract_terminal_preflight",
            "verdict": "pass",
            "reason": "machine receipts are complete",
            "needs_more_work": False,
            "harness_generated": True,
        }
        requested = _event("verifier.requested", 3, run_id="root")
        requested["payload"] = {
            "verifier_kind": "run_contract_terminal_preflight",
            "needs_more_work": False,
            "harness_generated": True,
        }
        kwargs = {
            "conv_id": "conversation",
            "user_id": "user",
            "root_run_id": "root",
            "requested_model_id": "model",
            "resolved_model_id": "model",
        }

        async with self.sessions() as session:
            await chat_router._persist_agent_events(
                session,
                **kwargs,
                events=[completed, requested],
            )
            await session.commit()

        async with self.sessions() as session:
            task = (await session.execute(
                select(TaskItem).where(
                    TaskItem.task_key
                    == "verifier:root:run_contract_terminal_preflight"
                )
            )).scalar_one()
            self.assertEqual("succeeded", task.status)
            self.assertEqual("machine receipts are complete", task.summary)

    async def test_sqlite_lock_retry_is_bounded_and_recovers(self):
        attempts = 0

        async def transient_operation():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise OperationalError(
                    "SELECT 1",
                    {},
                    sqlite3.OperationalError("database is locked"),
                )
            return "persisted"

        with patch.object(chat_router.asyncio, "sleep", new=AsyncMock()) as sleep:
            result = await chat_router._run_sqlite_persist_with_retry(
                transient_operation,
                description="test projection",
            )

        self.assertEqual(result, "persisted")
        self.assertEqual(attempts, 3)
        self.assertEqual(sleep.await_count, 2)

        failed_attempts = 0

        async def permanently_locked():
            nonlocal failed_attempts
            failed_attempts += 1
            raise OperationalError(
                "SELECT 1",
                {},
                sqlite3.OperationalError("database is locked"),
            )

        with patch.object(chat_router.asyncio, "sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(OperationalError):
                await chat_router._run_sqlite_persist_with_retry(
                    permanently_locked,
                    description="bounded test projection",
                )
        self.assertEqual(
            failed_attempts,
            chat_router._SQLITE_PERSIST_MAX_ATTEMPTS,
        )
        self.assertEqual(
            sleep.await_count,
            chat_router._SQLITE_PERSIST_MAX_ATTEMPTS - 1,
        )

    async def test_failed_event_immediate_projection_persists_usage(self):
        failed_event = _event("run.failed", 1)
        kwargs = {
            "conv_id": "conversation",
            "user_id": "user",
            "root_run_id": "root",
            "requested_model_id": "model",
            "resolved_model_id": "model",
            "event": failed_event,
        }
        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(
                chat_router.settings,
                "agent_event_immediate_persist",
                True,
            ),
            patch.object(chat_router.settings, "agent_debug_trace", True),
        ):
            self.assertTrue(
                await chat_router._persist_agent_event_immediate(**kwargs)
            )

        async with self.sessions() as session:
            child = await session.get(AgentRun, "child")
            self.assertEqual(child.status, "failed")
            self.assertEqual(child.input_tokens, 13)
            self.assertEqual(child.output_tokens, 5)
            self.assertEqual(child.total_tokens, 18)

    async def test_immediate_terminal_usage_is_monotonic_and_run_local(self):
        root_usage = _event("usage.updated", 1, run_id="root")
        root_usage["payload"] = {
            "input_tokens": 21,
            "output_tokens": 9,
            "total_tokens": 1,
        }
        child_completed = _event("run.completed", 1)
        child_completed["payload"]["usage"] = {
            "input_tokens": 700,
            "output_tokens": 299,
            "total_tokens": 999,
        }
        root_failed = _event("run.failed", 2, run_id="root")
        root_failed["payload"]["usage"] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        kwargs = {
            "conv_id": "conversation",
            "user_id": "user",
            "root_run_id": "root",
            "requested_model_id": "model",
            "resolved_model_id": "model",
        }
        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(
                chat_router.settings,
                "agent_event_immediate_persist",
                True,
            ),
            patch.object(chat_router.settings, "agent_debug_trace", True),
        ):
            for event in (root_usage, child_completed, root_failed):
                self.assertTrue(
                    await chat_router._persist_agent_event_immediate(
                        **kwargs,
                        event=event,
                    )
                )

        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            child = await session.get(AgentRun, "child")
            self.assertEqual(root.status, "committing")
            self.assertIsNone(root.ended_at)
            self.assertEqual(
                (root.input_tokens, root.output_tokens, root.total_tokens),
                (21, 9, 30),
            )
            self.assertEqual(child.total_tokens, 999)
            root_task = (await session.execute(
                select(TaskItem).where(TaskItem.run_id == "root")
            )).scalar_one()
            self.assertEqual("committing", root_task.status)

        # The final assistant/root transaction replays the already-durable
        # exact terminal event and atomically commits its lifecycle projection.
        async with self.sessions() as session:
            await chat_router._persist_agent_events(
                session,
                **kwargs,
                events=[root_failed],
            )
            await session.commit()
        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            root_task = (await session.execute(
                select(TaskItem).where(TaskItem.run_id == "root")
            )).scalar_one()
            self.assertEqual(root.status, "failed")
            self.assertEqual("fixture_failure", root.finish_reason)
            self.assertEqual(root_task.status, "failed")
            self.assertEqual("fixture_failure", root_task.summary)

    async def test_terminal_projection_reconciles_root_usage_only(self):
        child_completed = _event("run.completed", 1)
        child_completed["payload"]["usage"] = {
            "input_tokens": 700,
            "output_tokens": 299,
            "total_tokens": 999,
        }
        root_failed = _event("run.failed", 2, run_id="root")
        events = [child_completed, root_failed]

        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(chat_router, "emit_event", new=AsyncMock()),
        ):
            self.assertTrue(await chat_router._persist_after_stream(
                "conversation",
                "model",
                "partial assistant output",
                "",
                "",
                "",
                "root",
                "model",
                {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                "stop",
                "bounded fixture failure",
                events,
            ))

        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            child = await session.get(AgentRun, "child")
            conversation = await session.get(Conversation, "conversation")
            message = (await session.execute(
                select(chat_router.Message).where(
                    chat_router.Message.conversation_id == "conversation",
                    chat_router.Message.role == "assistant",
                )
            )).scalar_one()
            self.assertEqual(root.status, "failed")
            self.assertEqual("fixture_failure", root.finish_reason)
            self.assertEqual(
                (root.input_tokens, root.output_tokens, root.total_tokens),
                (13, 5, 18),
            )
            self.assertEqual(child.status, "succeeded")
            self.assertEqual(child.total_tokens, 999)
            self.assertEqual(conversation.total_tokens, 18)
            self.assertEqual(message.total_tokens, 18)

    async def test_projection_failure_terminalizes_and_releases_session(self):
        terminal = _event("run.completed", 2, run_id="root")
        async with self.sessions() as session:
            session.add(Message(
                id="user-message",
                conversation_id="conversation",
                role="user",
                content="Create a recurring cross-domain observation.",
                model_id="model",
            ))
            session.add(AgentEngineSession(
                id="engine-state",
                user_id="user",
                conversation_id="conversation",
                engine_id="claude_code",
                status="running",
                active_run_id="root",
            ))
            await session.commit()
        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(chat_router, "emit_event", new=AsyncMock()),
            patch.object(chat_router.settings, "agent_event_immediate_persist", True),
            patch.object(chat_router.settings, "agent_debug_trace", True),
        ):
            self.assertTrue(await chat_router._persist_agent_event_immediate(
                conv_id="conversation",
                user_id="user",
                root_run_id="root",
                requested_model_id="model",
                resolved_model_id="model",
                event=terminal,
            ))
            with patch.object(
                chat_router,
                "_persist_stream_projection_once",
                new=AsyncMock(side_effect=ValueError(
                    "schedule_control_tools_unknown"
                )),
            ):
                self.assertTrue(await chat_router._persist_after_stream(
                    "conversation",
                    "model",
                    "A controller effect was accepted.",
                    "",
                    "",
                    "",
                    "root",
                    "model",
                    {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10},
                    "stop",
                    None,
                    [terminal],
                ))

        async with self.sessions() as session:
            run = await session.get(AgentRun, "root")
            engine_state = await session.get(AgentEngineSession, "engine-state")
            messages = (await session.execute(
                select(Message).where(
                    Message.conversation_id == "conversation"
                ).order_by(Message.created_at, Message.id)
            )).scalars().all()
            projection_events = (await session.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == "root",
                    AgentRunEvent.event_type == "run.projection_failed",
                )
            )).scalars().all()
            task = (await session.execute(
                select(TaskItem).where(TaskItem.run_id == "root")
            )).scalar_one()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.finish_reason, "terminal_projection_failed")
        self.assertEqual(task.status, "failed")
        self.assertEqual(engine_state.status, "failed")
        self.assertIsNone(engine_state.active_run_id)
        self.assertEqual([message.role for message in messages], ["user", "assistant"])
        self.assertIn("schedule_control_tools_unknown", messages[-1].content)
        self.assertEqual(len(projection_events), 1)

    async def test_post_projection_hook_uses_persisted_root_projection(self):
        child_completed = _event("run.completed", 1)
        child_completed["payload"]["usage"] = {
            "input_tokens": 700,
            "output_tokens": 299,
            "total_tokens": 999,
        }
        root_failed = _event("run.failed", 2, run_id="root")
        events = [child_completed, root_failed]

        async def persist_fixture(*_args, **_kwargs):
            async with self.sessions() as session:
                root = await session.get(AgentRun, "root")
                root.status = "failed"
                root.error = "bounded persisted fixture failure"
                root.input_tokens = 13
                root.output_tokens = 5
                root.total_tokens = 18
                await session.commit()
            return True

        with (
            patch.object(
                chat_router,
                "_persist_after_stream",
                new=AsyncMock(side_effect=persist_fixture),
            ),
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(chat_router, "emit_event", new=AsyncMock()) as emitted,
        ):
            task = chat_router._spawn_persist_then_emit(
                user_id="user",
                conv_id="hook-conversation",
                model_id="model",
                content="partial assistant output",
                reasoning="",
                tool_progress="",
                first_user_content="",
                run_id="root",
                resolved_model_id="model",
                usage={
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                },
                finish_reason="stop",
                error_message=None,
                agent_events=events,
            )
            self.assertTrue(await task)
            await asyncio.sleep(0)

        emitted.assert_awaited_once()
        self.assertEqual(emitted.await_args.args[1], "run.failed")
        notification = emitted.await_args.args[2]
        self.assertEqual(
            notification["error"],
            "bounded persisted fixture failure",
        )
        self.assertEqual(
            notification["usage"],
            {"input_tokens": 13, "output_tokens": 5, "total_tokens": 18},
        )

    async def test_terminal_backfill_waits_for_live_writer_and_restores_events(self):
        timeline: list[str] = []
        writer_started = asyncio.Event()
        release_writer = asyncio.Event()
        root_events = [
            _event("run.started", 1, run_id="root"),
            _event("run.completed", 2, run_id="root"),
        ]

        async def delayed_immediate(**kwargs):
            timeline.append("immediate_started")
            writer_started.set()
            await release_writer.wait()
            timeline.append("immediate_finished")
            return True

        def tracked_sessions():
            timeline.append("db_opened")
            return self.sessions()

        with (
            patch.object(
                chat_router,
                "_persist_agent_event_immediate",
                side_effect=delayed_immediate,
            ),
            patch.object(chat_router, "async_session", side_effect=tracked_sessions),
            patch.object(chat_router, "emit_event", new=AsyncMock()),
        ):
            chat_router._spawn_agent_event_immediate_persist(
                conv_id="conversation",
                user_id="user",
                root_run_id="root",
                requested_model_id="model",
                resolved_model_id="model",
                event=root_events[0],
            )
            final_projection = asyncio.create_task(chat_router._persist_after_stream(
                "conversation",
                "model",
                "",
                "",
                "",
                "",
                "root",
                "model",
                {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
                "stop",
                None,
                root_events,
            ))
            await writer_started.wait()
            await asyncio.sleep(0)
            self.assertNotIn("db_opened", timeline)
            release_writer.set()
            self.assertTrue(await final_projection)

        self.assertLess(
            timeline.index("immediate_finished"),
            timeline.index("db_opened"),
        )
        async with self.sessions() as session:
            persisted = (await session.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == "root")
                .order_by(AgentRunEvent.seq)
            )).scalars().all()
            self.assertEqual(
                [(event.event_type, event.seq) for event in persisted],
                [("run.started", 1), ("run.completed", 2)],
            )
            root = await session.get(AgentRun, "root")
            self.assertEqual(root.status, "succeeded")
            task = (await session.execute(
                select(TaskItem).where(TaskItem.run_id == "root")
            )).scalar_one()
            self.assertEqual(task.status, "succeeded")

    async def test_title_generation_is_outside_durable_projection_barrier(self):
        title_started = asyncio.Event()
        release_title = asyncio.Event()

        async def delayed_title(*_args, **_kwargs):
            title_started.set()
            await release_title.wait()

        async with self.sessions() as session:
            session.add(Message(
                id="durable-first-user",
                conversation_id="conversation",
                role="user",
                content="first user request",
                source="chat",
            ))
            await session.commit()

        events = [_event("run.completed", 2, run_id="root")]
        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(chat_router, "emit_event", new=AsyncMock()),
            patch.object(chat_router, "_generate_title", delayed_title),
        ):
            projected = await asyncio.wait_for(
                chat_router._persist_after_stream(
                    "conversation",
                    "model",
                    "complete answer",
                    "",
                    "",
                    "first user request",
                    "root",
                    "model",
                    {
                        "input_tokens": 11,
                        "output_tokens": 7,
                        "total_tokens": 18,
                    },
                    "stop",
                    None,
                    events,
                ),
                timeout=1,
            )
            self.assertTrue(projected)
            await asyncio.wait_for(title_started.wait(), timeout=1)
            self.assertTrue(any(
                not task.done()
                for task in chat_router._best_effort_tasks
            ))
            release_title.set()
            await asyncio.gather(
                *list(chat_router._best_effort_tasks),
                return_exceptions=True,
            )

    async def test_native_control_does_not_suppress_first_exchange_title(self):
        async with self.sessions() as session:
            session.add_all([
                Message(
                    id="initial-user-message",
                    conversation_id="conversation",
                    role="user",
                    content="Inspect the municipal archive index.",
                    source="chat",
                ),
                Message(
                    id="queued-native-control",
                    conversation_id="conversation",
                    role="user",
                    content="Keep the answer concise.",
                    source="native_control",
                ),
            ])
            await session.commit()

        generated_title = AsyncMock()
        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(chat_router, "emit_event", new=AsyncMock()),
            patch.object(chat_router, "_generate_title", generated_title),
        ):
            self.assertTrue(await chat_router._persist_after_stream(
                "conversation",
                "model",
                "The archive index is ready.",
                "",
                "",
                "transient caller text",
                "root",
                "model",
                {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
                "stop",
                None,
                [_event("run.completed", 2, run_id="root")],
            ))
            await asyncio.gather(
                *list(chat_router._best_effort_tasks),
                return_exceptions=True,
            )

        generated_title.assert_awaited_once()
        arguments = generated_title.await_args.args
        self.assertEqual(arguments[0], "conversation")
        self.assertEqual(arguments[1], "Inspect the municipal archive index.")
        self.assertEqual(arguments[2], "The archive index is ready.")

    async def test_title_save_is_first_writer_wins(self):
        async with self.sessions() as session:
            self.assertTrue(await chat_router._save_conversation_title_if_unset(
                "conversation",
                "Municipal Archive",
                session,
            ))
        async with self.sessions() as session:
            self.assertFalse(await chat_router._save_conversation_title_if_unset(
                "conversation",
                "Later Turn",
                session,
            ))
            conversation = await session.get(Conversation, "conversation")
            self.assertEqual(conversation.title, "Municipal Archive")


if __name__ == "__main__":
    unittest.main()
