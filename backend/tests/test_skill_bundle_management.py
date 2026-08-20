import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import workspace
from models import (
    Base,
    Conversation,
    MCPServerRegistration,
    SkillPackage,
    User,
)
from routers import skill_router, workspace_router


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, contents in files.items():
            archive.writestr(path, contents)
    return buffer.getvalue()


def _single_skill_zip(name: str) -> bytes:
    return _zip({
        "skill/SKILL.md": (
            "---\n"
            f"name: {name}\n"
            f"description: Standalone fixture {name}.\n"
            "---\n"
            "Run independently.\n"
        ),
    })


def _bundle_zip(requirement: str = "fixture-runtime==1") -> bytes:
    return _zip({
        "requirements.txt": requirement + "\n",
        "main/SKILL.md": (
            "---\n"
            "name: main-workflow\n"
            "description: Primary workflow fixture.\n"
            "---\n"
            "Coordinate the bundle.\n"
        ),
        "bundle/support/helper/SKILL.md": (
            "---\n"
            "name: helper-capability\n"
            "description: Supporting workflow fixture.\n"
            "---\n"
            "Support the primary.\n"
        ),
    })


def _empty_mcp() -> dict:
    return {"registered": [], "skipped": [], "errors": [], "runtime": None}


class _FailCommit:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def commit(self):
        raise RuntimeError("injected commit failure")


class SkillBundleManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skills_root = self.root / "skills"
        self.workspace_root = self.root / "workspace-root"
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.root / 'test.db'}"
        )
        self.sessions = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add(User(
                id="user-1",
                username="skill-management-user",
                hashed_password="test",
            ))
            db.add(Conversation(
                id="source-session",
                user_id="user-1",
                title="Source",
                model_id="shaiengine_glm_5_2",
            ))
            await db.commit()
        self.user = SimpleNamespace(id="user-1")
        self.auto_mcp = AsyncMock(
            side_effect=lambda *_args, **_kwargs: _empty_mcp()
        )
        self.remove_mcp = AsyncMock(return_value={
            "success": True,
            "removed": [],
        })
        self.patches = (
            patch.object(skill_router, "SKILLS_DATA_DIR", self.skills_root),
            patch.object(workspace, "WORKSPACE_ROOT", self.workspace_root),
            patch.object(skill_router, "_project_skill_mcp", new=self.auto_mcp),
            patch.object(skill_router, "_release_skill_mcp_projection", new=self.remove_mcp),
            patch.object(
                workspace_router,
                "emit_event",
                new=AsyncMock(return_value=None),
            ),
        )
        for active_patch in self.patches:
            active_patch.start()

    async def asyncTearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def _install(self, contents: bytes, filename: str = "bundle.zip"):
        async with self.sessions() as db:
            return await skill_router._process_skill_zip(
                contents,
                filename,
                None,
                "source-session",
                self.user,
                db,
            )

    async def _source_rows(self) -> list[SkillPackage]:
        async with self.sessions() as db:
            return list((await db.execute(
                select(SkillPackage).where(
                    SkillPackage.user_id == "user-1",
                    SkillPackage.session_id == "source-session",
                ).order_by(SkillPackage.name)
            )).scalars().all())

    async def _scope_rows(
        self,
        session_id: str | None,
    ) -> list[SkillPackage]:
        async with self.sessions() as db:
            scope_filter = (
                SkillPackage.session_id == session_id
                if session_id is not None
                else SkillPackage.session_id.is_(None)
            )
            return list((await db.execute(
                select(SkillPackage).where(
                    SkillPackage.user_id == "user-1",
                    scope_filter,
                ).order_by(SkillPackage.name)
            )).scalars().all())

    async def test_bundle_members_fail_closed_for_individual_promote_and_delete(self):
        await self._install(_bundle_zip())
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as supporting_promote:
                await skill_router.promote_skill(
                    "helper-capability",
                    "source-session",
                    self.user,
                    db,
                )
            self.assertEqual(409, supporting_promote.exception.status_code)

            with self.assertRaises(HTTPException) as supporting_delete:
                await skill_router.delete_skill(
                    "helper-capability",
                    "source-session",
                    False,
                    self.user,
                    db,
                )
            self.assertEqual(409, supporting_delete.exception.status_code)

            with self.assertRaises(HTTPException) as primary_promote:
                await skill_router.promote_skill(
                    "main-workflow",
                    "source-session",
                    self.user,
                    db,
                )
            self.assertEqual(409, primary_promote.exception.status_code)

            with self.assertRaises(HTTPException) as primary_delete:
                await skill_router.delete_skill(
                    "main-workflow",
                    "source-session",
                    False,
                    self.user,
                    db,
                )
            self.assertEqual(409, primary_delete.exception.status_code)

        self.assertEqual(
            ["helper-capability", "main-workflow"],
            [row.name for row in await self._source_rows()],
        )

        async with self.sessions() as db:
            deleted = await skill_router.delete_skill(
                "main-workflow",
                "source-session",
                True,
                self.user,
                db,
            )
        self.assertEqual("bundle_deleted", deleted["action"])
        self.assertEqual(
            ["helper-capability", "main-workflow"],
            deleted["deleted_skills"],
        )
        self.assertEqual([], await self._source_rows())
        self.assertFalse(
            (self.skills_root / "user-1" / "source-session"
             / "helper-capability").exists()
        )
        self.assertFalse(
            (self.skills_root / "user-1" / "source-session"
             / "main-workflow").exists()
        )

    async def test_single_skill_keeps_legacy_delete_and_promote_behavior(self):
        await self._install(_single_skill_zip("deletable"), "deletable.zip")
        async with self.sessions() as db:
            deleted = await skill_router.delete_skill(
                "deletable",
                "source-session",
                False,
                self.user,
                db,
            )
        self.assertEqual("deleted", deleted["action"])

        await self._install(_single_skill_zip("promotable"), "promotable.zip")
        async with self.sessions() as db:
            promoted = await skill_router.promote_skill(
                "promotable",
                "source-session",
                self.user,
                db,
            )
            user_row = (await db.execute(
                select(SkillPackage).where(
                    SkillPackage.user_id == "user-1",
                    SkillPackage.session_id.is_(None),
                    SkillPackage.name == "promotable",
                )
            )).scalar_one()
        self.assertEqual("user", promoted["promoted_to"])
        self.assertEqual("reconciled", promoted["mcp"]["status"])
        self.assertEqual("primary", user_row.bundle_role)
        self.assertTrue(
            (self.skills_root / "user-1" / "promotable" / "SKILL.md").is_file()
        )
        self.assertFalse(
            (self.skills_root / "user-1" / "source-session"
             / "promotable").exists()
        )

    async def test_fork_copies_bundle_identity_and_reports_mcp_rebuild(self):
        await self._install(_bundle_zip())
        workspace.ensure_workspace("user-1", "source-session")
        self.auto_mcp.reset_mock()

        async with self.sessions() as db:
            result = await workspace_router.fork_conversation(
                "source-session",
                title="Fork",
                include_messages=True,
                user=self.user,
                db=db,
            )
            source_rows = list((await db.execute(
                select(SkillPackage).where(
                    SkillPackage.user_id == "user-1",
                    SkillPackage.session_id == "source-session",
                ).order_by(SkillPackage.name)
            )).scalars().all())
            fork_rows = list((await db.execute(
                select(SkillPackage).where(
                    SkillPackage.user_id == "user-1",
                    SkillPackage.session_id == result["id"],
                ).order_by(SkillPackage.name)
            )).scalars().all())

        self.assertEqual(2, result["cloned_skill_count"])
        self.assertEqual(
            "reconciled",
            result["skill_mcp_rebuild"]["mcp"]["status"],
        )
        self.assertEqual(2, self.auto_mcp.await_count)
        self.assertEqual(
            [
                (
                    row.bundle_id,
                    row.bundle_role,
                    row.bundle_root_name,
                    row.bundle_source_path,
                )
                for row in source_rows
            ],
            [
                (
                    row.bundle_id,
                    row.bundle_role,
                    row.bundle_root_name,
                    row.bundle_source_path,
                )
                for row in fork_rows
            ],
        )
        for row in fork_rows:
            self.assertTrue(
                (self.skills_root / "user-1" / result["id"]
                 / row.name / "SKILL.md").is_file()
            )

    async def test_fork_copies_only_exact_session_mcp_declarations(self):
        """A fork freezes its source Session MCP view without scope bleed."""

        workspace.ensure_workspace("user-1", "source-session")
        async with self.sessions() as db:
            db.add_all([
                MCPServerRegistration(
                    user_id="user-1",
                    session_id="source-session",
                    name="warehouse-ledger",
                    config_json=(
                        '{"command":"inventory-reader",'
                        '"args":["--site","north"]}'
                    ),
                ),
                MCPServerRegistration(
                    user_id="user-1",
                    session_id=None,
                    name="user-catalog",
                    config_json='{"command":"catalog-reader"}',
                ),
                MCPServerRegistration(
                    user_id="user-1",
                    session_id="other-session",
                    name="laboratory-console",
                    config_json='{"command":"assay-reader"}',
                ),
            ])
            await db.commit()

        async with self.sessions() as db:
            result = await workspace_router.fork_conversation(
                "source-session",
                title="Renamed MCP Fork",
                include_messages=False,
                fork_id="d" * 32,
                user=self.user,
                db=db,
            )

        async with self.sessions() as db:
            copied = list((await db.execute(
                select(MCPServerRegistration).where(
                    MCPServerRegistration.user_id == "user-1",
                    MCPServerRegistration.session_id == result["id"],
                ).order_by(MCPServerRegistration.name)
            )).scalars().all())

        self.assertEqual(
            [
                (
                    "warehouse-ledger",
                    '{"command":"inventory-reader",'
                    '"args":["--site","north"]}',
                )
            ],
            [(row.name, row.config_json) for row in copied],
        )

    async def test_native_fork_drops_archived_harness_routing_state(self):
        """Renamed-domain stale executor settings cannot affect a new fork."""

        workspace.ensure_workspace("user-1", "source-session")
        async with self.sessions() as db:
            source = await db.get(Conversation, "source-session")
            source.enabled_tools = '["warehouse_only_fixture"]'
            source.fallback_model_ids = '["museum_fallback_fixture"]'
            await db.commit()

        async with self.sessions() as db:
            result = await workspace_router.fork_conversation(
                "source-session",
                title="Native laboratory fork",
                include_messages=False,
                fork_id="e" * 32,
                user=self.user,
                db=db,
            )
            fork = await db.get(Conversation, result["id"])
            first_snapshot = fork.fork_snapshot_sha256

        self.assertIsNone(fork.enabled_tools)
        self.assertIsNone(fork.fallback_model_ids)

        # Archived Harness columns are neither copied nor part of native fork
        # identity. Renaming or mutating them cannot perturb the durable
        # snapshot of data that the native target actually receives.
        async with self.sessions() as db:
            source = await db.get(Conversation, "source-session")
            source.enabled_tools = '["geology_retired_fixture"]'
            source.fallback_model_ids = '["archive_retired_fixture"]'
            await db.commit()

        async with self.sessions() as db:
            repeated = await workspace_router.fork_conversation(
                "source-session",
                title="Native laboratory fork",
                include_messages=False,
                fork_id="f" * 32,
                user=self.user,
                db=db,
            )
            repeated_fork = await db.get(Conversation, repeated["id"])

        self.assertEqual(first_snapshot, repeated_fork.fork_snapshot_sha256)
        self.assertIsNone(repeated_fork.enabled_tools)
        self.assertIsNone(repeated_fork.fallback_model_ids)

    async def test_bundle_runtime_is_content_addressed_and_removed_with_bundle(self):
        first = await self._install(
            _bundle_zip("fixture-runtime==1"),
            "bundle-v1.zip",
        )
        first_bundle_id = first["skill"]["bundle_id"]
        first_runtime = (
            self.skills_root / "user-1" / "source-session"
            / "_bundle_runtime" / first_bundle_id / "requirements.txt"
        )
        self.assertEqual("fixture-runtime==1\n", first_runtime.read_text())

        async with self.sessions() as db:
            await skill_router.delete_skill(
                "main-workflow",
                "source-session",
                True,
                self.user,
                db,
            )
        self.assertFalse(first_runtime.exists())

        second = await self._install(
            _bundle_zip("fixture-runtime==2"),
            "bundle-v2.zip",
        )
        second_bundle_id = second["skill"]["bundle_id"]
        self.assertNotEqual(first_bundle_id, second_bundle_id)
        second_runtime = (
            self.skills_root / "user-1" / "source-session"
            / "_bundle_runtime" / second_bundle_id / "requirements.txt"
        )
        self.assertEqual("fixture-runtime==2\n", second_runtime.read_text())

    async def test_delete_postcommit_failure_recovers_from_quarantine(self):
        installed = await self._install(_bundle_zip(), "delete-recovery.zip")
        bundle_id = installed["skill"]["bundle_id"]

        async def interrupted_cleanup(*_args, **_kwargs):
            raise asyncio.CancelledError()

        self.remove_mcp.side_effect = interrupted_cleanup
        async with self.sessions() as db:
            with self.assertRaises(asyncio.CancelledError):
                await skill_router.delete_skill(
                    "main-workflow",
                    "source-session",
                    True,
                    self.user,
                    db,
                )
        self.assertEqual([], await self._source_rows())
        operation_dir = skill_router._skill_operation_dir(
            user_id="user-1",
            kind="delete",
            identity_parts=["source-session", "main-workflow"],
        )
        self.assertTrue((operation_dir / "quarantine").is_dir())

        self.remove_mcp.side_effect = None
        self.remove_mcp.return_value = {"success": True, "removed": []}
        async with self.sessions() as db:
            recovered = await skill_router.delete_skill(
                "main-workflow",
                "source-session",
                True,
                self.user,
                db,
            )
        self.assertTrue(recovered["idempotent"])
        self.assertFalse((operation_dir / "quarantine").exists())
        self.assertFalse(
            (self.skills_root / "user-1" / "source-session"
             / "_bundle_runtime" / bundle_id).exists()
        )

    async def test_promote_postcommit_failure_is_exactly_recoverable(self):
        await self._install(
            _single_skill_zip("recover-promote"),
            "recover-promote.zip",
        )

        async def interrupted_cleanup(*_args, **_kwargs):
            raise asyncio.CancelledError()

        self.remove_mcp.side_effect = interrupted_cleanup
        async with self.sessions() as db:
            with self.assertRaises(asyncio.CancelledError):
                await skill_router.promote_skill(
                    "recover-promote",
                    "source-session",
                    self.user,
                    db,
                )
        self.assertEqual([], await self._source_rows())
        self.assertEqual(
            ["recover-promote"],
            [row.name for row in await self._scope_rows(None)],
        )

        self.remove_mcp.side_effect = None
        self.remove_mcp.return_value = {"success": True, "removed": []}
        async with self.sessions() as db:
            recovered = await skill_router.promote_skill(
                "recover-promote",
                "source-session",
                self.user,
                db,
            )
        self.assertTrue(recovered["idempotent"])
        self.assertEqual("completed", recovered["operation_status"])

    async def test_fork_postcommit_mcp_failure_retries_exact_target(self):
        await self._install(_bundle_zip(), "fork-recovery.zip")
        workspace.ensure_workspace("user-1", "source-session")
        fixed_fork_id = "a" * 32

        async def interrupted_mcp(*_args, **_kwargs):
            raise asyncio.CancelledError()

        self.auto_mcp.side_effect = interrupted_mcp
        async with self.sessions() as db:
            with self.assertRaises(asyncio.CancelledError):
                await workspace_router.fork_conversation(
                    "source-session",
                    title="Recoverable Fork",
                    include_messages=True,
                    fork_id=fixed_fork_id,
                    user=self.user,
                    db=db,
                )
        async with self.sessions() as db:
            committed = await db.get(Conversation, fixed_fork_id)
        self.assertIsNotNone(committed)
        self.assertEqual(
            "source-session",
            committed.forked_from_conversation_id,
        )

        self.auto_mcp.side_effect = (
            lambda *_args, **_kwargs: _empty_mcp()
        )
        async with self.sessions() as db:
            recovered = await workspace_router.fork_conversation(
                "source-session",
                title="Recoverable Fork",
                include_messages=True,
                fork_id=fixed_fork_id,
                user=self.user,
                db=db,
            )
        self.assertTrue(recovered["idempotent"])
        self.assertEqual(fixed_fork_id, recovered["id"])

    async def test_concurrent_same_fork_id_creates_one_exact_snapshot(self):
        await self._install(_bundle_zip(), "fork-concurrent.zip")
        workspace.ensure_workspace("user-1", "source-session")
        fixed_fork_id = "b" * 32
        async with self.sessions() as first_db, self.sessions() as second_db:
            first, second = await asyncio.gather(
                workspace_router.fork_conversation(
                    "source-session",
                    title="Concurrent Fork",
                    include_messages=True,
                    fork_id=fixed_fork_id,
                    user=self.user,
                    db=first_db,
                ),
                workspace_router.fork_conversation(
                    "source-session",
                    title="Concurrent Fork",
                    include_messages=True,
                    fork_id=fixed_fork_id,
                    user=self.user,
                    db=second_db,
                ),
            )
        self.assertEqual(
            {False, True},
            {first["idempotent"], second["idempotent"]},
        )
        async with self.sessions() as db:
            conversations = list((await db.execute(
                select(Conversation).where(
                    Conversation.id == fixed_fork_id,
                    Conversation.user_id == "user-1",
                )
            )).scalars().all())
        self.assertEqual(1, len(conversations))

    async def test_delete_commit_failure_restores_registry_and_quarantine(self):
        await self._install(
            _single_skill_zip("delete-rollback"),
            "delete-rollback.zip",
        )
        async with self.sessions() as inner:
            with self.assertRaisesRegex(
                RuntimeError,
                "injected commit failure",
            ):
                await skill_router.delete_skill(
                    "delete-rollback",
                    "source-session",
                    False,
                    self.user,
                    _FailCommit(inner),
                )
        self.assertEqual(
            ["delete-rollback"],
            [row.name for row in await self._source_rows()],
        )
        self.assertTrue(
            (self.skills_root / "user-1" / "source-session"
             / "delete-rollback" / "SKILL.md").is_file()
        )
        operation_dir = skill_router._skill_operation_dir(
            user_id="user-1",
            kind="delete",
            identity_parts=["source-session", "delete-rollback"],
        )
        self.assertFalse(operation_dir.exists())

    async def test_promote_commit_failure_removes_published_target(self):
        await self._install(
            _single_skill_zip("promote-rollback"),
            "promote-rollback.zip",
        )
        async with self.sessions() as inner:
            with self.assertRaisesRegex(
                RuntimeError,
                "injected commit failure",
            ):
                await skill_router.promote_skill(
                    "promote-rollback",
                    "source-session",
                    self.user,
                    _FailCommit(inner),
                )
        self.assertEqual(
            ["promote-rollback"],
            [row.name for row in await self._source_rows()],
        )
        self.assertEqual([], await self._scope_rows(None))
        self.assertFalse(
            (self.skills_root / "user-1" / "promote-rollback").exists()
        )

    async def test_fork_commit_failure_removes_published_snapshots(self):
        await self._install(_bundle_zip(), "fork-rollback.zip")
        workspace.ensure_workspace("user-1", "source-session")
        fixed_fork_id = "c" * 32
        async with self.sessions() as inner:
            with self.assertRaisesRegex(
                RuntimeError,
                "injected commit failure",
            ):
                await workspace_router.fork_conversation(
                    "source-session",
                    title="Rollback Fork",
                    include_messages=True,
                    fork_id=fixed_fork_id,
                    user=self.user,
                    db=_FailCommit(inner),
                )
        async with self.sessions() as db:
            self.assertIsNone(await db.get(Conversation, fixed_fork_id))
        self.assertFalse(
            (self.workspace_root / "user-1" / fixed_fork_id).exists()
        )
        self.assertFalse(
            (self.skills_root / "user-1" / fixed_fork_id).exists()
        )
        operation_dir = skill_router._skill_operation_dir(
            user_id="user-1",
            kind="fork",
            identity_parts=["source-session", fixed_fork_id],
        )
        self.assertFalse(operation_dir.exists())


if __name__ == "__main__":
    unittest.main()
