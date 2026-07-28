import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import (
    AgentRun,
    AgentRunEvent,
    Artifact,
    Base,
    Conversation,
    Message,
    TaskItem,
    User,
)
from routers import chat_router, workspace_router


class RunCardRehydrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "run-cards.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add(User(
                id="user",
                username="run-card-user",
                hashed_password="hash",
            ))
            session.add(Conversation(
                id="conversation",
                user_id="user",
                model_id="model",
            ))
            await session.commit()
        self.user = SimpleNamespace(id="user")

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    def test_agent_run_error_storage_is_bounded(self):
        bounded = chat_router._bounded_agent_run_error("e" * 9000)
        self.assertEqual(len(bounded), 4000)
        self.assertTrue(bounded.endswith("…"))
        self.assertEqual(
            chat_router._bounded_agent_run_error("short"),
            "short",
        )
        self.assertIsNone(chat_router._bounded_agent_run_error(None))

    async def test_run_cards_restore_exact_turn_semantics_and_recovery(self):
        started_at = datetime(2026, 7, 28, 8, 0, 0, 123456)
        async with self.sessions() as session:
            session.add(Message(
                id="trigger-message",
                conversation_id="conversation",
                role="user",
                content="perform the workflow",
                source="chat",
                created_at=started_at,
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
                agent_kind="primary",
                agent_name="primary",
                requested_tools="{malformed",
                effective_tools='"not-a-list"',
                policy="[not-an-object]",
                tool_events="not-json",
                started_at=started_at,
            ))
            session.add(AgentRun(
                id="child",
                user_id="user",
                conversation_id="conversation",
                parent_run_id="root",
                root_run_id="root",
                source="delegate",
                requested_model_id="model",
                resolved_model_id="model",
                status="succeeded",
                agent_kind="delegate",
                agent_name="delegate-2",
                depth=1,
                error="e" * 9000,
                requested_tools=json.dumps([
                    f"tool-{index}" for index in range(100)
                ]),
                effective_tools=json.dumps(["x" * 70000]),
                policy=json.dumps({
                    "nested": {
                        "value": "p" * 2000,
                    },
                }),
                tool_events=json.dumps([
                    {"detail": "t" * 200}
                    for _index in range(70)
                ]),
                started_at=started_at + timedelta(seconds=1),
                ended_at=started_at + timedelta(seconds=5),
            ))
            session.add_all([
                AgentRunEvent(
                    id="spawn",
                    run_id="child",
                    conversation_id="conversation",
                    user_id="user",
                    parent_run_id="root",
                    seq=0,
                    event_type="agent.spawned",
                    payload=json.dumps({
                        "worker_id": "worker-safety-extraction",
                        "workflow_stage": "workers",
                        "step_type": "worker",
                        "step_id": "safety",
                        "goal": "Extract safety evidence",
                        "delegation_batch_id": "batch-root-call-1",
                        "delegation_slot": 2,
                        "delegation_batch_size": 3,
                    }),
                    event_time=started_at + timedelta(seconds=1),
                ),
                AgentRunEvent(
                    id="tool-failed",
                    run_id="child",
                    conversation_id="conversation",
                    user_id="user",
                    parent_run_id="root",
                    seq=1,
                    event_type="tool.failed",
                    tool_name="web_search",
                    tool_call_id="call-1",
                    payload=json.dumps({
                        "tool_name": "web_search",
                        "error": "provider timeout",
                    }),
                    event_time=started_at + timedelta(seconds=2),
                ),
                AgentRunEvent(
                    id="tool-conflicting-completed",
                    run_id="child",
                    conversation_id="conversation",
                    user_id="user",
                    parent_run_id="root",
                    seq=2,
                    event_type="tool.completed",
                    tool_name="web_search",
                    tool_call_id="call-1",
                    payload=json.dumps({"tool_name": "web_search"}),
                    event_time=started_at + timedelta(
                        seconds=2,
                        microseconds=1,
                    ),
                ),
                AgentRunEvent(
                    id="tool-completed",
                    run_id="child",
                    conversation_id="conversation",
                    user_id="user",
                    parent_run_id="root",
                    seq=3,
                    event_type="tool.completed",
                    tool_name="web_search",
                    tool_call_id="call-2",
                    payload=json.dumps({"tool_name": "web_search"}),
                    event_time=started_at + timedelta(seconds=3),
                ),
                AgentRunEvent(
                    id="completed",
                    run_id="child",
                    conversation_id="conversation",
                    user_id="user",
                    parent_run_id="root",
                    seq=4,
                    event_type="run.completed",
                    payload=json.dumps({
                        "authoritative": True,
                        "completion_quality": "degraded",
                        "terminal_reason": "post_dispatch_stream_recovery_synthesis",
                    }),
                    event_time=started_at + timedelta(seconds=4),
                ),
                AgentRunEvent(
                    id="late-conflicting-failure",
                    run_id="child",
                    conversation_id="conversation",
                    user_id="user",
                    parent_run_id="root",
                    seq=5,
                    event_type="run.failed",
                    payload=json.dumps({
                        "authoritative": True,
                        "terminal_reason": "late_conflict",
                        "error": "must not replace first terminal",
                    }),
                    event_time=started_at + timedelta(seconds=5),
                ),
            ])
            session.add(TaskItem(
                id="child-task",
                user_id="user",
                conversation_id="conversation",
                run_id="child",
                root_run_id="root",
                parent_run_id="root",
                task_key="run:child",
                kind="delegate",
                title="delegate-2",
                status="succeeded",
                agent_name="delegate-2",
                summary="completed",
                started_at=started_at + timedelta(seconds=1),
                updated_at=started_at + timedelta(seconds=4),
            ))
            await session.commit()

            payload = await workspace_router.list_run_cards(
                "conversation",
                20,
                self.user,
                session,
            )
            runs_payload = await workspace_router.list_runs(
                "conversation",
                200,
                self.user,
                session,
            )

        self.assertTrue(payload["has_active_runs"])
        root = payload["roots"][0]
        self.assertEqual(root["trigger_message_id"], "trigger-message")
        self.assertIsNone(root["assistant_message_id"])
        self.assertEqual(root["mapping_status"], "exact_no_assistant")
        child = next(run for run in root["runs"] if run["id"] == "child")
        self.assertEqual(child["display_name"], "worker-safety-extraction")
        self.assertEqual(child["lifecycle_status"], "degraded")
        self.assertTrue(child["recovered"])
        self.assertEqual(
            [tool["status"] for tool in child["tools"]],
            ["failed", "success"],
        )
        self.assertTrue(child["tools"][0]["terminal_conflict"])
        self.assertTrue(child["tools"][0]["later_success_same_tool"])
        self.assertEqual(child["tools"][0]["tool_call_id"], "call-1")
        self.assertEqual(child["tools"][1]["tool_call_id"], "call-2")
        self.assertEqual(child["delegation_batch_id"], "batch-root-call-1")
        self.assertEqual(child["delegation_slot"], 2)
        self.assertEqual(child["delegation_batch_size"], 3)
        self.assertTrue(child["terminal_event_conflict"])
        self.assertFalse(child["terminal_projection_conflict"])
        self.assertEqual(len(child["error"]), 4000)
        self.assertEqual(child["error_source_chars"], 9000)
        self.assertEqual(child["error_source_bytes"], 9000)
        self.assertTrue(child["error_truncated"])
        self.assertEqual(child["requested_tool_count"], 100)
        self.assertEqual(len(child["requested_tools"]), 64)
        self.assertTrue(child["requested_tools_truncated"])
        self.assertFalse(child["requested_tools_malformed"])
        self.assertIsNone(child["effective_tool_count"])
        self.assertEqual(child["effective_tools"], [])
        self.assertTrue(child["effective_tools_truncated"])
        self.assertFalse(child["effective_tools_malformed"])
        self.assertEqual(child["policy_item_count"], 1)
        self.assertTrue(child["policy_truncated"])
        self.assertEqual(child["tool_event_count"], 70)
        self.assertEqual(len(child["tool_events"]), 64)
        self.assertTrue(child["tool_events_truncated"])
        self.assertTrue(child["dto_truncated"])
        self.assertTrue(payload["projection_truncated"]["run_dtos"])
        self.assertEqual(payload["run_dto_truncated_count"], 2)
        list_child = next(
            run for run in runs_payload["runs"] if run["id"] == "child"
        )
        for key in (
            "error",
            "error_source_chars",
            "error_source_bytes",
            "error_truncated",
            "requested_tools",
            "requested_tool_count",
            "requested_tools_source_bytes",
            "requested_tools_truncated",
            "effective_tools",
            "effective_tool_count",
            "effective_tools_source_bytes",
            "effective_tools_truncated",
            "policy",
            "policy_item_count",
            "policy_source_bytes",
            "policy_truncated",
            "tool_events",
            "tool_event_count",
            "tool_events_source_bytes",
            "tool_events_truncated",
            "dto_truncated",
            "dto_truncated_fields",
        ):
            self.assertEqual(list_child[key], child[key], key)
        self.assertTrue(
            runs_payload["projection_truncated"]["run_dtos"]
        )
        root_card = next(run for run in root["runs"] if run["id"] == "root")
        self.assertEqual(root_card["requested_tools"], [])
        self.assertEqual(root_card["effective_tools"], [])
        self.assertIsNone(root_card["policy"])
        self.assertEqual(root_card["tool_events"], [])
        self.assertTrue(root_card["requested_tools_malformed"])
        self.assertTrue(root_card["requested_tools_truncated"])
        self.assertEqual(
            child["status_reason"],
            "post_dispatch_stream_recovery_synthesis",
        )

    async def test_list_runs_returns_latest_page_plus_active_old_run(self):
        base = datetime(2026, 7, 28, 9, 0, 0)
        async with self.sessions() as session:
            for index in range(205):
                session.add(AgentRun(
                    id=f"run-{index:03d}",
                    user_id="user",
                    conversation_id="conversation",
                    root_run_id=f"run-{index:03d}",
                    source="chat",
                    requested_model_id="model",
                    resolved_model_id="model",
                    status="planned" if index == 0 else "succeeded",
                    agent_kind="primary",
                    agent_name="primary",
                    started_at=base + timedelta(seconds=index),
                    ended_at=(
                        None
                        if index == 0
                        else base + timedelta(seconds=index, microseconds=1)
                    ),
                ))
            await session.commit()

            payload = await workspace_router.list_runs(
                "conversation",
                200,
                self.user,
                session,
            )

        run_ids = [run["id"] for run in payload["runs"]]
        self.assertEqual(run_ids[0], "run-204")
        self.assertIn("run-000", run_ids)
        self.assertNotIn("run-001", run_ids)
        self.assertTrue(payload["has_active_runs"])
        self.assertEqual(payload["returned_count"], 201)

    async def test_orphan_active_child_remains_global_activity_truth(self):
        started_at = datetime(2026, 7, 28, 10, 0, 0)
        async with self.sessions() as session:
            session.add(AgentRun(
                id="orphan-child",
                user_id="user",
                conversation_id="conversation",
                parent_run_id="missing-parent",
                root_run_id="missing-root",
                source="delegate",
                requested_model_id="model",
                resolved_model_id="model",
                status="running",
                agent_kind="delegate",
                agent_name="worker-evidence",
                depth=1,
                started_at=started_at,
            ))
            await session.commit()

            payload = await workspace_router.list_run_cards(
                "conversation",
                20,
                self.user,
                session,
            )

        self.assertTrue(payload["has_active_runs"])
        self.assertEqual(payload["poll_after_ms"], 2500)
        self.assertEqual(payload["orphan_active_root_count"], 1)
        self.assertEqual(len(payload["roots"]), 1)
        root = payload["roots"][0]
        self.assertTrue(root["orphaned_root"])
        self.assertEqual(root["root_run_id"], "missing-root")
        self.assertEqual(root["mapping_status"], "orphaned_root")
        self.assertTrue(root["active"])
        self.assertEqual(root["runs"][0]["id"], "orphan-child")

    async def test_event_cap_preserves_spawn_terminal_and_quality_anchors(self):
        started_at = datetime(2026, 7, 28, 11, 0, 0)
        async with self.sessions() as session:
            session.add(AgentRun(
                id="root-anchor",
                user_id="user",
                conversation_id="conversation",
                root_run_id="root-anchor",
                source="chat",
                requested_model_id="model",
                resolved_model_id="model",
                status="succeeded",
                agent_kind="primary",
                agent_name="delegate-1",
                started_at=started_at,
                ended_at=started_at + timedelta(seconds=20),
            ))
            session.add_all([
                AgentRunEvent(
                    id="anchor-spawn",
                    run_id="root-anchor",
                    conversation_id="conversation",
                    user_id="user",
                    seq=0,
                    event_type="agent.spawned",
                    payload=json.dumps({
                        "worker_id": "evidence-worker",
                    }),
                    event_time=started_at,
                ),
                AgentRunEvent(
                    id="anchor-terminal",
                    run_id="root-anchor",
                    conversation_id="conversation",
                    user_id="user",
                    seq=1,
                    event_type="run.completed",
                    payload=json.dumps({
                        "authoritative": True,
                        "completion_quality": "degraded",
                        "terminal_reason": "bounded_quality_result",
                    }),
                    event_time=started_at + timedelta(seconds=1),
                ),
                AgentRunEvent(
                    id="anchor-verifier",
                    run_id="root-anchor",
                    conversation_id="conversation",
                    user_id="user",
                    seq=2,
                    event_type="verifier.completed",
                    payload=json.dumps({
                        "verdict": "pass",
                        "reason": "quality contract passed",
                    }),
                    event_time=started_at + timedelta(seconds=2),
                ),
            ])
            for index in range(10):
                session.add(AgentRunEvent(
                    id=f"late-tool-{index}",
                    run_id="root-anchor",
                    conversation_id="conversation",
                    user_id="user",
                    seq=10 + index,
                    event_type="tool.completed",
                    tool_name=f"tool-{index}",
                    tool_call_id=f"call-{index}",
                    payload=json.dumps({
                        "tool_name": f"tool-{index}",
                    }),
                    event_time=started_at + timedelta(seconds=10 + index),
                ))
            await session.commit()

            with patch.object(
                workspace_router,
                "_RUN_CARD_MAX_EVENTS",
                2,
            ):
                payload = await workspace_router.list_run_cards(
                    "conversation",
                    20,
                    self.user,
                    session,
                )

        self.assertTrue(payload["projection_truncated"]["events"])
        card = payload["roots"][0]["runs"][0]
        self.assertEqual(card["display_name"], "evidence-worker")
        self.assertEqual(card["lifecycle_status"], "degraded")
        self.assertEqual(card["status_reason"], "bounded_quality_result")
        self.assertEqual(card["verifier"]["status"], "pass")
        self.assertEqual(
            card["verifier"]["reason"],
            "quality contract passed",
        )

    async def test_task_and_artifact_dtos_are_malformed_safe_and_bounded(self):
        started_at = datetime(2026, 7, 28, 12, 0, 0)
        async with self.sessions() as session:
            session.add(AgentRun(
                id="root-bounded",
                user_id="user",
                conversation_id="conversation",
                root_run_id="root-bounded",
                source="chat",
                requested_model_id="model",
                resolved_model_id="model",
                status="succeeded",
                agent_kind="primary",
                agent_name="primary",
                started_at=started_at,
                ended_at=started_at + timedelta(seconds=1),
            ))
            session.add(TaskItem(
                id="bounded-task",
                user_id="user",
                conversation_id="conversation",
                run_id="root-bounded",
                root_run_id="root-bounded",
                task_key="run:root-bounded",
                kind="run",
                title="bounded task",
                status="succeeded",
                summary="s" * 5000,
                error="e" * 6000,
                metadata_json="{malformed",
                started_at=started_at,
                updated_at=started_at,
            ))
            for index in range(30):
                session.add(Artifact(
                    id=f"artifact-{index:02d}",
                    user_id="user",
                    conversation_id="conversation",
                    run_id="root-bounded",
                    root_run_id="root-bounded",
                    kind="file",
                    title=f"artifact {index}",
                    path=f"artifact-{index}.md",
                    summary="a" * 3000,
                    metadata_json=(
                        "{malformed"
                        if index == 29
                        else json.dumps({
                            "nested": {
                                "value": "v" * 2000,
                            },
                        })
                    ),
                    created_at=started_at + timedelta(microseconds=index),
                ))
            await session.commit()

            payload = await workspace_router.list_run_cards(
                "conversation",
                20,
                self.user,
                session,
            )

        card = payload["roots"][0]["runs"][0]
        self.assertEqual(len(card["task"]["summary"]), 2000)
        self.assertTrue(card["task"]["summary_truncated"])
        self.assertEqual(len(card["task"]["error"]), 4000)
        self.assertTrue(card["task"]["error_truncated"])
        self.assertEqual(card["task"]["metadata"], {})
        self.assertTrue(card["task"]["metadata_truncated"])
        self.assertEqual(card["artifact_count"], 30)
        self.assertEqual(len(card["artifacts"]), 24)
        self.assertTrue(card["artifacts_truncated"])
        malformed = next(
            artifact
            for artifact in card["artifacts"]
            if artifact["id"] == "artifact-29"
        )
        self.assertEqual(malformed["metadata"], {})
        self.assertTrue(malformed["metadata_truncated"])
        self.assertEqual(len(malformed["summary"]), 2000)
        self.assertTrue(malformed["summary_truncated"])


if __name__ == "__main__":
    unittest.main()
