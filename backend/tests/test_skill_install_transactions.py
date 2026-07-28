import asyncio
import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import workspace
from models import Base, Conversation, SkillPackage, User
from routers import conv_router, skill_router


def _skill_zip(name: str, *, body: str = "Run the workflow.") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "package/SKILL.md",
            (
                "---\n"
                f"name: {name}\n"
                f"description: Transaction fixture for {name}.\n"
                "version: '1.0'\n"
                "---\n"
                f"{body}\n"
            ),
        )
        archive.writestr("package/references/source.txt", "evidence\n")
    return buffer.getvalue()


def _empty_mcp() -> dict:
    return {"registered": [], "skipped": [], "errors": [], "runtime": None}


class _DbProxy:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _BlockSecondExecute(_DbProxy):
    def __init__(self, inner):
        super().__init__(inner)
        self.calls = 0
        self.started = asyncio.Event()
        self.block = asyncio.Event()

    async def execute(self, statement):
        self.calls += 1
        if self.calls == 2:
            self.started.set()
            await self.block.wait()
        return await self.inner.execute(statement)


class _CommitGate(_DbProxy):
    def __init__(self, inner):
        super().__init__(inner)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def commit(self):
        self.started.set()
        await self.release.wait()
        await self.inner.commit()


class _FailCommit(_DbProxy):
    async def commit(self):
        raise RuntimeError("commit failed")


