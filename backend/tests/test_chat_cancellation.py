import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import (
    AgentRun,
    AgentRunEvent,
    Base,
    Conversation,
    Message,
    TaskItem,
    User,
)
from routers import chat_router
from routers.hook_router import ALLOWED_EVENTS
from schemas import ChatRequest


def _terminal(event_type: str, run_id: str, seq: int) -> dict:
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
        "payload": {
            "finish_reason": (
                "task_cancelled" if event_type == "run.cancelled" else "stop"
            ),
            "terminal_reason": (
                "task_cancelled" if event_type == "run.cancelled" else "stop"
            ),
            "usage": {
                "input_tokens": 13,
                "output_tokens": 5,
                "total_tokens": 18,
            },
        },
    }


class ChatStreamFailureClassificationTests(unittest.TestCase):
    def test_child_and_provisional_failures_do_not_override_root_success(self):
        child_failed = _terminal("run.failed", "child", 90)
        child_failed["payload"].update({
            "error": "child output contract failed",
            "authoritative": True,
        })
        provisional_root_failed = _terminal("run.failed", "root", 1)
        provisional_root_failed["payload"].update({
            "error": "provisional convergence failure",
            "authoritative": False,
        })
        root_completed = _terminal("run.completed", "root", 2)
        root_completed["payload"]["authoritative"] = True
        events = [child_failed, provisional_root_failed, root_completed]

        self.assertEqual(
            ("succeeded", None),
            chat_router._agent_event_terminal_status(events, run_id="root"),
        )
        self.assertEqual(
            (None, False),
            chat_router._reconcile_root_stream_error(
                events,
                run_id="root",
                stream_error="late socket close",
            ),
        )

    def test_execution_failure_and_stream_interruption_have_distinct_notices(self):
        root_failed = _terminal("run.failed", "root", 1)
        root_failed["payload"].update({
            "error": "root verifier exhausted",
            "authoritative": True,
        })
        error, execution_failed = chat_router._reconcile_root_stream_error(
            [root_failed],
            run_id="root",
            stream_error="transport symptom",
        )
        execution_notice = chat_router._chat_stream_failure_notice(
            error or "",
            execution_failed=execution_failed,
            has_partial_content=True,
        )
        self.assertIn("本次任务执行失败", execution_notice)
        self.assertNotIn("流式输出过程中中断", execution_notice)
        self.assertIn("root verifier exhausted", execution_notice)

        error, execution_failed = chat_router._reconcile_root_stream_error(
            [],
            run_id="root",
            stream_error="Harness 服务响应超时。",
        )
        stream_notice = chat_router._chat_stream_failure_notice(
            error or "",
            execution_failed=execution_failed,
            has_partial_content=True,
        )
        self.assertIn("流式输出过程中中断", stream_notice)
        self.assertNotIn("本次任务执行失败", stream_notice)

    def test_cancelled_root_is_never_reconciled_as_success(self):
        cancelled = _terminal("run.cancelled", "root", 3)
        cancelled["payload"].update({
            "cancellation_source": "client_disconnected",
            "exception_class": "CancelledError",
        })
        error, execution_failed = chat_router._reconcile_root_stream_error(
            [cancelled],
            run_id="root",
            stream_error=None,
        )
        self.assertIn("cancelled before completion", error)
        self.assertFalse(execution_failed)

    def test_contract_and_provider_debug_summary_are_bounded(self):
        failed = _terminal("run.failed", "root", 4)
        failed["payload"].update({
            "finish_reason": "provider_tool_stream_corrupt",
            "failure_class": "provider_protocol",
            "error": "must-not-be-copied secret-token",
            "run_contract": {
                "quality_ledger": {
                    "entry_count_active": 9,
                    "active_nonverified_count": 2,
                    "required_failed_count": 1,
                    "pending_dispatch_count": 1,
                    "completion_blocker_count": 2,
                    "completion_allowed": False,
                    "entries": [{"receipt": {"args": "must-not-persist"}}],
                },
            },
        })
        contract = chat_router._unsatisfied_contract_summary(
            [failed],
            run_id="root",
        )
        provider = chat_router._provider_failure_summary(
            [failed],
            run_id="root",
        )
        self.assertEqual(contract["status"], "unsatisfied")
        self.assertEqual(contract["completion_blocker_count"], 2)
        self.assertNotIn("entries", contract)
        self.assertTrue(provider["reported"])
        rendered = json.dumps(
            {"contract": contract, "provider": provider},
            ensure_ascii=False,
        )
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("must-not-persist", rendered)

        debug_gap = {
            "event_type": "debug.tool.stream.salvage.reviewed",
            "run_id": "root",
            "seq": 5,
            "payload": {
                "unresolved_call_count": 3,
                "required_logical_call_count": 1,
                "calls": [{"arguments": "must-not-persist"}],
            },
        }
        fallback = chat_router._unsatisfied_contract_summary(
            [debug_gap],
            run_id="root",
        )
        self.assertEqual(fallback["status"], "unsatisfied")
        self.assertEqual(fallback["unresolved_call_count"], 3)
        self.assertFalse(fallback["sealed_contract_snapshot"])
        self.assertNotIn("calls", fallback)

    def test_default_backend_timeout_exceeds_harness_provider_deadline(self):
        self.assertGreater(
            chat_router.settings.harness_stream_timeout_seconds,
            2400,
        )

    def test_lifecycle_persistence_does_not_depend_on_debug_mode(self):
        with (
            patch.object(
                chat_router.settings,
                "agent_event_immediate_persist",
                True,
            ),
            patch.object(chat_router.settings, "agent_debug_trace", False),
        ):
            self.assertTrue(
                chat_router._should_persist_agent_event_immediately(
                    "agent.spawned"
                )
            )
            self.assertTrue(
                chat_router._should_persist_agent_event_immediately(
                    "tool.failed"
                )
            )
            self.assertFalse(
                chat_router._should_persist_agent_event_immediately(
                    "debug.fixture"
                )
            )


class DetachedStreamRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_relay_backpressures_and_detach_wakes_publisher(self):
        relay = chat_router._DetachedStreamRelay()
        for index in range(chat_router._DETACHED_STREAM_MAX_CHUNKS):
            await relay.publish(f"chunk-{index}")

        blocked = asyncio.create_task(relay.publish("one-too-many"))
        await asyncio.sleep(0)
        self.assertFalse(blocked.done())

        relay.detach()
        await asyncio.wait_for(blocked, timeout=1)
        self.assertEqual("consumer_closed", relay.detach_reason)
        if not relay._queue.empty():
            self.assertIs(
                chat_router._DETACHED_STREAM_DONE,
                relay._queue.get_nowait(),
            )
        self.assertTrue(relay._queue.empty())

    async def test_stalled_subscriber_is_detached_without_blocking_producer(self):
        relay = chat_router._DetachedStreamRelay()
        for index in range(chat_router._DETACHED_STREAM_MAX_CHUNKS):
            await relay.publish(f"chunk-{index}")

        with patch.object(
            chat_router,
            "_DETACHED_STREAM_PUBLISH_WAIT_SECONDS",
            0.01,
        ):
            await asyncio.wait_for(
                relay.publish("producer-must-continue"),
                timeout=0.2,
            )

        self.assertEqual(
            "subscriber_backpressure",
            relay.detach_reason,
        )
        self.assertFalse(relay._attached)
        self.assertEqual(0, relay._queued_bytes)

    async def test_relay_drains_buffer_before_propagating_error(self):
        relay = chat_router._DetachedStreamRelay()
        await relay.publish("partial")
        relay.finish(RuntimeError("producer failed"))
        iterator = relay.stream()
        self.assertEqual("partial", await anext(iterator))
        with self.assertRaisesRegex(RuntimeError, "producer failed"):
            await anext(iterator)

    async def test_prestart_cancel_invokes_recovery_callback(self):
        entered = False
        recovered = asyncio.Event()

        async def operation():
            nonlocal entered
            entered = True
            await asyncio.sleep(10)

        relay = chat_router._DetachedStreamRelay()
        task = chat_router._track_detached_chat_producer(
            conv_id="conversation",
            run_id="root",
            relay=relay,
            operation=operation(),
            producer_started=lambda: entered,
            on_prestart_exit=lambda _error: recovered.set(),
        )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(recovered.wait(), timeout=1)
        self.assertFalse(entered)


class ChatCancellationProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add(User(
                id="user",
                username="cancel-user",
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
        await self.engine.dispose()

    async def test_cancelled_root_is_persisted_without_child_terminal_override(self):
        self.assertIn("run.cancelled", ALLOWED_EVENTS)
        events = [
            _terminal("run.completed", "child", 99),
            _terminal("run.cancelled", "root", 2),
        ]
        self.assertEqual(
            chat_router._agent_event_terminal_status(events, run_id="root"),
            ("cancelled", None),
        )
        self.assertEqual(
            chat_router._agent_event_terminal_status(
                events[:1],
                run_id="root",
            ),
            (None, None),
        )

        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(chat_router, "emit_event", new=AsyncMock()),
        ):
            await chat_router._persist_after_stream(
                "conversation",
                "model",
                "",
                "",
                "",
                "",
                "root",
                "model",
                {"input_tokens": 13, "output_tokens": 5, "total_tokens": 18},
                "stop",
                "transport disconnected after terminal",
                events,
            )

        async with self.sessions() as session:
            run = await session.get(AgentRun, "root")
            self.assertEqual(run.status, "cancelled")
            self.assertEqual(run.finish_reason, "task_cancelled")
            self.assertIsNone(run.error)
            self.assertIsNotNone(run.ended_at)
            self.assertEqual(run.total_tokens, 18)
            persisted = (await session.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == "root",
                    AgentRunEvent.event_type == "run.cancelled",
                )
            )).scalars().all()
            self.assertEqual(len(persisted), 1)
            task = (await session.execute(
                select(TaskItem).where(TaskItem.run_id == "root")
            )).scalar_one()
            self.assertEqual(task.status, "cancelled")
            self.assertIsNotNone(task.ended_at)
            assistant = (await session.execute(
                select(Message).where(
                    Message.conversation_id == "conversation",
                    Message.role == "assistant",
                )
            )).scalar_one()
            self.assertIn("流式输出过程中中断", assistant.content)
            self.assertIn("cancelled before completion", assistant.content)

    async def test_root_terminal_reconciles_orphaned_descendant(self):
        async with self.sessions() as session:
            session.add(AgentRun(
                id="orphan-child",
                user_id="user",
                conversation_id="conversation",
                parent_run_id="root",
                root_run_id="root",
                source="delegate",
                requested_model_id="model",
                resolved_model_id="model",
                status="running",
                agent_kind="delegate",
                agent_name="Evidence extraction",
                depth=1,
            ))
            session.add(TaskItem(
                id="orphan-task",
                user_id="user",
                conversation_id="conversation",
                run_id="orphan-child",
                root_run_id="root",
                parent_run_id="root",
                task_key="run:orphan-child",
                kind="delegate",
                title="Evidence extraction",
                status="running",
            ))
            await session.commit()

        events = [_terminal("run.cancelled", "root", 2)]
        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(chat_router, "emit_event", new=AsyncMock()),
        ):
            self.assertTrue(await chat_router._persist_after_stream(
                "conversation",
                "model",
                "",
                "",
                "",
                "",
                "root",
                "model",
                {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                "task_cancelled",
                None,
                events,
            ))

        async with self.sessions() as session:
            child = await session.get(AgentRun, "orphan-child")
            self.assertEqual(child.status, "cancelled")
            self.assertEqual(child.finish_reason, "parent_run_terminal")
            task = await session.get(TaskItem, "orphan-task")
            self.assertEqual(task.status, "cancelled")
            terminal = (await session.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == "orphan-child",
                    AgentRunEvent.event_type == "run.cancelled",
                )
            )).scalar_one()
            payload = json.loads(terminal.payload)
            self.assertEqual(
                payload["cancellation_source"],
                "root_run_terminal",
            )
            self.assertEqual(
                payload["parent_terminal_status"],
                "cancelled",
            )

    async def test_missing_root_terminal_fails_root_task_and_closes_children(
        self,
    ):
        async with self.sessions() as session:
            session.add(AgentRun(
                id="orphan-child",
                user_id="user",
                conversation_id="conversation",
                parent_run_id="root",
                root_run_id="root",
                source="delegate",
                requested_model_id="model",
                resolved_model_id="model",
                status="running",
                agent_kind="delegate",
                agent_name="Evidence extraction",
                depth=1,
            ))
            session.add_all([
                TaskItem(
                    id="root-task",
                    user_id="user",
                    conversation_id="conversation",
                    run_id="root",
                    root_run_id="root",
                    task_key="run:root",
                    kind="primary",
                    title="primary",
                    status="running",
                ),
                TaskItem(
                    id="orphan-task",
                    user_id="user",
                    conversation_id="conversation",
                    run_id="orphan-child",
                    root_run_id="root",
                    parent_run_id="root",
                    task_key="run:orphan-child",
                    kind="delegate",
                    title="Evidence extraction",
                    status="running",
                ),
            ])
            await session.commit()

        debug_event = {
            "type": "agent_event",
            "event_type": "debug.backend_stream.terminated",
            "run_id": "root",
            "root_run_id": "root",
            "seq": 7,
            "payload": {
                "termination_source": "upstream_harness_timeout",
            },
        }
        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(chat_router, "emit_event", new=AsyncMock()),
        ):
            self.assertTrue(await chat_router._persist_after_stream(
                "conversation",
                "model",
                "partial draft",
                "",
                "",
                "",
                "root",
                "model",
                {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
                "stop",
                "Harness service timed out.",
                [debug_event],
            ))

        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            self.assertEqual("failed", root.status)
            self.assertEqual(
                "missing_authoritative_root_terminal",
                root.finish_reason,
            )
            root_task = await session.get(TaskItem, "root-task")
            self.assertEqual("failed", root_task.status)
            child = await session.get(AgentRun, "orphan-child")
            self.assertEqual("cancelled", child.status)
            child_task = await session.get(TaskItem, "orphan-task")
            self.assertEqual("cancelled", child_task.status)
            terminals = (await session.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == "root",
                    AgentRunEvent.event_type.in_(
                        ("run.completed", "run.failed", "run.cancelled")
                    ),
                )
            )).scalars().all()
            self.assertEqual(1, len(terminals))
            terminal_payload = json.loads(terminals[0].payload)
            self.assertEqual("stream_transport", terminal_payload["failure_class"])
            self.assertEqual(8, terminals[0].seq)
            assistant = (await session.execute(
                select(Message).where(
                    Message.conversation_id == "conversation",
                    Message.role == "assistant",
                )
            )).scalar_one()
            self.assertIn("流式输出过程中中断", assistant.content)

    async def test_successful_tool_only_root_still_projects_assistant_turn(self):
        async with self.sessions() as session:
            session.add(Message(
                id="user-message",
                conversation_id="conversation",
                role="user",
                content="create an artifact",
                source="chat",
            ))
            await session.commit()

        completed = _terminal("run.completed", "root", 2)
        completed["payload"]["authoritative"] = True
        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(chat_router, "emit_event", new=AsyncMock()),
        ):
            self.assertTrue(await chat_router._persist_after_stream(
                "conversation",
                "model",
                "",
                "",
                "artifact created",
                "",
                "root",
                "model",
                {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
                "stop",
                None,
                [completed],
            ))

        async with self.sessions() as session:
            assistants = (await session.execute(
                select(Message).where(
                    Message.conversation_id == "conversation",
                    Message.role == "assistant",
                )
            )).scalars().all()
            self.assertEqual(1, len(assistants))
            self.assertEqual("", assistants[0].content)
            await chat_router._assert_no_unprojected_primary_turn(
                session,
                "conversation",
            )

    async def test_final_projection_replays_existing_terminal_instead_of_synthesizing(
        self,
    ):
        completed = _terminal("run.completed", "root", 5)
        completed["payload"]["authoritative"] = True
        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            root.status = "committing"
            session.add(AgentRunEvent(
                id="durable-terminal",
                run_id="root",
                conversation_id="conversation",
                user_id="user",
                seq=5,
                event_type="run.completed",
                payload=json.dumps(completed["payload"]),
            ))
            await session.commit()

        trailing_debug = {
            "type": "agent_event",
            "event_type": "debug.backend_stream.terminated",
            "run_id": "root",
            "root_run_id": "root",
            "seq": 6,
            "payload": {"termination_source": "downstream_send_failed"},
        }
        with (
            patch.object(chat_router, "async_session", self.sessions),
            patch.object(chat_router, "emit_event", new=AsyncMock()),
        ):
            self.assertTrue(await chat_router._persist_after_stream(
                "conversation",
                "model",
                "completed response",
                "",
                "",
                "",
                "root",
                "model",
                {"input_tokens": 13, "output_tokens": 5, "total_tokens": 18},
                "stop",
                "late transport symptom",
                [trailing_debug],
            ))

        async with self.sessions() as session:
            root = await session.get(AgentRun, "root")
            self.assertEqual("succeeded", root.status)
            terminals = (await session.execute(
                select(AgentRunEvent).where(
                    AgentRunEvent.run_id == "root",
                    AgentRunEvent.event_type.in_(
                        ("run.completed", "run.failed", "run.cancelled")
                    ),
                )
            )).scalars().all()
            self.assertEqual(["run.completed"], [
                event.event_type for event in terminals
            ])
            assistant = (await session.execute(
                select(Message).where(
                    Message.conversation_id == "conversation",
                    Message.role == "assistant",
                )
            )).scalar_one()
            self.assertEqual("completed response", assistant.content)

    async def test_explicit_sse_close_detaches_but_run_finishes_in_background(self):
        harness_stream_closed = asyncio.Event()
        release_harness = asyncio.Event()
        persist_finished = asyncio.Event()
        persisted_calls: list[dict] = []
        observed_client_timeout = None

        class FakeHarnessResponse:
            status_code = 200

            def __init__(self, request_json: dict):
                self.request_json = request_json

            async def aiter_lines(self):
                run_id = self.request_json["run_metadata"]["run_id"]
                event = _terminal("run.started", run_id, 1)
                event["root_run_id"] = run_id
                event["parent_run_id"] = None
                event["agent_kind"] = "primary"
                event["agent_name"] = "primary"
                event["depth"] = 0
                event["payload"] = {"model_id": "deepseek_v4_pro"}
                chunk = {
                    "choices": [{
                        "delta": {"agent_event": event},
                        "index": 0,
                    }]
                }
                yield "data: " + json.dumps(chunk)
                yield "data: " + json.dumps({
                    "choices": [{
                        "delta": {"content": "partial draft"},
                        "index": 0,
                    }],
                })
                await release_harness.wait()
                completed = _terminal("run.completed", run_id, 2)
                completed["root_run_id"] = run_id
                completed["payload"]["authoritative"] = True
                yield "data: " + json.dumps({
                    "choices": [{
                        "delta": {"agent_event": completed},
                        "index": 0,
                        "finish_reason": "stop",
                    }],
                })
                yield "data: [DONE]"

        class FakeStreamContext:
            def __init__(self, request_json: dict):
                self.response = FakeHarnessResponse(request_json)

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, exc_type, exc, traceback):
                harness_stream_closed.set()

        class FakeClient:
            def __init__(self, *args, **kwargs):
                nonlocal observed_client_timeout
                observed_client_timeout = kwargs.get("timeout")

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, **kwargs):
                return FakeStreamContext(kwargs["json"])

        def capture_persist(**kwargs):
            persisted_calls.append(kwargs)
            async def durable_projection():
                persist_finished.set()
                return True
            return asyncio.create_task(durable_projection())

        async with self.sessions() as session:
            user = await session.get(User, "user")
            # This test starts a fresh streamed root.  The fixture's running
            # root belongs to the direct projection test above and would
            # correctly be treated as an unprojected predecessor here.
            fixture_root = await session.get(AgentRun, "root")
            await session.delete(fixture_root)
            await session.commit()
            with (
                patch.object(
                    chat_router,
                    "resolve_model_config",
                    new=AsyncMock(return_value={
                        "id": "deepseek_v4_pro",
                        "base_url": "http://provider/v1",
                        "api_model": "AgentModel",
                    }),
                ),
                patch.object(
                    chat_router,
                    "ensure_workspace_async",
                    new=AsyncMock(),
                ),
                patch.object(chat_router, "emit_event", new=AsyncMock()),
                patch.object(chat_router.httpx, "AsyncClient", FakeClient),
                patch.object(
                    chat_router,
                    "_append_backend_stream_debug_file",
                ),
                patch.object(
                    chat_router,
                    "_spawn_persist_then_emit",
                    side_effect=capture_persist,
                ),
                patch.object(
                    chat_router.settings,
                    "agent_event_immediate_persist",
                    False,
                ),
            ):
                response = await chat_router._chat_stream(
                    ChatRequest(
                        conversation_id="conversation",
                        content="keep streaming",
                        model_id="deepseek_v4_pro",
                    ),
                    user,
                    session,
                )
                iterator = response.body_iterator
                routed = await anext(iterator)
                self.assertIn("routed_model", str(routed))
                forwarded = await anext(iterator)
                self.assertIn("agent_event", str(forwarded))
                partial = await anext(iterator)
                self.assertIn("partial draft", str(partial))
                await iterator.aclose()
                self.assertFalse(harness_stream_closed.is_set())
                self.assertEqual(persisted_calls, [])
                release_harness.set()
                await asyncio.wait_for(harness_stream_closed.wait(), timeout=1)
                await asyncio.wait_for(persist_finished.wait(), timeout=1)
                pending = [
                    task
                    for task in chat_router._background_tasks
                    if not task.done()
                ]
                if pending:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *(asyncio.shield(task) for task in pending),
                            return_exceptions=True,
                        ),
                        timeout=1,
                    )

        self.assertTrue(harness_stream_closed.is_set())
        self.assertEqual(
            observed_client_timeout,
            chat_router.settings.harness_stream_timeout_seconds,
        )
        self.assertEqual(len(persisted_calls), 1)
        persisted = persisted_calls[0]
        root_run_id = persisted["run_id"]
        root_events = [
            event for event in persisted["agent_events"]
            if event.get("run_id") == root_run_id
        ]
        self.assertEqual(
            [event["event_type"] for event in root_events],
            [
                "run.started",
                "run.completed",
                "debug.backend_stream.terminated",
            ],
        )
        self.assertEqual(persisted["finish_reason"], "stop")
        self.assertIsNone(persisted["error_message"])
        self.assertEqual("partial draft", persisted["content"])
        debug_payload = root_events[2]["payload"]
        self.assertEqual(
            debug_payload["termination_source"],
            "upstream_harness_completed",
        )
        self.assertEqual(debug_payload["last_root_event_type"], "run.completed")
        self.assertEqual(debug_payload["root_phase"], "terminal")

    async def test_abort_source_requires_observed_disconnect_or_shutdown(self):
        observation = chat_router._StreamObservation(
            run_id="root",
            ingress_request_id="0123456789abcdef",
        )
        self.assertEqual(
            observation.snapshot()["correlation"],
            {
                "backend_run_id": "root",
                "ingress_request_id": "0123456789abcdef",
            },
        )
        observation.observe_produced_chunk("produced")
        observation.observe_relayed_chunk("relayed")
        observation.observe_downstream_chunk(b"sent")
        counts = observation.snapshot()["stream_counts"]
        self.assertEqual(counts["produced_chunks"], 1)
        self.assertEqual(counts["relayed_chunks"], 1)
        self.assertEqual(counts["downstream_chunks"], 1)
        self.assertEqual(
            chat_router._local_abort_source(
                asyncio.CancelledError(),
                observation,
            ),
            "asyncio_cancelled_unknown",
        )

        observed_disconnect = chat_router._StreamObservation(run_id="root")
        observed_disconnect.observe_http_disconnect()
        self.assertEqual(
            chat_router._local_abort_source(
                asyncio.CancelledError(),
                observed_disconnect,
            ),
            "client_disconnected",
        )
        self.assertTrue(
            observed_disconnect.snapshot()["connection_state"][
                "http_disconnect_observed"
            ]
        )

        wrapped_send_failure = chat_router._StreamObservation(run_id="root")
        wrapped_send_failure.observe_client_disconnect_exception(
            exception_class="ClientDisconnect",
        )
        self.assertEqual(
            wrapped_send_failure.snapshot()["connection_state"]["downstream"],
            "downstream_send_failed",
        )
        self.assertFalse(
            wrapped_send_failure.snapshot()["connection_state"][
                "http_disconnect_observed"
            ]
        )

        shutdown = chat_router._StreamObservation(run_id="root")
        chat_router.set_service_shutdown_started(True)
        try:
            self.assertEqual(
                chat_router._local_abort_source(
                    asyncio.CancelledError(),
                    shutdown,
                ),
                "service_shutdown",
            )
        finally:
            chat_router.set_service_shutdown_started(False)


class ChatBackgroundShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_resistant_best_effort_task_cannot_block_shutdown(self):
        release = asyncio.Event()
        started = asyncio.Event()

        async def cancel_resistant():
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        task = asyncio.create_task(cancel_resistant())
        chat_router._best_effort_tasks.add(task)
        try:
            await started.wait()
            await asyncio.wait_for(
                chat_router.shutdown_chat_background_tasks(
                    producer_cancel_seconds=0.01,
                    projection_grace_seconds=0.01,
                    best_effort_cancel_seconds=0.01,
                ),
                timeout=0.5,
            )
            self.assertFalse(task.done())
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=0.5)
            chat_router._best_effort_tasks.discard(task)


if __name__ == "__main__":
    unittest.main()
