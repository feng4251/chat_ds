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
]


async def init_db():
    from models import Base as ModelBase  # noqa: F811
    async with engine.begin() as conn:
        await conn.run_sync(ModelBase.metadata.create_all)
        for sql in _LIGHTWEIGHT_MIGRATIONS:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass  # column already exists
