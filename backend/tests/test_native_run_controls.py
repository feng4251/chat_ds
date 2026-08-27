import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent_engines.base import AgentEngineError
from models import AgentRun, AgentRunEvent, Base, Conversation, Message, User
from routers import workspace_router
from schemas import NativeRunControl


class NativeRunControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        path = Path(self.temporary.name) / "native-controls.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.user = SimpleNamespace(id="1" * 32)
        self.conversation_id = "2" * 32
        self.run_id = "3" * 32
        self.started = datetime(2026, 8, 27, 8, 0, 0, 100)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add_all([
                User(
                    id=self.user.id,
                    username="native-control-owner",
                    hashed_password="fixture",
                ),
                Conversation(
                    id=self.conversation_id,
                    user_id=self.user.id,
                    engine_id="claude_code",
                    model_id="generic-model",
                ),
                Message(
                    id="4" * 32,
                    conversation_id=self.conversation_id,
                    role="user",
                    content="Inspect the municipal archive.",
                    source="chat",
                    created_at=self.started,
                ),
                AgentRun(
                    id=self.run_id,
                    root_run_id=self.run_id,
                    user_id=self.user.id,
                    conversation_id=self.conversation_id,
                    engine_id="claude_code",
                    requested_model_id="generic-model",
                    resolved_model_id="generic-model",
                    status="running",
                    source="chat",
                    started_at=self.started,
                ),
            ])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temporary.cleanup()

    async def _conversation(self, db):
        return (await db.execute(select(Conversation).where(
            Conversation.id == self.conversation_id
        ))).scalar_one()

    async def test_followup_is_durable_idempotent_and_keeps_turn_mapping(self):
        engine = SimpleNamespace(control_run=AsyncMock(return_value={
            "schema": "chatds.native-run-control-receipt.v1",
            "accepted": True,
            "idempotent": False,
            "control_id": "5" * 32,
            "seq": 1,
            "action": "followup",
            "status": "delivered",
            "code": None,
        }))
        registry = SimpleNamespace(get=lambda _engine_id: engine)
        payload = NativeRunControl(
            control_id="5" * 32,
            action="followup",
            text="Also compare the renamed library catalog.",
        )
        async with self.sessions() as db:
            conv = await self._conversation(db)
            with (
                patch.object(
                    workspace_router,
                    "_conversation",
                    AsyncMock(return_value=conv),
                ),
                patch(
                    "agent_engines.registry.build_agent_engine_registry",
                    return_value=registry,
                ),
            ):
                first = await workspace_router.control_native_run(
                    self.conversation_id,
                    self.run_id,
                    payload,
                    user=self.user,
                    db=db,
                )
                second = await workspace_router.control_native_run(
                    self.conversation_id,
                    self.run_id,
                    payload,
                    user=self.user,
                    db=db,
                )
                followups = (await db.execute(select(Message).where(
                    Message.conversation_id == self.conversation_id,
                    Message.source == "native_control",
                ))).scalars().all()
                self.assertEqual(len(followups), 1)
                self.assertEqual(first["message_id"], followups[0].id)
                self.assertTrue(second["idempotent"])
                self.assertEqual(engine.control_run.await_count, 1)

                db.add(Message(
                    id="6" * 32,
                    conversation_id=self.conversation_id,
                    role="assistant",
                    content="Archive comparison complete.",
                    source="chat",
                    run_id=self.run_id,
                    created_at=self.started + timedelta(seconds=2),
                ))
                await db.commit()
                cards = await workspace_router.list_run_cards(
                    self.conversation_id,
                    root_limit=20,
                    user=self.user,
                    db=db,
                )
        root = cards["roots"][0]
        self.assertEqual(root["mapping_status"], "exact")
        self.assertEqual(root["assistant_message_id"], "6" * 32)
        self.assertEqual(root["controls"][0]["status"], "delivered")
        self.assertEqual(root["controls"][0]["action"], "followup")

    async def test_uncertain_transport_keeps_request_pending_for_exact_retry(self):
        engine = SimpleNamespace(control_run=AsyncMock(side_effect=[
            AgentEngineError(
                "transport unavailable",
                code="native_control_transport_error",
                retryable=True,
            ),
            {
                "schema": "chatds.native-run-control-receipt.v1",
                "accepted": True,
                "idempotent": True,
                "control_id": "7" * 32,
                "seq": 1,
                "action": "steer",
                "status": "delivered",
                "code": None,
            },
        ]))
        registry = SimpleNamespace(get=lambda _engine_id: engine)
        payload = NativeRunControl(
            control_id="7" * 32,
            action="steer",
            text="Prioritize the newest ferry manifest.",
        )
        async with self.sessions() as db:
            conv = await self._conversation(db)
            with (
                patch.object(
                    workspace_router,
                    "_conversation",
                    AsyncMock(return_value=conv),
                ),
                patch(
                    "agent_engines.registry.build_agent_engine_registry",
                    return_value=registry,
                ),
            ):
                with self.assertRaises(HTTPException) as raised:
                    await workspace_router.control_native_run(
                        self.conversation_id,
                        self.run_id,
                        payload,
                        user=self.user,
                        db=db,
                    )
                self.assertEqual(raised.exception.status_code, 503)
                events = (await db.execute(select(AgentRunEvent).where(
                    AgentRunEvent.run_id == self.run_id,
                    AgentRunEvent.tool_call_id == payload.control_id,
                ))).scalars().all()
                self.assertEqual(
                    [event.event_type for event in events],
                    ["native.control.requested"],
                )

                recovered = await workspace_router.control_native_run(
                    self.conversation_id,
                    self.run_id,
                    payload,
                    user=self.user,
                    db=db,
                )
                self.assertEqual(recovered["status"], "delivered")
                events = (await db.execute(select(AgentRunEvent).where(
                    AgentRunEvent.run_id == self.run_id,
                    AgentRunEvent.tool_call_id == payload.control_id,
                ))).scalars().all()
                self.assertEqual(
                    {event.event_type for event in events},
                    {"native.control.requested", "native.control.delivered"},
                )

    async def test_control_receipt_survives_global_event_window_crowding(self):
        control_id = "8" * 32
        async with self.sessions() as db:
            db.add_all([
                AgentRunEvent(
                    run_id=self.run_id,
                    conversation_id=self.conversation_id,
                    user_id=self.user.id,
                    parent_run_id=None,
                    seq=1,
                    event_type="native.control.requested",
                    payload=json.dumps({
                        "action": "followup",
                        "message_id": "9" * 32,
                    }),
                    tool_name="native_runtime_control",
                    tool_call_id=control_id,
                    event_time=self.started + timedelta(seconds=1),
                ),
                AgentRunEvent(
                    run_id=self.run_id,
                    conversation_id=self.conversation_id,
                    user_id=self.user.id,
                    parent_run_id=None,
                    seq=1,
                    event_type="native.control.delivered",
                    payload=json.dumps({"action": "followup"}),
                    tool_name="native_runtime_control",
                    tool_call_id=control_id,
                    event_time=self.started + timedelta(seconds=2),
                ),
            ])
            for index in range(24):
                db.add(AgentRunEvent(
                    run_id=self.run_id,
                    conversation_id=self.conversation_id,
                    user_id=self.user.id,
                    parent_run_id=None,
                    seq=index + 1,
                    event_type="tool.completed",
                    payload="{}",
                    tool_name="catalog_lookup",
                    tool_call_id=f"lookup-{index}",
                    event_time=self.started + timedelta(seconds=10 + index),
                ))
            await db.commit()
            with patch.object(workspace_router, "_RUN_CARD_MAX_EVENTS", 10):
                cards = await workspace_router.list_run_cards(
                    self.conversation_id,
                    root_limit=20,
                    user=self.user,
                    db=db,
                )
        root = cards["roots"][0]
        self.assertEqual(root["controls"], [{
            "control_id": control_id,
            "seq": 1,
            "action": "followup",
            "message_id": "9" * 32,
            "status": "delivered",
            "code": None,
            "event_time": str(self.started + timedelta(seconds=1)),
        }])

    async def test_definitive_native_rejection_is_persisted(self):
        engine = SimpleNamespace(control_run=AsyncMock(return_value={
            "schema": "chatds.native-run-control-adapter-rejection.v1",
            "accepted": False,
            "idempotent": False,
            "control_id": "a" * 32,
            "action": "interrupt",
            "status": "rejected",
            "code": "renamed_native_control_rejected",
        }))
        registry = SimpleNamespace(get=lambda _engine_id: engine)
        payload = NativeRunControl(
            control_id="a" * 32,
            action="interrupt",
            text=None,
        )
        async with self.sessions() as db:
            conv = await self._conversation(db)
            with (
                patch.object(
                    workspace_router,
                    "_conversation",
                    AsyncMock(return_value=conv),
                ),
                patch(
                    "agent_engines.registry.build_agent_engine_registry",
                    return_value=registry,
                ),
            ):
                receipt = await workspace_router.control_native_run(
                    self.conversation_id,
                    self.run_id,
                    payload,
                    user=self.user,
                    db=db,
                )
            events = (await db.execute(select(AgentRunEvent).where(
                AgentRunEvent.run_id == self.run_id,
                AgentRunEvent.tool_call_id == payload.control_id,
            ))).scalars().all()
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(
            {event.event_type for event in events},
            {"native.control.requested", "native.control.rejected"},
        )
