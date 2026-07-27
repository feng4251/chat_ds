import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from database import _LIGHTWEIGHT_MIGRATIONS


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


if __name__ == "__main__":
    unittest.main()
