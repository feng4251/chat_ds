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
    "ALTER TABLE messages ADD COLUMN skill_chain TEXT",
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
