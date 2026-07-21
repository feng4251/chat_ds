import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import AgentRun, Base, Conversation, Message, User
from routers import chat_router
from schemas import ChatRequest


def _completed_event(run_id: str) -> dict:
    return {
        "type": "agent_event",
        "event_type": "run.completed",
        "run_id": run_id,
        "root_run_id": run_id,
        "parent_run_id": None,
        "agent_kind": "primary",
        "agent_name": "primary",
        "depth": 0,
        "workspace_scope": "shared_session",
        "seq": 1,
        "payload": {
            "finish_reason": "stop",
            "terminal_reason": "stop",
            "usage": {
                "input_tokens": 4,
                "output_tokens": 2,
                "total_tokens": 6,
            },
        },
    }


class ChatTurnIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "turns.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add(User(
                id="user",
                username="turn-user",
                hashed_password="hash",
            ))
            session.add(Conversation(
                id="conversation",
                user_id="user",
                model_id="deepseek_v4_pro",
            ))
            await session.commit()

    async def asyncTearDown(self):
        pending = [
            task
            for state in chat_router._conversation_turn_states.values()
            for task in state.projection_tasks
            if not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        chat_router._conversation_turn_states.clear()
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_adjacent_turn_waits_for_durable_assistant_before_history(self):
        requests: list[dict] = []
        projection_started = asyncio.Event()
        release_projection = asyncio.Event()
        original_persist = chat_router._persist_after_stream

        class FakeHarnessResponse:
            status_code = 200

            def __init__(self, request_json: dict):
                self.request_json = request_json

            async def aiter_lines(self):
                last = self.request_json["messages"][-1]["content"]
                answer = "assistant-one" if last == "user-one" else "assistant-two"
                yield "data: " + json.dumps({
                    "choices": [{
                        "delta": {"content": answer},
                        "index": 0,
                    }],
                })
                run_id = self.request_json["run_metadata"]["run_id"]
                yield "data: " + json.dumps({
                    "choices": [{
                        "delta": {"agent_event": _completed_event(run_id)},
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
                return False

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, **kwargs):
                requests.append(kwargs["json"])
                return FakeStreamContext(kwargs["json"])

        async def delayed_persist(*args, **kwargs):
            projection_started.set()
            await release_projection.wait()
            return await original_persist(*args, **kwargs)

        async def consume(response):
            return [chunk async for chunk in response.body_iterator]

        common_patches = (
            patch.object(chat_router, "async_session", self.sessions),
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
            patch.object(chat_router, "_generate_title", new=AsyncMock()),
            patch.object(chat_router.httpx, "AsyncClient", FakeClient),
            patch.object(
                chat_router.settings,
                "agent_event_immediate_persist",
                False,
            ),
            patch.object(
                chat_router,
                "_persist_after_stream",
                side_effect=delayed_persist,
            ),
        )

        with common_patches[0], common_patches[1], common_patches[2], \
                common_patches[3], common_patches[4], common_patches[5], \
                common_patches[6], common_patches[7]:
            async with self.sessions() as first_session:
                user = await first_session.get(User, "user")
                first_response = await chat_router._chat_stream(
                    ChatRequest(
                        conversation_id="conversation",
                        content="user-one",
                        model_id="deepseek_v4_pro",
                    ),
                    user,
                    first_session,
                )
                first_consumer = asyncio.create_task(consume(first_response))
                await projection_started.wait()
                self.assertFalse(first_consumer.done())

                async with self.sessions() as second_session:
                    second_user = await second_session.get(User, "user")
                    second_builder = asyncio.create_task(chat_router._chat_stream(
                        ChatRequest(
                            conversation_id="conversation",
                            content="user-two",
                            model_id="deepseek_v4_pro",
                        ),
                        second_user,
                        second_session,
                    ))
                    await asyncio.sleep(0)
                    self.assertFalse(second_builder.done())

                    release_projection.set()
                    await first_consumer
                    second_response = await second_builder
                    await consume(second_response)

        self.assertEqual(len(requests), 2)
        second_messages = requests[1]["messages"]
        self.assertEqual(
            [(item["role"], item["content"]) for item in second_messages],
            [
                ("system", "You are a helpful AI assistant."),
                ("user", "user-one"),
                ("assistant", "assistant-one"),
                ("user", "user-two"),
            ],
        )
        async with self.sessions() as session:
            messages = (await session.execute(
                select(Message).where(
                    Message.conversation_id == "conversation"
                ).order_by(Message.created_at, Message.id)
            )).scalars().all()
        self.assertEqual(
            [(message.role, message.content) for message in messages],
            [
                ("user", "user-one"),
                ("assistant", "assistant-one"),
                ("user", "user-two"),
                ("assistant", "assistant-two"),
            ],
        )
        self.assertTrue(all(
            left.created_at < right.created_at
            for left, right in zip(messages, messages[1:])
        ))
        self.assertNotIn("conversation", chat_router._conversation_turn_states)

    async def test_disconnected_projection_is_drained_without_blocking_other_session(self):
        projection_started = asyncio.Event()
        release_projection = asyncio.Event()

        async def projection():
            projection_started.set()
            await release_projection.wait()
            return True

        first_lease = await chat_router._acquire_conversation_turn("conversation")
        projection_task = chat_router._track_conversation_projection(
            "conversation",
            projection(),
        )
        _ = projection_task
        _ = await projection_started.wait()
        chat_router._release_conversation_turn("conversation", first_lease)

        next_turn = asyncio.create_task(
            chat_router._acquire_conversation_turn("conversation")
        )
        await asyncio.sleep(0)
        self.assertFalse(next_turn.done())

        unrelated = await asyncio.wait_for(
            chat_router._acquire_conversation_turn("other-conversation"),
            timeout=0.2,
        )
        chat_router._release_conversation_turn("other-conversation", unrelated)

        release_projection.set()
        next_lease = await asyncio.wait_for(next_turn, timeout=1)
        chat_router._release_conversation_turn("conversation", next_lease)
        await projection_task
        await asyncio.sleep(0)
        self.assertNotIn("conversation", chat_router._conversation_turn_states)
        self.assertNotIn("other-conversation", chat_router._conversation_turn_states)

    async def test_failed_disconnected_projection_does_not_open_next_turn(self):
        fail_projection = asyncio.Event()

        async def projection():
            await fail_projection.wait()
            raise RuntimeError("database projection failed")

        first_lease = await chat_router._acquire_conversation_turn("conversation")
        projection_task = chat_router._track_conversation_projection(
            "conversation",
            projection(),
        )
        chat_router._release_conversation_turn("conversation", first_lease)
        next_turn = asyncio.create_task(
            chat_router._acquire_conversation_turn("conversation")
        )
        await asyncio.sleep(0)
        fail_projection.set()
        with self.assertRaises(chat_router._ConversationProjectionBarrierError):
            await next_turn
        with self.assertRaises(RuntimeError):
            await projection_task
        await asyncio.sleep(0)
        self.assertNotIn("conversation", chat_router._conversation_turn_states)

    async def test_durable_orphan_marker_blocks_later_turn_after_registry_cleanup(self):
        timestamp = datetime.utcnow()
        async with self.sessions() as session:
            session.add(Message(
                conversation_id="conversation",
                role="user",
                content="unprojected user turn",
                source="chat",
                created_at=timestamp,
            ))
            session.add(AgentRun(
                id="orphaned-run",
                user_id="user",
                conversation_id="conversation",
                root_run_id="orphaned-run",
                source="chat",
                requested_model_id="deepseek_v4_pro",
                resolved_model_id="deepseek_v4_pro",
                status="succeeded",
                agent_kind="primary",
                parent_run_id=None,
                started_at=timestamp,
                ended_at=timestamp,
            ))
            await session.commit()
            with self.assertRaises(HTTPException) as raised:
                await chat_router._assert_no_unprojected_primary_turn(
                    session,
                    "conversation",
                )
        self.assertEqual(raised.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
