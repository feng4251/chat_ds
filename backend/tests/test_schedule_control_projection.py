import json
import unittest
from datetime import datetime

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import Base, Conversation, ScheduledJob, User
from scheduler import stage_schedule_control_writes


class ScheduleControlProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def _foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.user_id = "a" * 32
        self.conversation_id = "b" * 32
        async with self.sessions() as db:
            db.add(User(
                id=self.user_id,
                username="schedule-holdout",
                hashed_password="unused",
            ))
            db.add(Conversation(
                id=self.conversation_id,
                user_id=self.user_id,
                model_id="renamed-model",
                engine_id="claude_code",
            ))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    @staticmethod
    def _write():
        return {
            "schema": "chatds.schedule-write.v1",
            "operation": "create",
            "tool_call_id": "generic-tool-receipt-1",
            "request": {
                "name": "Two factory sensors",
                "prompt": "Read and report both selected sensor values.",
                "schedule": "every 10m",
                "timezone": "Asia/Shanghai",
                "max_runs": 12,
                "expires_at": "2099-08-12T15:00:00+08:00",
                "enabled_tools": ["web_search"],
                "delete_after_run": False,
            },
        }

    async def test_root_terminal_write_is_bound_bounded_and_idempotent(self):
        async with self.sessions() as db:
            first = await stage_schedule_control_writes(
                db,
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                root_run_id="c" * 32,
                model_id="renamed-model",
                writes=[self._write()],
            )
            await db.commit()
            second = await stage_schedule_control_writes(
                db,
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                root_run_id="c" * 32,
                model_id="renamed-model",
                writes=[self._write()],
            )
            await db.commit()
            count = (await db.execute(
                select(func.count()).select_from(ScheduledJob)
            )).scalar_one()
            job = await db.get(ScheduledJob, first[0])

        self.assertEqual(first, second)
        self.assertEqual(count, 1)
        self.assertEqual(job.user_id, self.user_id)
        self.assertEqual(job.conversation_id, self.conversation_id)
        self.assertEqual(job.max_runs, 12)
        self.assertEqual(job.run_count, 0)
        self.assertLessEqual(job.next_run_at, job.expires_at)
        self.assertIsInstance(job.expires_at, datetime)

    async def test_visible_tool_aliases_compile_to_exact_session_subset(self):
        write = self._write()
        write["request"]["enabled_tools"] = [
            "Bash",
            "mcp__chatds-market-data__market_quote",
        ]
        async with self.sessions() as db:
            created = await stage_schedule_control_writes(
                db,
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                root_run_id="d" * 32,
                model_id="renamed-model",
                writes=[write],
                allowed_tools=frozenset({"market_quote", "cronjob"}),
            )
            await db.commit()
            job = await db.get(ScheduledJob, created[0])
        self.assertEqual(json.loads(job.enabled_tools), ["market_quote"])

    async def test_foreign_alias_and_authority_widening_fail_closed(self):
        foreign = self._write()
        foreign["request"]["enabled_tools"] = [
            "mcp__renamed-foreign__market_quote"
        ]
        async with self.sessions() as db:
            with self.assertRaisesRegex(ValueError, "unknown"):
                await stage_schedule_control_writes(
                    db,
                    user_id=self.user_id,
                    conversation_id=self.conversation_id,
                    root_run_id="e" * 32,
                    model_id="renamed-model",
                    writes=[foreign],
                    allowed_tools=frozenset({"market_quote"}),
                )
        widened = self._write()
        widened["request"]["enabled_tools"] = ["web_search"]
        async with self.sessions() as db:
            with self.assertRaisesRegex(ValueError, "unauthorized"):
                await stage_schedule_control_writes(
                    db,
                    user_id=self.user_id,
                    conversation_id=self.conversation_id,
                    root_run_id="f" * 32,
                    model_id="renamed-model",
                    writes=[widened],
                    allowed_tools=frozenset({"market_quote"}),
                )

    async def test_explicit_empty_tools_remain_empty_in_durable_job(self):
        write = self._write()
        write["request"]["enabled_tools"] = []
        async with self.sessions() as db:
            created = await stage_schedule_control_writes(
                db,
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                root_run_id="1" * 32,
                model_id="renamed-model",
                writes=[write],
                allowed_tools=frozenset({"market_quote"}),
            )
            await db.commit()
            job = await db.get(ScheduledJob, created[0])
        self.assertEqual(job.enabled_tools, "[]")


if __name__ == "__main__":
    unittest.main()
