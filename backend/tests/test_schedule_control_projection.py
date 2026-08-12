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


if __name__ == "__main__":
    unittest.main()
