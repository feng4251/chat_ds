from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from config import settings

engine = create_async_engine(settings.database_url, echo=False)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:  # type: ignore
    async with async_session() as session:
        yield session


# Lightweight column-add migrations for SQLite (no Alembic).
_LIGHTWEIGHT_MIGRATIONS = [
    "ALTER TABLE messages ADD COLUMN reasoning TEXT",
    "ALTER TABLE messages ADD COLUMN tool_progress TEXT",
    "ALTER TABLE messages ADD COLUMN source VARCHAR(24) NOT NULL DEFAULT 'chat'",
    "ALTER TABLE messages ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN total_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE skill_packages ADD COLUMN session_id VARCHAR(32)",
    "CREATE INDEX IF NOT EXISTS ix_skill_packages_session_id ON skill_packages (session_id)",
    "ALTER TABLE conversations ADD COLUMN enabled_tools TEXT",
    "ALTER TABLE conversations ADD COLUMN fallback_model_ids TEXT",
    "ALTER TABLE conversations ADD COLUMN enabled_user_skills TEXT",
    "ALTER TABLE conversations ADD COLUMN workspace_version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE conversations ADD COLUMN goal_objective TEXT",
    "ALTER TABLE conversations ADD COLUMN goal_status VARCHAR(24)",
    "ALTER TABLE conversations ADD COLUMN goal_note TEXT",
    "ALTER TABLE conversations ADD COLUMN goal_token_budget INTEGER",
    "ALTER TABLE conversations ADD COLUMN goal_started_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN total_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE agent_runs ADD COLUMN parent_run_id VARCHAR(32)",
    "CREATE INDEX IF NOT EXISTS ix_agent_runs_parent_run_id ON agent_runs (parent_run_id)",
    "ALTER TABLE agent_runs ADD COLUMN root_run_id VARCHAR(32)",
    "CREATE INDEX IF NOT EXISTS ix_agent_runs_root_run_id ON agent_runs (root_run_id)",
    "ALTER TABLE agent_runs ADD COLUMN delegation_tool_call_id VARCHAR(128)",
    "ALTER TABLE agent_runs ADD COLUMN agent_kind VARCHAR(32) NOT NULL DEFAULT 'primary'",
    "ALTER TABLE agent_runs ADD COLUMN agent_name VARCHAR(128)",
    "ALTER TABLE agent_runs ADD COLUMN depth INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE agent_runs ADD COLUMN workspace_scope VARCHAR(32) NOT NULL DEFAULT 'shared_session'",
    "ALTER TABLE agent_runs ADD COLUMN workspace_ref VARCHAR(512)",
    "ALTER TABLE agent_runs ADD COLUMN requested_tools TEXT",
    "ALTER TABLE agent_runs ADD COLUMN effective_tools TEXT",
    "ALTER TABLE agent_runs ADD COLUMN policy TEXT",
    "CREATE TABLE IF NOT EXISTS agent_run_events ("
    "id VARCHAR(32) PRIMARY KEY, "
    "run_id VARCHAR(32) NOT NULL, "
    "conversation_id VARCHAR(32) NOT NULL, "
    "user_id VARCHAR(32) NOT NULL, "
    "parent_run_id VARCHAR(32), "
    "seq INTEGER NOT NULL DEFAULT 0, "
    "event_type VARCHAR(64) NOT NULL, "
    "payload TEXT, "
    "tool_name VARCHAR(512), "
    "tool_call_id VARCHAR(128), "
    "event_time DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE INDEX IF NOT EXISTS ix_agent_run_events_run_id ON agent_run_events (run_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_run_events_conversation_id ON agent_run_events (conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_run_events_user_id ON agent_run_events (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_run_events_parent_run_id ON agent_run_events (parent_run_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_run_events_event_type ON agent_run_events (event_type)",
    "CREATE INDEX IF NOT EXISTS ix_agent_run_events_tool_name ON agent_run_events (tool_name)",
    "CREATE INDEX IF NOT EXISTS ix_agent_run_events_tool_call_id ON agent_run_events (tool_call_id)",
    "CREATE TABLE IF NOT EXISTS artifacts ("
    "id VARCHAR(32) PRIMARY KEY, "
    "user_id VARCHAR(32) NOT NULL, "
    "conversation_id VARCHAR(32) NOT NULL, "
    "run_id VARCHAR(32) NOT NULL, "
    "root_run_id VARCHAR(32), "
    "parent_run_id VARCHAR(32), "
    "kind VARCHAR(32) NOT NULL DEFAULT 'file', "
    "title VARCHAR(256), "
    "path VARCHAR(1024), "
    "mime_type VARCHAR(128), "
    "preview_kind VARCHAR(32), "
    "size_bytes INTEGER NOT NULL DEFAULT 0, "
    "sha256 VARCHAR(64), "
    "source_tool_name VARCHAR(128), "
    "source_tool_call_id VARCHAR(128), "
    "source_event_key VARCHAR(192), "
    "summary TEXT, "
    "metadata_json TEXT, "
    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE INDEX IF NOT EXISTS ix_artifacts_user_id ON artifacts (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_artifacts_conversation_id ON artifacts (conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_artifacts_run_id ON artifacts (run_id)",
    "CREATE INDEX IF NOT EXISTS ix_artifacts_root_run_id ON artifacts (root_run_id)",
    "CREATE INDEX IF NOT EXISTS ix_artifacts_source_tool_name ON artifacts (source_tool_name)",
    "CREATE INDEX IF NOT EXISTS ix_artifacts_source_tool_call_id ON artifacts (source_tool_call_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_artifacts_source_event_key ON artifacts (source_event_key)",
    "CREATE TABLE IF NOT EXISTS task_items ("
    "id VARCHAR(32) PRIMARY KEY, "
    "user_id VARCHAR(32) NOT NULL, "
    "conversation_id VARCHAR(32) NOT NULL, "
    "run_id VARCHAR(32) NOT NULL, "
    "root_run_id VARCHAR(32), "
    "parent_run_id VARCHAR(32), "
    "task_key VARCHAR(192) NOT NULL, "
    "kind VARCHAR(32) NOT NULL DEFAULT 'run', "
    "title VARCHAR(256), "
    "status VARCHAR(24) NOT NULL DEFAULT 'running', "
    "agent_name VARCHAR(128), "
    "summary TEXT, "
    "error TEXT, "
    "metadata_json TEXT, "
    "started_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
    "ended_at DATETIME, "
    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE INDEX IF NOT EXISTS ix_task_items_user_id ON task_items (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_task_items_conversation_id ON task_items (conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_task_items_run_id ON task_items (run_id)",
    "CREATE INDEX IF NOT EXISTS ix_task_items_root_run_id ON task_items (root_run_id)",
    "CREATE INDEX IF NOT EXISTS ix_task_items_parent_run_id ON task_items (parent_run_id)",
    "CREATE INDEX IF NOT EXISTS ix_task_items_task_key ON task_items (task_key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_items_conversation_task_key ON task_items (conversation_id, task_key)",
    "UPDATE agent_runs SET root_run_id = id WHERE root_run_id IS NULL",
]

_AGENT_RUN_EVENT_IDENTITY_INDEX = (
    "ux_agent_run_events_conversation_run_type_seq"
)
_AGENT_RUN_EVENT_IDENTITY_COLUMNS = (
    "conversation_id",
    "run_id",
    "event_type",
    "seq",
)


async def _ensure_agent_run_event_identity(conn) -> None:
    """Install the durable event identity contract on legacy SQLite databases.

    Old databases predate the database-level uniqueness guarantee and may
    contain replay duplicates. SQLite ``rowid`` reflects insertion order for
    this ordinary (non-``WITHOUT ROWID``) table, so retaining the smallest
    ``rowid`` implements the same stable first-wins rule as the projection
    layer. The cleanup and index creation run in the caller's transaction.
    """

    if conn.dialect.name != "sqlite":
        # New non-SQLite databases receive the index from SQLAlchemy metadata.
        # The repository's lightweight legacy migrations are SQLite-specific.
        return

    index_rows = (
        await conn.execute(text("PRAGMA index_list('agent_run_events')"))
    ).mappings().all()
    matching_index = next(
        (
            row
            for row in index_rows
            if str(row.get("name") or "") == _AGENT_RUN_EVENT_IDENTITY_INDEX
        ),
        None,
    )
    if matching_index is not None:
        index_columns = tuple(
            str(row.get("name") or "")
            for row in (
                await conn.execute(text(
                    "PRAGMA index_info("
                    f"'{_AGENT_RUN_EVENT_IDENTITY_INDEX}'"
                    ")"
                ))
            ).mappings().all()
        )
        if (
            int(matching_index.get("unique") or 0) != 1
            or index_columns != _AGENT_RUN_EVENT_IDENTITY_COLUMNS
        ):
            raise RuntimeError(
                "Existing agent-run event identity index has an "
                "incompatible definition"
            )
        return

    await conn.execute(text(
        "DELETE FROM agent_run_events "
        "WHERE rowid NOT IN ("
        "SELECT MIN(rowid) FROM agent_run_events "
        "GROUP BY conversation_id, run_id, event_type, seq"
        ")"
    ))
    await conn.execute(text(
        "CREATE UNIQUE INDEX "
        f"{_AGENT_RUN_EVENT_IDENTITY_INDEX} "
        "ON agent_run_events "
        "(conversation_id, run_id, event_type, seq)"
    ))


async def init_db():
    from models import Base as ModelBase  # noqa: F811
    async with engine.begin() as conn:
        await conn.run_sync(ModelBase.metadata.create_all)
        for sql in _LIGHTWEIGHT_MIGRATIONS:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass  # column already exists
        await _ensure_agent_run_event_identity(conn)
