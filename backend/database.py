import json
import logging
import uuid

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from config import settings

logger = logging.getLogger(__name__)

engine = create_async_engine(settings.database_url, echo=False)


@event.listens_for(engine.sync_engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """Enable FK enforcement on every SQLite connection, including tests."""

    if engine.sync_engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA foreign_keys")
        row = cursor.fetchone()
        if row is None or int(row[0]) != 1:
            raise RuntimeError(
                "SQLite connection did not enable foreign key enforcement."
            )
    finally:
        cursor.close()


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
    "ALTER TABLE skill_packages ADD COLUMN bundle_id VARCHAR(64)",
    "ALTER TABLE skill_packages ADD COLUMN bundle_role VARCHAR(16)",
    "ALTER TABLE skill_packages ADD COLUMN bundle_root_name VARCHAR(128)",
    "ALTER TABLE skill_packages ADD COLUMN bundle_source_path VARCHAR(512)",
    "CREATE INDEX IF NOT EXISTS ix_skill_packages_bundle_id ON skill_packages (bundle_id)",
    "ALTER TABLE conversations ADD COLUMN enabled_tools TEXT",
    "ALTER TABLE conversations ADD COLUMN engine_id VARCHAR(32) NOT NULL DEFAULT 'legacy'",
    "ALTER TABLE conversations ADD COLUMN fallback_model_ids TEXT",
    "ALTER TABLE conversations ADD COLUMN enabled_user_skills TEXT",
    "ALTER TABLE conversations ADD COLUMN forked_from_conversation_id VARCHAR(32)",
    "CREATE INDEX IF NOT EXISTS ix_conversations_forked_from_conversation_id ON conversations (forked_from_conversation_id)",
    "ALTER TABLE conversations ADD COLUMN fork_snapshot_sha256 VARCHAR(64)",
    "ALTER TABLE conversations ADD COLUMN workspace_version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE conversations ADD COLUMN goal_objective TEXT",
    "ALTER TABLE conversations ADD COLUMN goal_status VARCHAR(24)",
    "ALTER TABLE conversations ADD COLUMN goal_note TEXT",
    "ALTER TABLE conversations ADD COLUMN goal_token_budget INTEGER",
    "ALTER TABLE conversations ADD COLUMN goal_started_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN total_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE scheduled_jobs ADD COLUMN max_runs INTEGER",
    "ALTER TABLE scheduled_jobs ADD COLUMN run_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE scheduled_jobs ADD COLUMN expires_at DATETIME",
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
    "ALTER TABLE agent_runs ADD COLUMN engine_id VARCHAR(32) NOT NULL DEFAULT 'legacy'",
    "CREATE INDEX IF NOT EXISTS ix_agent_runs_engine_id ON agent_runs (engine_id)",
    "ALTER TABLE agent_runs ADD COLUMN engine_version VARCHAR(64)",
    "ALTER TABLE agent_runs ADD COLUMN native_session_id VARCHAR(128)",
    "CREATE INDEX IF NOT EXISTS ix_agent_runs_native_session_id ON agent_runs (native_session_id)",
    "CREATE TABLE IF NOT EXISTS agent_engine_sessions ("
    "id VARCHAR(32) PRIMARY KEY, "
    "user_id VARCHAR(32) NOT NULL, "
    "conversation_id VARCHAR(32) NOT NULL, "
    "engine_id VARCHAR(32) NOT NULL, "
    "native_session_id VARCHAR(128), "
    "status VARCHAR(24) NOT NULL DEFAULT 'idle', "
    "active_run_id VARCHAR(32), "
    "generation INTEGER NOT NULL DEFAULT 0, "
    "skill_view_sha256 VARCHAR(64), "
    "engine_version VARCHAR(64), "
    "last_model_id VARCHAR(128), "
    "last_event_seq INTEGER NOT NULL DEFAULT 0, "
    "error TEXT, "
    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE INDEX IF NOT EXISTS ix_agent_engine_sessions_user_id ON agent_engine_sessions (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_engine_sessions_conversation_id ON agent_engine_sessions (conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_engine_sessions_engine_id ON agent_engine_sessions (engine_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_engine_sessions_native_session_id ON agent_engine_sessions (native_session_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_engine_sessions_status ON agent_engine_sessions (status)",
    "CREATE INDEX IF NOT EXISTS ix_agent_engine_sessions_active_run_id ON agent_engine_sessions (active_run_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_engine_sessions_conversation_engine ON agent_engine_sessions (conversation_id, engine_id)",
    "CREATE TABLE IF NOT EXISTS agent_engine_raw_events ("
    "id VARCHAR(32) PRIMARY KEY, "
    "user_id VARCHAR(32) NOT NULL, "
    "conversation_id VARCHAR(32) NOT NULL, "
    "run_id VARCHAR(32) NOT NULL, "
    "engine_id VARCHAR(32) NOT NULL, "
    "seq INTEGER NOT NULL, "
    "native_event_id VARCHAR(192), "
    "native_event_type VARCHAR(96), "
    "payload TEXT NOT NULL, "
    "payload_sha256 VARCHAR(64) NOT NULL, "
    "received_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE INDEX IF NOT EXISTS ix_agent_engine_raw_events_user_id ON agent_engine_raw_events (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_engine_raw_events_conversation_id ON agent_engine_raw_events (conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_engine_raw_events_run_id ON agent_engine_raw_events (run_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_engine_raw_events_engine_id ON agent_engine_raw_events (engine_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_engine_raw_events_native_event_type ON agent_engine_raw_events (native_event_type)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_engine_raw_events_run_seq ON agent_engine_raw_events (run_id, seq)",
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
_SKILL_PACKAGE_SCOPE_INDEXES = {
    "ux_skill_packages_user_session_name": (
        ("user_id", "session_id", "name"),
        "session_id IS NOT NULL",
    ),
    "ux_skill_packages_user_name": (
        ("user_id", "name"),
        "session_id IS NULL",
    ),
}
_ORPHAN_REPAIR_BATCH_SIZE = 500
_SESSION_CONVERSATION_COLUMNS = (
    ("agent_engine_raw_events", "conversation_id"),
    ("agent_engine_sessions", "conversation_id"),
    ("agent_run_events", "conversation_id"),
    ("artifacts", "conversation_id"),
    ("task_items", "conversation_id"),
    ("agent_runs", "conversation_id"),
    ("messages", "conversation_id"),
    ("skill_packages", "session_id"),
    ("event_hooks", "conversation_id"),
    ("scheduled_jobs", "conversation_id"),
)
_USER_OWNED_TABLES = (
    ("agent_engine_raw_events", "user_id"),
    ("agent_engine_sessions", "user_id"),
    ("agent_run_events", "user_id"),
    ("artifacts", "user_id"),
    ("task_items", "user_id"),
    ("agent_runs", "user_id"),
    ("skill_packages", "user_id"),
    ("event_hooks", "user_id"),
    ("scheduled_jobs", "user_id"),
    ("custom_model_configs", "user_id"),
    ("conversations", "user_id"),
)


async def _delete_legacy_conversation_orphans(
    conn,
    *,
    table_name: str,
    column_name: str,
) -> int:
    """Delete bounded rowid cohorts whose declared Conversation is absent."""

    total = 0
    while True:
        await conn.execute(text(
            f"DELETE FROM {table_name} "
            "WHERE rowid IN ("
            f"SELECT child.rowid FROM {table_name} AS child "
            "LEFT JOIN conversations AS parent "
            f"ON parent.id = child.{column_name} "
            "LEFT JOIN users AS parent_owner "
            "ON parent_owner.id = parent.user_id "
            f"WHERE child.{column_name} IS NOT NULL "
            "AND (parent.id IS NULL OR parent_owner.id IS NULL) "
            "ORDER BY child.rowid "
            f"LIMIT {_ORPHAN_REPAIR_BATCH_SIZE}"
            ")"
        ))
        removed = int((
            await conn.execute(text("SELECT changes()"))
        ).scalar_one())
        total += removed
        if removed < _ORPHAN_REPAIR_BATCH_SIZE:
            return total


async def _delete_legacy_user_orphans(
    conn,
    *,
    table_name: str,
    column_name: str,
) -> int:
    total = 0
    while True:
        await conn.execute(text(
            f"DELETE FROM {table_name} "
            "WHERE rowid IN ("
            f"SELECT child.rowid FROM {table_name} AS child "
            "LEFT JOIN users AS parent "
            f"ON parent.id = child.{column_name} "
            f"WHERE child.{column_name} IS NOT NULL "
            "AND parent.id IS NULL "
            "ORDER BY child.rowid "
            f"LIMIT {_ORPHAN_REPAIR_BATCH_SIZE}"
            ")"
        ))
        removed = int((
            await conn.execute(text("SELECT changes()"))
        ).scalar_one())
        total += removed
        if removed < _ORPHAN_REPAIR_BATCH_SIZE:
            return total


async def _repair_legacy_foreign_key_orphans(conn) -> dict[str, int]:
    """Repair only rows whose owning Conversation no longer exists.

    Old deployments ran SQLite with ``foreign_keys=0``. The bounded,
    deterministic cleanup runs in the caller's startup transaction, preserving
    every row that still has a live Conversation owner.
    """

    if conn.dialect.name != "sqlite":
        return {}
    repaired: dict[str, int] = {}

    # Runs of an orphan scheduled job must be removed before the job. Direct
    # conversation orphans are also session-owned historic rows.
    total_job_runs = 0
    while True:
        await conn.execute(text(
            "DELETE FROM scheduled_job_runs "
            "WHERE rowid IN ("
            "SELECT run.rowid FROM scheduled_job_runs AS run "
            "LEFT JOIN scheduled_jobs AS job ON job.id = run.job_id "
            "LEFT JOIN users AS job_owner ON job_owner.id = job.user_id "
            "LEFT JOIN conversations AS direct_parent "
            "ON direct_parent.id = run.conversation_id "
            "LEFT JOIN users AS direct_parent_owner "
            "ON direct_parent_owner.id = direct_parent.user_id "
            "LEFT JOIN conversations AS job_parent "
            "ON job_parent.id = job.conversation_id "
            "LEFT JOIN users AS job_parent_owner "
            "ON job_parent_owner.id = job_parent.user_id "
            "WHERE job.id IS NULL "
            "OR job_owner.id IS NULL "
            "OR (run.conversation_id IS NOT NULL "
            "AND (direct_parent.id IS NULL "
            "OR direct_parent_owner.id IS NULL)) "
            "OR (job.conversation_id IS NOT NULL "
            "AND (job_parent.id IS NULL "
            "OR job_parent_owner.id IS NULL)) "
            "ORDER BY run.rowid "
            f"LIMIT {_ORPHAN_REPAIR_BATCH_SIZE}"
            ")"
        ))
        removed = int((
            await conn.execute(text("SELECT changes()"))
        ).scalar_one())
        total_job_runs += removed
        if removed < _ORPHAN_REPAIR_BATCH_SIZE:
            break
    if total_job_runs:
        repaired["scheduled_job_runs"] = total_job_runs

    for table_name, column_name in _SESSION_CONVERSATION_COLUMNS:
        removed = await _delete_legacy_conversation_orphans(
            conn,
            table_name=table_name,
            column_name=column_name,
        )
        if removed:
            repaired[table_name] = removed
    # Conversation-descendant rows were removed first, so deleting a
    # Conversation whose User is absent cannot strand raw legacy tables that
    # predate FK declarations. Global user-owned rows are handled here too.
    for table_name, column_name in _USER_OWNED_TABLES:
        removed = await _delete_legacy_user_orphans(
            conn,
            table_name=table_name,
            column_name=column_name,
        )
        if removed:
            repaired[table_name] = (
                repaired.get(table_name, 0) + removed
            )
    if repaired:
        logger.warning(
            "Repaired legacy rows with missing Conversation owners: %s",
            repaired,
        )
    return repaired


async def _assert_sqlite_foreign_key_integrity(conn) -> None:
    if conn.dialect.name != "sqlite":
        return
    result = await conn.execute(text("PRAGMA foreign_key_check"))
    rows = result.fetchmany(1001)
    if not rows:
        return
    truncated = len(rows) > 1000
    evidence_rows = rows[:1000]
    counts: dict[str, int] = {}
    for row in evidence_rows:
        table_name = str(row[0] or "unknown")
        counts[table_name] = counts.get(table_name, 0) + 1
    raise RuntimeError(
        "SQLite foreign-key integrity check failed after bounded legacy "
        f"repair: sample_counts={counts}, truncated={truncated}"
    )


async def _ensure_skill_package_scope_identity(conn) -> None:
    """Make one Skill name unique inside each user/session scope.

    Historic duplicate rows all point at the same canonical directory. Retain
    the latest inserted registry row (largest SQLite ``rowid``), then install
    separate partial indexes for session and user scope. This avoids SQLite's
    ``NULL != NULL`` uniqueness behavior without changing valid packages.
    """

    if conn.dialect.name != "sqlite":
        return
    table_exists = (
        await conn.execute(text(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'skill_packages'"
        ))
    ).scalar_one_or_none()
    if table_exists is None:
        return

    index_rows = {
        str(row.get("name") or ""): row
        for row in (
            await conn.execute(text("PRAGMA index_list('skill_packages')"))
        ).mappings().all()
    }
    missing: list[tuple[str, tuple[str, ...], str]] = []
    for index_name, (expected_columns, predicate) in (
        _SKILL_PACKAGE_SCOPE_INDEXES.items()
    ):
        existing = index_rows.get(index_name)
        if existing is None:
            missing.append((index_name, expected_columns, predicate))
            continue
        columns = tuple(
            str(row.get("name") or "")
            for row in (
                await conn.execute(text(f"PRAGMA index_info('{index_name}')"))
            ).mappings().all()
        )
        definition = str((
            await conn.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'index' AND name = :name"
                ),
                {"name": index_name},
            )
        ).scalar_one_or_none() or "")
        normalized_definition = " ".join(definition.upper().split())
        if (
            int(existing.get("unique") or 0) != 1
            or columns != expected_columns
            or f"WHERE {predicate.upper()}" not in normalized_definition
        ):
            raise RuntimeError(
                f"Existing Skill package identity index '{index_name}' "
                "has an incompatible definition"
            )

    if not missing:
        return
    await conn.execute(text(
        "DELETE FROM skill_packages "
        "WHERE rowid NOT IN ("
        "SELECT MAX(rowid) FROM skill_packages "
        "GROUP BY user_id, session_id, name"
        ")"
    ))
    for index_name, columns, predicate in missing:
        await conn.execute(text(
            f"CREATE UNIQUE INDEX {index_name} "
            f"ON skill_packages ({', '.join(columns)}) "
            f"WHERE {predicate}"
        ))


def _authoritative_terminal_from_rows(rows) -> tuple[str, dict] | None:
    """Return the first authoritative terminal from ordered SQLite rows."""

    for row in rows:
        event_type = str(row.get("event_type") or "")
        try:
            payload = json.loads(row.get("payload") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        authoritative = payload.get("authoritative")
        if authoritative is False or (
            authoritative is not True
            and payload.get("provisional_terminal") is True
        ):
            continue
        return event_type, payload
    return None


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


async def _reconcile_orphaned_descendant_runs(conn) -> None:
    """Cancel runs still active from a previous single Backend process.

    This deployment uses one Uvicorn worker and one Backend container against
    SQLite. Consequently no in-memory producer can survive process startup:
    every pre-existing active row is stale. Roots are attributed to process
    restart; descendants of a root that is (or becomes) terminal are
    attributed to parent-terminal reconciliation. The caller owns one startup
    transaction, so events, runs, and task projections move together.

    A future multi-instance deployment must replace this single-owner startup
    rule with persisted instance leases/heartbeats before sharing a database.
    """

    if conn.dialect.name != "sqlite":
        return
    active_statuses = (
        "pending",
        "planned",
        "queued",
        "running",
        "committing",
    )
    rows = (
        await conn.execute(
            text(
                "SELECT child.id, child.conversation_id, child.user_id, "
                "child.parent_run_id, child.root_run_id, "
                "child.status AS run_status, "
                "root.status AS root_status "
                "FROM agent_runs AS child "
                "LEFT JOIN agent_runs AS root "
                "ON root.id = child.root_run_id "
                "WHERE child.status IN "
                "('pending', 'planned', 'queued', 'running', 'committing') "
                "ORDER BY CASE "
                "WHEN child.parent_run_id IS NULL "
                "OR child.id = child.root_run_id THEN 0 ELSE 1 END, "
                "child.started_at, child.id"
            )
        )
    ).mappings().all()

    durable_terminals: dict[str, tuple[str, dict]] = {}
    for row in rows:
        terminal_rows = (
            await conn.execute(
                text(
                    "SELECT event_type, payload "
                    "FROM agent_run_events "
                    "WHERE conversation_id = :conversation_id "
                    "AND run_id = :run_id "
                    "AND event_type IN "
                    "('run.completed', 'run.failed', 'run.cancelled') "
                    "ORDER BY seq, event_time, id"
                ),
                {
                    "conversation_id": row["conversation_id"],
                    "run_id": row["id"],
                },
            )
        ).mappings().all()
        terminal = _authoritative_terminal_from_rows(terminal_rows)
        if terminal is not None:
            durable_terminals[str(row["id"])] = terminal

    reconciled_root_statuses: dict[str, str] = {}

    for row in rows:
        run_id = str(row["id"])
        root_run_id = str(row.get("root_run_id") or run_id)
        is_root = (
            row.get("parent_run_id") is None
            or run_id == root_run_id
        )
        root_status = str(row.get("root_status") or "")
        root_terminal = durable_terminals.get(root_run_id)
        if root_run_id in reconciled_root_statuses:
            root_status = reconciled_root_statuses[root_run_id]
        elif root_terminal is not None:
            root_status = {
                "run.completed": "succeeded",
                "run.failed": "failed",
                "run.cancelled": "cancelled",
            }[root_terminal[0]]
        durable_terminal = durable_terminals.get(run_id)
        if durable_terminal is not None:
            event_type, terminal_payload = durable_terminal
            projection_interrupted = bool(
                is_root and str(row.get("run_status") or "") == "committing"
            )
            if projection_interrupted:
                # ``committing`` means the engine terminal was recorded but
                # the assistant/control-write transaction never committed.
                # Replaying native success here would manufacture effects and
                # lie about the application-level outcome.
                projected_status = "failed"
                finish_reason = "terminal_projection_interrupted"
                projected_error = (
                    "Controller-owned terminal projection was interrupted "
                    "by Backend restart."
                )
                reconciled_root_statuses[root_run_id] = "failed"
            else:
                projected_status = {
                    "run.completed": "succeeded",
                    "run.failed": "failed",
                    "run.cancelled": "cancelled",
                }[event_type]
                finish_reason = str(
                    terminal_payload.get("finish_reason")
                    or terminal_payload.get("terminal_reason")
                    or (
                        "stop"
                        if projected_status == "succeeded"
                        else "task_cancelled"
                        if projected_status == "cancelled"
                        else "agent_run_failed"
                    )
                )[:256]
                projected_error = (
                    str(
                        terminal_payload.get("error")
                        or "Agent run failed."
                    )[:4000]
                    if projected_status == "failed"
                    else None
                )
            await conn.execute(
                text(
                    "UPDATE agent_runs "
                    "SET status = :status, error = :error, "
                    "finish_reason = :finish_reason, "
                    "ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP) "
                    "WHERE id = :run_id "
                    "AND status IN "
                    "('pending', 'planned', 'queued', 'running', 'committing')"
                ),
                {
                    "run_id": run_id,
                    "status": projected_status,
                    "error": projected_error,
                    "finish_reason": finish_reason,
                },
            )
            await conn.execute(
                text(
                    "UPDATE task_items "
                    "SET status = :status, error = :error, "
                    "summary = :summary, "
                    "ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP), "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE run_id = :run_id "
                    "AND status IN "
                    "('pending', 'planned', 'queued', 'running', 'committing')"
                ),
                {
                    "run_id": run_id,
                    "status": projected_status,
                    "error": projected_error,
                    "summary": (
                        "terminal_projection_interrupted"
                        if projection_interrupted
                        else "backend_projection_recovered_after_restart"
                    ),
                },
            )
            if is_root:
                next_seq = (
                    await conn.execute(
                        text(
                            "SELECT COALESCE(MAX(seq), 0) + 1 "
                            "FROM agent_run_events "
                            "WHERE conversation_id = :conversation_id "
                            "AND run_id = :run_id"
                        ),
                        {
                            "conversation_id": row["conversation_id"],
                            "run_id": run_id,
                        },
                    )
                ).scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO agent_run_events ("
                        "id, run_id, conversation_id, user_id, "
                        "parent_run_id, seq, event_type, payload, event_time"
                        ") VALUES ("
                        ":id, :run_id, :conversation_id, :user_id, NULL, "
                        ":seq, 'run.projection_aborted', :payload, "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "run_id": run_id,
                        "conversation_id": row["conversation_id"],
                        "user_id": row["user_id"],
                        "seq": int(next_seq),
                        "payload": json.dumps(
                            {
                                "reason": "backend_process_restart",
                                "recovered_terminal_event_type": event_type,
                                "projection_committed": False,
                                "authoritative": False,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                )
                latest_role = (
                    await conn.execute(
                        text(
                            "SELECT role FROM messages "
                            "WHERE conversation_id = :conversation_id "
                            "ORDER BY created_at DESC, id DESC LIMIT 1"
                        ),
                        {"conversation_id": row["conversation_id"]},
                    )
                ).scalar_one_or_none()
                if latest_role == "user":
                    await conn.execute(
                        text(
                            "INSERT INTO messages ("
                            "id, conversation_id, role, content, model_id, "
                            "source, input_tokens, output_tokens, "
                            "total_tokens, created_at"
                            ") VALUES ("
                            ":id, :conversation_id, 'assistant', :content, "
                            "(SELECT COALESCE(resolved_model_id, "
                            "requested_model_id) FROM agent_runs "
                            "WHERE id = :run_id), "
                            "'chat', 0, 0, 0, "
                            "COALESCE(("
                            "SELECT strftime("
                            "'%Y-%m-%d %H:%M:%f', "
                            "julianday(MAX(created_at)) + "
                            "(1.0 / 86400000.0)"
                            ") FROM messages "
                            "WHERE conversation_id = :conversation_id"
                            "), CURRENT_TIMESTAMP))"
                        ),
                        {
                            "id": uuid.uuid4().hex,
                            "conversation_id": row["conversation_id"],
                            "run_id": run_id,
                            "content": (
                                "⚠️ Backend restarted after the engine terminal "
                                "was received but before the assistant response "
                                "and controller-owned effects were durably "
                                "committed. Existing artifacts and run records "
                                "were preserved, but this Turn failed closed."
                            ),
                        },
                    )
            continue
        if is_root or root_status not in {
            "succeeded",
            "failed",
            "cancelled",
            *active_statuses,
        }:
            finish_reason = "backend_process_restart"
            cancellation_source = "backend_startup_orphan_repair"
            parent_terminal_status = None
        else:
            finish_reason = "parent_run_terminal_reconciliation"
            cancellation_source = "root_run_terminal_startup_repair"
            parent_terminal_status = (
                "cancelled"
                if root_status in active_statuses
                else root_status
            )
        payload = {
            "authoritative": True,
            "finish_reason": finish_reason,
            "terminal_reason": finish_reason,
            "cancellation_source": cancellation_source,
        }
        if parent_terminal_status:
            payload["parent_terminal_status"] = parent_terminal_status

        next_seq = (
            await conn.execute(
                text(
                    "SELECT COALESCE(MAX(seq), 0) + 1 "
                    "FROM agent_run_events "
                    "WHERE conversation_id = :conversation_id "
                    "AND run_id = :run_id"
                ),
                {
                    "conversation_id": row["conversation_id"],
                    "run_id": run_id,
                },
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO agent_run_events ("
                "id, run_id, conversation_id, user_id, parent_run_id, seq, "
                "event_type, payload, event_time"
                ") VALUES ("
                ":id, :run_id, :conversation_id, :user_id, :parent_run_id, "
                ":seq, 'run.cancelled', :payload, CURRENT_TIMESTAMP)"
            ),
            {
                "id": uuid.uuid4().hex,
                "run_id": run_id,
                "conversation_id": row["conversation_id"],
                "user_id": row["user_id"],
                "parent_run_id": row.get("parent_run_id"),
                "seq": int(next_seq),
                "payload": json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )
        await conn.execute(
            text(
                "UPDATE task_items "
                "SET status = 'cancelled', error = NULL, "
                "summary = :finish_reason, "
                "ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP), "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE run_id = :run_id "
                "AND status IN "
                "('pending', 'planned', 'queued', 'running', 'committing')"
            ),
            {
                "run_id": run_id,
                "finish_reason": finish_reason,
            },
        )
        await conn.execute(
            text(
                "UPDATE agent_runs "
                "SET status = 'cancelled', error = NULL, "
                "finish_reason = :finish_reason, "
                "ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP) "
                "WHERE id = :run_id "
                "AND status IN "
                "('pending', 'planned', 'queued', 'running', 'committing')"
            ),
            {
                "run_id": run_id,
                "finish_reason": finish_reason,
            },
        )


async def _reconcile_orphaned_scheduled_job_runs(conn) -> int:
    """Terminalize cron executions left running by a prior Backend process.

    The scheduler is process-owned and this deployment has one Backend worker,
    so no persisted ``running`` row can still have a live execution at startup.
    Recurring jobs retain the next time claimed before dispatch; one-shot jobs
    are disabled so a restart cannot duplicate an unattended side effect.
    """

    if conn.dialect.name != "sqlite":
        return 0
    stale_count = int((await conn.execute(text(
        "SELECT COUNT(*) FROM scheduled_job_runs WHERE status = 'running'"
    ))).scalar_one())
    if stale_count == 0:
        return 0
    await conn.execute(text(
        "UPDATE scheduled_jobs "
        "SET last_status = 'cancelled', "
        "enabled = CASE WHEN schedule_kind = 'once' THEN 0 ELSE enabled END, "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE id IN ("
        "SELECT stale.job_id FROM scheduled_job_runs AS stale "
        "WHERE stale.status = 'running' "
        "AND stale.rowid = ("
        "SELECT MAX(latest.rowid) FROM scheduled_job_runs AS latest "
        "WHERE latest.job_id = stale.job_id"
        ")"
        ")"
    ))
    await conn.execute(text(
        "UPDATE scheduled_job_runs "
        "SET status = 'cancelled', "
        "error = COALESCE(NULLIF(error, ''), 'backend_process_restart'), "
        "ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP) "
        "WHERE status = 'running'"
    ))
    return stale_count


async def init_db():
    from models import Base as ModelBase  # noqa: F811
    async with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            enabled = (
                await conn.execute(text("PRAGMA foreign_keys"))
            ).scalar_one()
            if int(enabled) != 1:
                raise RuntimeError(
                    "SQLite foreign key enforcement is disabled."
                )
        await conn.run_sync(ModelBase.metadata.create_all)
        for sql in _LIGHTWEIGHT_MIGRATIONS:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass  # column already exists
        await _repair_legacy_foreign_key_orphans(conn)
        await _ensure_skill_package_scope_identity(conn)
        await _ensure_agent_run_event_identity(conn)
        repaired_scheduled_runs = (
            await _reconcile_orphaned_scheduled_job_runs(conn)
        )
        if repaired_scheduled_runs:
            logger.warning(
                "Cancelled %s orphaned scheduled run(s) during startup",
                repaired_scheduled_runs,
            )
        await _reconcile_orphaned_descendant_runs(conn)
        await _assert_sqlite_foreign_key_integrity(conn)