class SkillInstallTransactionTests(unittest.IsolatedAsyncioTestCase):
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
                username="skill-transaction-user",
                hashed_password="test",
            ))
            for session_id in (
                "session-1",
                "session-json-fail",
                "session-multipart-fail",
                "session-conversation-fail",
            ):
                db.add(Conversation(
                    id=session_id,
                    user_id="user-1",
                    model_id="AgentModel",
                ))
            await db.commit()
        self.user = SimpleNamespace(id="user-1")
        self.patches = (
            patch.object(skill_router, "SKILLS_DATA_DIR", self.skills_root),
            patch.object(workspace, "WORKSPACE_ROOT", self.workspace_root),
            patch.object(conv_router, "SANDBOX_BASE", self.workspace_root),
            patch.object(
                skill_router,
                "_auto_register_mcp",
                new=AsyncMock(side_effect=lambda *_args, **_kwargs: _empty_mcp()),
            ),
            patch.object(skill_router, "_invalidate_skills_cache"),
        )
        for active_patch in self.patches:
            active_patch.start()

    async def asyncTearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def _row_count(self, session_id: str) -> int:
        async with self.sessions() as db:
            return int((await db.execute(
                select(func.count(SkillPackage.id)).where(
                    SkillPackage.user_id == "user-1",
                    SkillPackage.session_id == session_id,
                )
            )).scalar_one())

    async def test_concurrent_exact_install_serializes_to_one_registry_row(self):
        contents = _skill_zip("concurrent-skill")
        async with self.sessions() as first_db, self.sessions() as second_db:
            first, second = await asyncio.gather(
                skill_router._process_skill_zip(
                    contents,
                    "concurrent.zip",
                    None,
                    "session-1",
                    self.user,
                    first_db,
                ),
                skill_router._process_skill_zip(
                    contents,
                    "concurrent.zip",
                    None,
                    "session-1",
                    self.user,
                    second_db,
                ),
            )

        self.assertEqual(
            {"installed", "already_installed"},
            {first["installation_status"], second["installation_status"]},
        )
        self.assertEqual(1, await self._row_count("session-1"))
        self.assertTrue(
            (self.skills_root / "user-1" / "session-1"
             / "concurrent-skill" / "SKILL.md").is_file()
        )
        self.assertFalse(list(
            (self.skills_root / "user-1" / "session-1").glob(
                ".skill-install-*"
            )
        ))

    async def test_precommit_cancellation_rolls_back_only_request_owned_paths(self):
        contents = _skill_zip("cancel-before-commit")
        async with self.sessions() as inner:
            db = _BlockSecondExecute(inner)
            task = asyncio.create_task(skill_router._process_skill_zip(
                contents,
                "cancel-before.zip",
                None,
                "session-1",
                self.user,
                db,
            ))
            await db.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(0, await self._row_count("session-1"))
        self.assertFalse(
            (self.skills_root / "user-1" / "session-1"
             / "cancel-before-commit").exists()
        )
        self.assertFalse(
            (self.workspace_root / "user-1" / "session-1" / "workspace"
             / "cancel-before.zip").exists()
        )

    async def test_commit_cancellation_preserves_commit_and_exact_retry_repairs_mcp(self):
        contents = _skill_zip("cancel-during-commit")
        async with self.sessions() as inner:
            db = _CommitGate(inner)
            task = asyncio.create_task(skill_router._process_skill_zip(
                contents,
                "cancel-during.zip",
                None,
                "session-1",
                self.user,
                db,
            ))
            await db.started.wait()
            task.cancel()
            await asyncio.sleep(0)
            db.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(1, await self._row_count("session-1"))
        canonical = (
            self.skills_root / "user-1" / "session-1"
            / "cancel-during-commit"
        )
        archive = (
            self.workspace_root / "user-1" / "session-1" / "workspace"
            / "cancel-during.zip"
        )
        self.assertTrue((canonical / "SKILL.md").is_file())
        self.assertTrue(archive.is_file())

        async with self.sessions() as retry_db:
            retried = await skill_router._process_skill_zip(
                contents,
                "cancel-during.zip",
                None,
                "session-1",
                self.user,
                retry_db,
            )
        self.assertTrue(retried["idempotent"])
        self.assertEqual("already_installed", retried["installation_status"])
        self.assertEqual("exact_retry", retried["mcp"]["reason"])
        self.assertEqual(1, await self._row_count("session-1"))

    async def test_all_three_session_entrypoints_share_archive_contract(self):
        cases = [
            ("json-entry", "json-entry.zip"),
            ("multipart-entry", "multipart-entry.zip"),
            ("conversation-entry", "conversation-entry.zip"),
        ]
        async with self.sessions() as db:
            json_contents = _skill_zip(cases[0][0])
            json_result = await skill_router.upload_skill_json(
                skill_router.SkillUploadJson(
                    filename=cases[0][1],
                    content_base64=base64.b64encode(json_contents).decode("ascii"),
                    session_id="session-1",
                ),
                self.user,
                db,
            )
            multipart_contents = _skill_zip(cases[1][0])
            multipart_result = await skill_router.upload_skill(
                file=UploadFile(
                    filename=cases[1][1],
                    file=io.BytesIO(multipart_contents),
                ),
                category=None,
                session_id="session-1",
                user=self.user,
                db=db,
            )
            conversation_contents = _skill_zip(cases[2][0])
            conversation_result = await conv_router.upload_session_file(
                "session-1",
                UploadFile(
                    filename=cases[2][1],
                    file=io.BytesIO(conversation_contents),
                ),
                self.user,
                db,
            )

        results = [json_result, multipart_result, conversation_result]
        for (skill_name, filename), result in zip(cases, results):
            attachment = result["workspace_attachment"]
            self.assertEqual(filename, attachment["path"])
            self.assertTrue(
                (self.workspace_root / "user-1" / "session-1"
                 / "workspace" / filename).is_file()
            )
            self.assertTrue(
                (self.skills_root / "user-1" / "session-1"
                 / skill_name / "SKILL.md").is_file()
            )
        self.assertEqual(3, await self._row_count("session-1"))

    async def test_all_three_entrypoints_roll_back_archive_on_commit_failure(self):
        cases = [
            ("json-fail", "json-fail.zip", "session-json-fail", "json"),
            (
                "multipart-fail",
                "multipart-fail.zip",
                "session-multipart-fail",
                "multipart",
            ),
            (
                "conversation-fail",
                "conversation-fail.zip",
                "session-conversation-fail",
                "conversation",
            ),
        ]
        for skill_name, filename, session_id, entrypoint in cases:
            contents = _skill_zip(skill_name)
            async with self.sessions() as inner:
                db = _FailCommit(inner)
                with self.assertRaisesRegex(RuntimeError, "commit failed"):
                    if entrypoint == "json":
                        await skill_router.upload_skill_json(
                            skill_router.SkillUploadJson(
                                filename=filename,
                                content_base64=base64.b64encode(contents).decode(
                                    "ascii"
                                ),
                                session_id=session_id,
                            ),
                            self.user,
                            db,
                        )
                    elif entrypoint == "multipart":
                        await skill_router.upload_skill(
                            file=UploadFile(
                                filename=filename,
                                file=io.BytesIO(contents),
                            ),
                            category=None,
                            session_id=session_id,
                            user=self.user,
                            db=db,
                        )
                    else:
                        await conv_router.upload_session_file(
                            session_id,
                            UploadFile(
                                filename=filename,
                                file=io.BytesIO(contents),
                            ),
                            self.user,
                            db,
                        )

            self.assertEqual(0, await self._row_count(session_id))
            self.assertFalse(
                (self.skills_root / "user-1" / session_id / skill_name).exists()
            )
            self.assertFalse(
                (self.workspace_root / "user-1" / session_id / "workspace"
                 / filename).exists()
            )

    async def test_workspace_archive_conflict_never_clobbers_first_request(self):
        first = _skill_zip("first-skill")
        second = _skill_zip("second-skill", body="Different bytes.")
        async with self.sessions() as db:
            await skill_router._process_skill_zip(
                first,
                "same-name.zip",
                None,
                "session-1",
                self.user,
                db,
            )
            with self.assertRaises(HTTPException) as caught:
                await skill_router._process_skill_zip(
                    second,
                    "same-name.zip",
                    None,
                    "session-1",
                    self.user,
                    db,
                )
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(
            first,
            (self.workspace_root / "user-1" / "session-1" / "workspace"
             / "same-name.zip").read_bytes(),
        )
        self.assertFalse(
            (self.skills_root / "user-1" / "session-1"
             / "second-skill").exists()
        )


if __name__ == "__main__":
    unittest.main()
