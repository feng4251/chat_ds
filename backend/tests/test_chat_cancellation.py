import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import AgentRun, AgentRunEvent, Base, Conversation, TaskItem, User
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

        with patch.object(chat_router, "async_session", self.sessions):
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

    async def test_explicit_sse_close_closes_harness_stream_and_marks_root_cancelled(self):
        harness_stream_closed = False
        persisted_calls: list[dict] = []

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
                pass

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
                await iterator.aclose()

        self.assertTrue(harness_stream_closed)
        self.assertEqual(len(persisted_calls), 1)
        persisted = persisted_calls[0]
        root_run_id = persisted["run_id"]
        root_events = [
            event for event in persisted["agent_events"]
            if event.get("run_id") == root_run_id
        ]
        self.assertEqual(
            [event["event_type"] for event in root_events],
            ["run.started", "run.cancelled"],
        )
        self.assertEqual(persisted["finish_reason"], "task_cancelled")
        self.assertIsNone(persisted["error_message"])


if __name__ == "__main__":
    unittest.main()
