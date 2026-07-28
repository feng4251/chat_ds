import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from database import (
    _LIGHTWEIGHT_MIGRATIONS,
    _ensure_skill_package_scope_identity,
)


class SkillBundleSchemaMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_skill_table_receives_bundle_identity_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(temp_dir) / 'legacy.db'}"
            )
            try:
                async with engine.begin() as connection:
                    await connection.execute(text(
                        "CREATE TABLE skill_packages ("
                        "id VARCHAR(32) PRIMARY KEY, "
                        "user_id VARCHAR(32) NOT NULL, "
                        "name VARCHAR(128) NOT NULL)"
                    ))
                    migrations = [
                        sql
                        for sql in _LIGHTWEIGHT_MIGRATIONS
                        if "skill_packages" in sql
                    ]
                    for _ in range(2):
                        for sql in migrations:
                            try:
                                await connection.execute(text(sql))
                            except Exception:
                                # init_db intentionally treats an existing
                                # column as an idempotent no-op.
                                pass

                    columns = {
                        row["name"]: row["type"]
                        for row in (
                            await connection.execute(
                                text("PRAGMA table_info('skill_packages')")
                            )
                        ).mappings().all()
                    }
                    self.assertEqual("VARCHAR(64)", columns["bundle_id"])
                    self.assertEqual("VARCHAR(16)", columns["bundle_role"])
                    self.assertEqual(
                        "VARCHAR(128)", columns["bundle_root_name"]
                    )
                    self.assertEqual(
                        "VARCHAR(512)", columns["bundle_source_path"]
                    )

                    indexes = {
                        row["name"]
                        for row in (
                            await connection.execute(
                                text("PRAGMA index_list('skill_packages')")
                            )
                        ).mappings().all()
                    }
                    self.assertIn("ix_skill_packages_bundle_id", indexes)
            finally:
                await engine.dispose()

    async def test_scope_identity_migration_deduplicates_then_enforces_both_scopes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(temp_dir) / 'identity.db'}"
            )
            try:
                async with engine.begin() as connection:
                    await connection.execute(text(
                        "CREATE TABLE skill_packages ("
                        "id VARCHAR(32) PRIMARY KEY, "
                        "user_id VARCHAR(32) NOT NULL, "
                        "name VARCHAR(128) NOT NULL, "
                        "session_id VARCHAR(32))"
                    ))
                    await connection.execute(text(
                        "INSERT INTO skill_packages "
                        "(id, user_id, name, session_id) VALUES "
                        "('old-user', 'u', 'same', NULL), "
                        "('new-user', 'u', 'same', NULL), "
                        "('old-session', 'u', 'same', 's'), "
                        "('new-session', 'u', 'same', 's'), "
                        "('other-session', 'u', 'same', 's2')"
                    ))
                    await _ensure_skill_package_scope_identity(connection)
                    await _ensure_skill_package_scope_identity(connection)

                    rows = (
                        await connection.execute(text(
                            "SELECT id FROM skill_packages ORDER BY id"
                        ))
                    ).scalars().all()
                    self.assertEqual(
                        ["new-session", "new-user", "other-session"],
                        rows,
                    )
                    indexes = {
                        row["name"]: row
                        for row in (
                            await connection.execute(
                                text("PRAGMA index_list('skill_packages')")
                            )
                        ).mappings().all()
                    }
                    self.assertEqual(
                        1,
                        indexes["ux_skill_packages_user_session_name"]["unique"],
                    )
                    self.assertEqual(
                        1,
                        indexes["ux_skill_packages_user_name"]["unique"],
                    )

                    with self.assertRaises(Exception):
                        await connection.execute(text(
                            "INSERT INTO skill_packages "
                            "(id, user_id, name, session_id) "
                            "VALUES ('dup-user', 'u', 'same', NULL)"
                        ))
            finally:
                await engine.dispose()

    async def test_legacy_conversation_table_receives_fork_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(temp_dir) / 'fork.db'}"
            )
            try:
                async with engine.begin() as connection:
                    await connection.execute(text(
                        "CREATE TABLE conversations ("
                        "id VARCHAR(32) PRIMARY KEY)"
                    ))
                    migrations = [
                        sql
                        for sql in _LIGHTWEIGHT_MIGRATIONS
                        if (
                            "forked_from_conversation_id" in sql
                            or "fork_snapshot_sha256" in sql
                        )
                    ]
                    for sql in migrations:
                        await connection.execute(text(sql))
                    columns = {
                        row["name"]: row["type"]
                        for row in (
                            await connection.execute(
                                text("PRAGMA table_info('conversations')")
                            )
                        ).mappings().all()
                    }
                    self.assertEqual(
                        "VARCHAR(32)",
                        columns["forked_from_conversation_id"],
                    )
                    self.assertEqual(
                        "VARCHAR(64)",
                        columns["fork_snapshot_sha256"],
                    )
                    indexes = {
                        row["name"]
                        for row in (
                            await connection.execute(
                                text("PRAGMA index_list('conversations')")
                            )
                        ).mappings().all()
                    }
                    self.assertIn(
                        "ix_conversations_forked_from_conversation_id",
                        indexes,
                    )
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
