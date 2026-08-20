import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import AgentRun, Base, Conversation, TurnActivityEvent, User
from routers import workspace_router
from routers import chat_router
from schemas import ApprovalDecision, ConversationSettingsUpdate


class TurnActivityIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        path = Path(self.temporary.name) / "activity.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.owner = SimpleNamespace(id="1" * 32)
        self.other = SimpleNamespace(id="2" * 32)
        self.conversation_id = "3" * 32
        self.run_id = "4" * 32
        async with self.sessions() as db:
            db.add_all([
                User(id=self.owner.id, username="activity-owner", hashed_password="fixture"),
                User(id=self.other.id, username="activity-other", hashed_password="fixture"),
                Conversation(
                    id=self.conversation_id,
                    user_id=self.owner.id,
                    engine_id="claude_code",
                    model_id="shaiengine_glm_5_2",
                    permission_preset="workspace_write",
                ),
                AgentRun(
                    id=self.run_id,
                    root_run_id=self.run_id,
                    user_id=self.owner.id,
                    conversation_id=self.conversation_id,
                    engine_id="claude_code",
                    requested_model_id="shaiengine_glm_5_2",
                    status="running",
                ),
                TurnActivityEvent(
                    id="5" * 32,
                    user_id=self.owner.id,
                    conversation_id=self.conversation_id,
                    root_run_id=self.run_id,
                    run_id=self.run_id,
                    seq=1,
                    node_id="content:1",
                    kind="content",
                    operation="append",
                    payload=json.dumps({"text": "cross-domain"}),
                ),
            ])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temporary.cleanup()

    async def test_owner_replays_safe_projection(self):
        async with self.sessions() as db:
            result = await workspace_router.get_turn_activity_events(
                self.conversation_id,
                root_run_id=self.run_id,
                after=0,
                offset=0,
                limit=100,
                user=self.owner,
                db=db,
            )
        self.assertEqual(result["events"][0]["payload"]["text"], "cross-domain")

    async def test_other_user_cannot_read_or_infer_the_session(self):
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as raised:
                await workspace_router.get_turn_activity_events(
                    self.conversation_id,
                    root_run_id=self.run_id,
                    after=0,
                    offset=0,
                    limit=100,
                    user=self.other,
                    db=db,
                )
        self.assertEqual(raised.exception.status_code, 404)

    async def test_root_sequence_cursor_cannot_be_misapplied_globally(self):
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as raised:
                await workspace_router.get_turn_activity_events(
                    self.conversation_id,
                    root_run_id=None,
                    after=1,
                    offset=0,
                    limit=100,
                    user=self.owner,
                    db=db,
                )
        self.assertEqual(raised.exception.status_code, 400)

    async def test_fresh_session_defaults_to_confirmed_workspace_write(self):
        fresh_id = "9" * 32
        async with self.sessions() as db:
            fresh = Conversation(
                id=fresh_id,
                user_id=self.owner.id,
                engine_id="claude_code",
                model_id="shaiengine_glm_5_2",
            )
            db.add(fresh)
            await db.flush()
            self.assertEqual(fresh.permission_preset, "workspace_write")

    async def test_permission_change_is_rejected_during_an_active_turn(self):
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as raised:
                await workspace_router.update_conversation_settings(
                    self.conversation_id,
                    ConversationSettingsUpdate(permission_preset="read_only"),
                    user=self.owner,
                    db=db,
                )
        self.assertEqual(raised.exception.status_code, 409)

    async def test_activity_append_is_idempotent_and_conflicts_fail_closed(self):
        event = {
            "event_id": "6" * 32,
            "conversation_id": self.conversation_id,
            "root_run_id": self.run_id,
            "run_id": self.run_id,
            "seq": 2,
            "node_id": "content:2",
            "kind": "content",
            "operation": "append",
            "payload": {"text": "durable"},
        }
        with patch.object(chat_router, "async_session", self.sessions):
            await chat_router._persist_turn_activity_events(
                user_id=self.owner.id,
                conversation_id=self.conversation_id,
                root_run_id=self.run_id,
                events=[event],
            )
            await chat_router._persist_turn_activity_events(
                user_id=self.owner.id,
                conversation_id=self.conversation_id,
                root_run_id=self.run_id,
                events=[event],
            )
            with self.assertRaisesRegex(RuntimeError, "changed a durable sequence"):
                await chat_router._persist_turn_activity_events(
                    user_id=self.owner.id,
                    conversation_id=self.conversation_id,
                    root_run_id=self.run_id,
                    events=[{
                        **event,
                        "payload": {"text": "conflicting replay"},
                    }],
                )
        async with self.sessions() as db:
            rows = (await db.execute(
                select(TurnActivityEvent).where(
                    TurnActivityEvent.root_run_id == self.run_id,
                    TurnActivityEvent.seq == 2,
                )
            )).scalars().all()
        self.assertEqual(len(rows), 1)

    async def test_approval_is_owner_session_root_and_native_sequence_bound(self):
        request_id = "renamed-request-1"
        async with self.sessions() as db:
            db.add(TurnActivityEvent(
                id="7" * 32,
                user_id=self.owner.id,
                conversation_id=self.conversation_id,
                root_run_id=self.run_id,
                run_id=self.run_id,
                seq=2,
                node_id="approval:renamed",
                kind="approval",
                operation="merge",
                payload=json.dumps({
                    "request_id": request_id,
                    "request_seq": 17,
                    "status": "pending",
                    "tool_name": "RenamedMutation",
                }),
            ))
            await db.commit()

        class Engine:
            def __init__(self):
                self.calls = []

            async def decide_approval(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "accepted": True,
                    "idempotent": False,
                    "status": "allowed",
                }

        engine = Engine()
        registry = SimpleNamespace(get=lambda _engine_id: engine)
        with patch(
            "agent_engines.registry.build_agent_engine_registry",
            return_value=registry,
        ):
            async with self.sessions() as db:
                result = await workspace_router.decide_turn_approval(
                    self.conversation_id,
                    self.run_id,
                    request_id,
                    ApprovalDecision(decision="allow", request_seq=17),
                    user=self.owner,
                    db=db,
                )
        self.assertTrue(result["accepted"])
        self.assertEqual(engine.calls, [{
            "user_id": self.owner.id,
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "request_id": request_id,
            "request_seq": 17,
            "decision": "allow",
            "answers": None,
        }])

        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as stale:
                await workspace_router.decide_turn_approval(
                    self.conversation_id,
                    self.run_id,
                    request_id,
                    ApprovalDecision(decision="deny", request_seq=18),
                    user=self.owner,
                    db=db,
                )
        self.assertEqual(stale.exception.status_code, 409)

        async with self.sessions() as db:
            db.add(TurnActivityEvent(
                id="8" * 32,
                user_id=self.owner.id,
                conversation_id=self.conversation_id,
                root_run_id=self.run_id,
                run_id=self.run_id,
                seq=3,
                node_id="approval:renamed",
                kind="approval",
                operation="merge",
                payload=json.dumps({
                    "request_id": request_id,
                    "status": "allowed",
                    "tool_name": "RenamedMutation",
                }),
            ))
            await db.commit()
            replay = await workspace_router.decide_turn_approval(
                self.conversation_id,
                self.run_id,
                request_id,
                ApprovalDecision(decision="allow", request_seq=17),
                user=self.owner,
                db=db,
            )
        self.assertTrue(replay["idempotent"])
        self.assertEqual(len(engine.calls), 1)

    async def test_native_question_is_answerable_in_read_only_session(self):
        request_id = "museum-question-1"
        question_text = "Which museum wing should be audited?"
        async with self.sessions() as db:
            conversation = await db.get(Conversation, self.conversation_id)
            conversation.permission_preset = "read_only"
            db.add(TurnActivityEvent(
                id="a" * 32,
                user_id=self.owner.id,
                conversation_id=self.conversation_id,
                root_run_id=self.run_id,
                run_id=self.run_id,
                seq=4,
                node_id="approval:museum-question",
                kind="approval",
                operation="merge",
                payload=json.dumps({
                    "request_id": request_id,
                    "request_seq": 29,
                    "status": "pending",
                    "tool_name": "AskUserQuestion",
                    "interaction_kind": "question",
                    "questions": [{
                        "question": question_text,
                        "header": "Wing",
                        "multi_select": False,
                        "options": [
                            {"label": "East", "description": "East wing"},
                            {"label": "West", "description": "West wing"},
                        ],
                    }],
                }),
            ))
            await db.commit()

        class Engine:
            def __init__(self):
                self.calls = []

            async def decide_approval(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "accepted": True,
                    "idempotent": False,
                    "status": "allowed",
                }

        engine = Engine()
        with patch(
            "agent_engines.registry.build_agent_engine_registry",
            return_value=SimpleNamespace(get=lambda _engine_id: engine),
        ):
            async with self.sessions() as db:
                result = await workspace_router.decide_turn_approval(
                    self.conversation_id,
                    self.run_id,
                    request_id,
                    ApprovalDecision(
                        decision="allow",
                        request_seq=29,
                        answers={question_text: "East"},
                    ),
                    user=self.owner,
                    db=db,
                )
        self.assertTrue(result["accepted"])
        self.assertEqual(engine.calls[0]["answers"], {question_text: "East"})

        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as missing:
                await workspace_router.decide_turn_approval(
                    self.conversation_id,
                    self.run_id,
                    request_id,
                    ApprovalDecision(decision="allow", request_seq=29),
                    user=self.owner,
                    db=db,
                )
        self.assertEqual(missing.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
