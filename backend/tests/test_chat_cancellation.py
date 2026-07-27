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

    async def test_explicit_sse_close_closes_harness_stream_and_marks_root_cancelled(self):
        harness_stream_closed = False
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
                await asyncio.Event().wait()

        class FakeStreamContext:
            def __init__(self, request_json: dict):
                self.response = FakeHarnessResponse(request_json)

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, exc_type, exc, traceback):
                nonlocal harness_stream_closed
                harness_stream_closed = True

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
                patch.object(chat_router, "ensure_workspace"),
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

        self.assertTrue(harness_stream_closed)
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
                "debug.backend_stream.terminated",
                "run.cancelled",
            ],
        )
        self.assertEqual(persisted["finish_reason"], "task_cancelled")
        self.assertIn("响应生成器", persisted["error_message"])
        self.assertIn("partial draft", persisted["content"])
        self.assertIn("不完整草稿", persisted["content"])
        debug_payload = root_events[1]["payload"]
        cancel_payload = root_events[2]["payload"]
        self.assertEqual(
            debug_payload["termination_source"],
            "generator_closed",
        )
        self.assertEqual(
            cancel_payload["cancellation_source"],
            "generator_closed",
        )
        self.assertEqual(cancel_payload["exception_class"], "GeneratorExit")
        self.assertEqual(cancel_payload["last_root_event_type"], "run.started")
        self.assertEqual(cancel_payload["root_phase"], "executing")
        self.assertGreaterEqual(
            cancel_payload["stream_counts"]["downstream_chunks"],
            3,
        )
        self.assertEqual(
            cancel_payload["unsatisfied_contract"]["status"],
            "not_reported_by_harness",
        )
        self.assertEqual(
            cancel_payload["connection_state"]["downstream"],
            "generator_closed",
        )

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


if __name__ == "__main__":
    unittest.main()
