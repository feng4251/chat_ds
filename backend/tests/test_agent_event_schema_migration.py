import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from database import (
    _AGENT_RUN_EVENT_IDENTITY_COLUMNS,
    _AGENT_RUN_EVENT_IDENTITY_INDEX,
    _ensure_agent_run_event_identity,
)
from models import AgentRunEvent, Base


class AgentEventSchemaContractTests(unittest.IsolatedAsyncioTestCase):
    def test_model_uses_full_tool_name_width_and_unique_event_identity(self):
        self.assertEqual(512, AgentRunEvent.__table__.c.tool_name.type.length)
        identity_index = next(
            index
            for index in AgentRunEvent.__table__.indexes
            if index.name == _AGENT_RUN_EVENT_IDENTITY_INDEX
        )
        self.assertTrue(identity_index.unique)
        self.assertEqual(
            _AGENT_RUN_EVENT_IDENTITY_COLUMNS,
            tuple(column.name for column in identity_index.columns),
        )

    async def test_new_database_rejects_duplicate_event_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(temp_dir) / 'new.db'}"
            )
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                    table_info = (
                        await connection.execute(
                            text("PRAGMA table_info('agent_run_events')")
                        )
                    ).mappings().all()
                    tool_type = next(
                        row["type"]
                        for row in table_info
                        if row["name"] == "tool_name"
                    )
                    self.assertEqual("VARCHAR(512)", tool_type)

                insert_sql = text(
                    "INSERT INTO agent_run_events "
                    "(id, conversation_id, run_id, user_id, seq, "
                    "event_type, payload, tool_name) "
                    "VALUES (:id, 'conversation', 'run', 'user', 7, "
                    "'tool.completed', '{}', :tool_name)"
                )
                async with engine.begin() as connection:
                    await connection.execute(
                        insert_sql,
                        {
                            "id": "first",
                            "tool_name": "m" * 512,
                        },
                    )
                with self.assertRaises(IntegrityError):
                    async with engine.begin() as connection:
                        await connection.execute(
                            insert_sql,
                            {
                                "id": "duplicate",
                                "tool_name": "other",
                            },
                        )
            finally:
                await engine.dispose()

    async def test_legacy_migration_deduplicates_first_wins_then_is_idempotent(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(temp_dir) / 'legacy.db'}"
            )
            try:
                async with engine.begin() as connection:
                    await connection.execute(text(
                        "CREATE TABLE agent_run_events ("
                        "id VARCHAR(32) PRIMARY KEY, "
                        "run_id VARCHAR(32) NOT NULL, "
                        "conversation_id VARCHAR(32) NOT NULL, "
                        "user_id VARCHAR(32) NOT NULL, "
                        "parent_run_id VARCHAR(32), "
                        "seq INTEGER NOT NULL DEFAULT 0, "
                        "event_type VARCHAR(64) NOT NULL, "
                        "payload TEXT, "
                        "tool_name VARCHAR(128), "
                        "tool_call_id VARCHAR(128), "
                        "event_time DATETIME DEFAULT CURRENT_TIMESTAMP)"
                    ))
                    # Lexical ID order intentionally disagrees with insertion
                    # order so the assertion proves stable first-inserted wins.
                    await connection.execute(text(
                        "INSERT INTO agent_run_events "
                        "(id, conversation_id, run_id, user_id, seq, "
                        "event_type, payload) VALUES "
                        "('z-first', 'conversation', 'run', 'user', 3, "
                        "'run.completed', '{\"winner\":\"first\"}'), "
                        "('a-second', 'conversation', 'run', 'user', 3, "
                        "'run.completed', '{\"winner\":\"second\"}'), "
                        "('independent', 'conversation', 'run', 'user', 4, "
                        "'run.completed', '{\"winner\":\"independent\"}')"
                    ))
                    # Mirror init_db ordering: metadata creation must leave an
                    # already-existing legacy table available for cleanup.
                    await connection.run_sync(Base.metadata.create_all)
                    await _ensure_agent_run_event_identity(connection)
                    # A second startup must be a no-op.
                    await _ensure_agent_run_event_identity(connection)

                    rows = (
                        await connection.execute(text(
                            "SELECT id, payload FROM agent_run_events "
                            "ORDER BY seq"
                        ))
                    ).all()
                    self.assertEqual(
                        [
                            ("z-first", '{"winner":"first"}'),
                            ("independent", '{"winner":"independent"}'),
                        ],
                        rows,
                    )
                    index_rows = (
                        await connection.execute(text(
                            "PRAGMA index_list('agent_run_events')"
                        ))
                    ).mappings().all()
                    identity_index = next(
                        row
                        for row in index_rows
                        if row["name"] == _AGENT_RUN_EVENT_IDENTITY_INDEX
                    )
                    self.assertEqual(1, identity_index["unique"])

                with self.assertRaises(IntegrityError):
                    async with engine.begin() as connection:
                        await connection.execute(text(
                            "INSERT INTO agent_run_events "
                            "(id, conversation_id, run_id, user_id, seq, "
                            "event_type, payload) VALUES "
                            "('late', 'conversation', 'run', 'user', 3, "
                            "'run.completed', '{\"winner\":\"late\"}')"
                        ))
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
