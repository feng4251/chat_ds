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
    _reconcile_orphaned_descendant_runs,
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

    async def test_startup_reconciles_active_children_of_terminal_roots_once(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(temp_dir) / 'orphan.db'}"
            )
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                    await connection.execute(text(
                        "INSERT INTO agent_runs "
                        "(id, user_id, conversation_id, parent_run_id, "
                        "root_run_id, agent_kind, depth, workspace_scope, "
                        "source, requested_model_id, status, input_tokens, "
                        "output_tokens, total_tokens) "
                        "VALUES "
                        "('root', 'user', 'conversation', NULL, 'root', "
                        "'primary', 0, 'shared_session', 'chat', 'model', "
                        "'cancelled', 0, 0, 0), "
                        "('child', 'user', 'conversation', 'root', 'root', "
                        "'delegate', 1, 'shared_session', 'chat', 'model', "
                        "'running', 0, 0, 0)"
                    ))
                    await connection.execute(text(
                        "INSERT INTO agent_run_events "
                        "(id, conversation_id, run_id, user_id, "
                        "parent_run_id, seq, event_type, payload) VALUES "
                        "('existing', 'conversation', 'child', 'user', "
                        "'root', 7, 'tool.completed', '{}')"
                    ))
                    await connection.execute(text(
                        "INSERT INTO task_items "
                        "(id, user_id, conversation_id, run_id, root_run_id, "
                        "parent_run_id, task_key, kind, status) VALUES "
                        "('task', 'user', 'conversation', 'child', 'root', "
                        "'root', 'child-task', 'run', 'running')"
                    ))

                    await _reconcile_orphaned_descendant_runs(connection)
                    # A second startup must not append another terminal event.
                    await _reconcile_orphaned_descendant_runs(connection)

                    child = (
                        await connection.execute(text(
                            "SELECT status, finish_reason, ended_at "
                            "FROM agent_runs WHERE id = 'child'"
                        ))
                    ).one()
                    self.assertEqual("cancelled", child.status)
                    self.assertEqual(
                        "parent_run_terminal_reconciliation",
                        child.finish_reason,
                    )
                    self.assertIsNotNone(child.ended_at)

                    task = (
                        await connection.execute(text(
                            "SELECT status, summary, error, ended_at "
                            "FROM task_items WHERE id = 'task'"
                        ))
                    ).one()
                    self.assertEqual("cancelled", task.status)
                    self.assertEqual(
                        "parent_run_terminal_reconciliation",
                        task.summary,
                    )
                    self.assertIsNone(task.error)
                    self.assertIsNotNone(task.ended_at)

                    events = (
                        await connection.execute(text(
                            "SELECT seq, event_type, payload "
                            "FROM agent_run_events "
                            "WHERE run_id = 'child' ORDER BY seq"
                        ))
                    ).all()
                    self.assertEqual(2, len(events))
                    self.assertEqual(8, events[-1].seq)
                    self.assertEqual("run.cancelled", events[-1].event_type)
                    self.assertIn(
                        '"cancellation_source":'
                        '"root_run_terminal_startup_repair"',
                        events[-1].payload,
                    )
                    self.assertIn(
                        '"parent_terminal_status":"cancelled"',
                        events[-1].payload,
                    )
            finally:
                await engine.dispose()

    async def test_startup_cancels_stale_active_root_before_its_children(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(temp_dir) / 'restart.db'}"
            )
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                    await connection.execute(text(
                        "INSERT INTO agent_runs "
                        "(id, user_id, conversation_id, parent_run_id, "
                        "root_run_id, agent_kind, depth, workspace_scope, "
                        "source, requested_model_id, status, input_tokens, "
                        "output_tokens, total_tokens) VALUES "
                        "('root', 'user', 'conversation', NULL, 'root', "
                        "'primary', 0, 'shared_session', 'chat', 'model', "
                        "'running', 0, 0, 0), "
                        "('child', 'user', 'conversation', 'root', 'root', "
                        "'delegate', 1, 'shared_session', 'delegate', "
                        "'model', 'planned', 0, 0, 0)"
                    ))
                    await connection.execute(text(
                        "INSERT INTO task_items "
                        "(id, user_id, conversation_id, run_id, root_run_id, "
                        "parent_run_id, task_key, kind, status) VALUES "
                        "('root-task', 'user', 'conversation', 'root', "
                        "'root', NULL, 'root-task-key', 'primary', "
                        "'committing'), "
                        "('child-task', 'user', 'conversation', 'child', "
                        "'root', 'root', 'child-task-key', 'delegate', "
                        "'queued')"
                    ))

                    await _reconcile_orphaned_descendant_runs(connection)
                    await _reconcile_orphaned_descendant_runs(connection)

                    runs = (
                        await connection.execute(text(
                            "SELECT id, status, finish_reason "
                            "FROM agent_runs ORDER BY id"
                        ))
                    ).all()
                    self.assertEqual(
                        [
                            (
                                "child",
                                "cancelled",
                                "parent_run_terminal_reconciliation",
                            ),
                            (
                                "root",
                                "cancelled",
                                "backend_process_restart",
                            ),
                        ],
                        runs,
                    )
                    events = (
                        await connection.execute(text(
                            "SELECT run_id, payload FROM agent_run_events "
                            "ORDER BY run_id"
                        ))
                    ).all()
                    self.assertEqual(2, len(events))
                    self.assertIn(
                        '"cancellation_source":'
                        '"root_run_terminal_startup_repair"',
                        events[0].payload,
                    )
                    self.assertIn(
                        '"parent_terminal_status":"cancelled"',
                        events[0].payload,
                    )
                    self.assertIn(
                        '"cancellation_source":'
                        '"backend_startup_orphan_repair"',
                        events[1].payload,
                    )
                    task_statuses = (
                        await connection.execute(text(
                            "SELECT status FROM task_items ORDER BY id"
                        ))
                    ).scalars().all()
                    self.assertEqual(
                        ["cancelled", "cancelled"],
                        task_statuses,
                    )
            finally:
                await engine.dispose()

    async def test_startup_fails_uncommitted_terminal_without_second_terminal(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(temp_dir) / 'committing.db'}"
            )
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                    await connection.execute(text(
                        "INSERT INTO users "
                        "(id, username, hashed_password, created_at) VALUES "
                        "('user', 'user', 'hash', CURRENT_TIMESTAMP)"
                    ))
                    await connection.execute(text(
                        "INSERT INTO conversations "
                        "(id, user_id, model_id, workspace_version, "
                        "goal_started_tokens, input_tokens, output_tokens, "
                        "total_tokens) VALUES "
                        "('conversation', 'user', 'model', 1, 0, 0, 0, 0)"
                    ))
                    await connection.execute(text(
                        "INSERT INTO messages "
                        "(id, conversation_id, role, content, model_id, "
                        "source, input_tokens, output_tokens, total_tokens) "
                        "VALUES ('user-message', 'conversation', 'user', "
                        "'request', 'model', 'chat', 0, 0, 0)"
                    ))
                    await connection.execute(text(
                        "INSERT INTO agent_runs "
                        "(id, user_id, conversation_id, parent_run_id, "
                        "root_run_id, agent_kind, depth, workspace_scope, "
                        "source, requested_model_id, resolved_model_id, "
                        "status, input_tokens, output_tokens, total_tokens) "
                        "VALUES "
                        "('root', 'user', 'conversation', NULL, 'root', "
                        "'primary', 0, 'shared_session', 'chat', 'model', "
                        "'model', 'committing', 0, 0, 0), "
                        "('child', 'user', 'conversation', 'root', 'root', "
                        "'delegate', 1, 'shared_session', 'delegate', "
                        "'model', 'model', 'running', 0, 0, 0)"
                    ))
                    await connection.execute(text(
                        "INSERT INTO task_items "
                        "(id, user_id, conversation_id, run_id, root_run_id, "
                        "parent_run_id, task_key, kind, status) VALUES "
                        "('root-task', 'user', 'conversation', 'root', "
                        "'root', NULL, 'root-task-key', 'primary', "
                        "'committing'), "
                        "('child-task', 'user', 'conversation', 'child', "
                        "'root', 'root', 'child-task-key', 'delegate', "
                        "'running')"
                    ))
                    await connection.execute(
                        text(
                            "INSERT INTO agent_run_events "
                            "(id, conversation_id, run_id, user_id, seq, "
                            "event_type, payload) VALUES "
                            "('terminal', 'conversation', 'root', 'user', 5, "
                            "'run.completed', :payload)"
                        ),
                        {
                            "payload": (
                                '{"authoritative":true,'
                                '"finish_reason":"stop"}'
                            ),
                        },
                    )

                    await _reconcile_orphaned_descendant_runs(connection)
                    await _reconcile_orphaned_descendant_runs(connection)

                    root = (
                        await connection.execute(text(
                            "SELECT status, finish_reason FROM agent_runs "
                            "WHERE id = 'root'"
                        ))
                    ).one()
                    self.assertEqual(
                        ("failed", "terminal_projection_interrupted"),
                        tuple(root),
                    )
                    child = (
                        await connection.execute(text(
                            "SELECT status, finish_reason FROM agent_runs "
                            "WHERE id = 'child'"
                        ))
                    ).one()
                    self.assertEqual(
                        (
                            "cancelled",
                            "parent_run_terminal_reconciliation",
                        ),
                        tuple(child),
                    )
                    terminal_types = (
                        await connection.execute(text(
                            "SELECT event_type FROM agent_run_events "
                            "WHERE run_id = 'root' "
                            "AND event_type IN "
                            "('run.completed', 'run.failed', "
                            "'run.cancelled') ORDER BY seq"
                        ))
                    ).scalars().all()
                    self.assertEqual(["run.completed"], terminal_types)
                    projected_roles = (
                        await connection.execute(text(
                            "SELECT role FROM messages "
                            "WHERE conversation_id = 'conversation' "
                            "ORDER BY created_at, id"
                        ))
                    ).scalars().all()
                    self.assertEqual(
                        ["user", "assistant"],
                        projected_roles,
                    )
                    projection_diagnostics = (
                        await connection.execute(text(
                            "SELECT event_type FROM agent_run_events "
                            "WHERE run_id = 'root' "
                            "AND event_type = 'run.projection_aborted'"
                        ))
                    ).scalars().all()
                    self.assertEqual(
                        ["run.projection_aborted"],
                        projection_diagnostics,
                    )
                    projection_events = (
                        await connection.execute(text(
                            "SELECT event_type FROM agent_run_events "
                            "WHERE run_id = 'root' ORDER BY seq"
                        ))
                    ).scalars().all()
                    self.assertEqual(
                        ["run.completed", "run.projection_aborted"],
                        projection_events,
                    )
                    roles = (
                        await connection.execute(text(
                            "SELECT role FROM messages "
                            "WHERE conversation_id = 'conversation' "
                            "ORDER BY created_at, id"
                        ))
                    ).scalars().all()
                    self.assertEqual(["user", "assistant"], roles)
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
