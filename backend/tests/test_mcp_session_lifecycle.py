import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import session_lifecycle
import workspace
from models import Base, Conversation, User
from routers import mcp_router, skill_router
from workspace_lock import WorkspaceMutationLockError


class MCPSessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.root / 'mcp.db'}"
        )
        self.sessions = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            db.add_all([
                User(
                    id="user-1",
                    username="mcp-owner",
                    hashed_password="test",
                ),
                User(
                    id="user-2",
                    username="mcp-other",
                    hashed_password="test",
                ),
                Conversation(
                    id="session-1",
                    user_id="user-1",
                    title="MCP session",
                    model_id="AgentModel",
                ),
            ])
            await db.commit()
        self.harness_request = AsyncMock(
            return_value={"success": True}
        )
        self.patches = (
            patch.object(workspace, "WORKSPACE_ROOT", self.root / "workspace"),
            patch.object(
                session_lifecycle,
                "async_session",
                self.sessions,
            ),
            patch.object(
                mcp_router,
                "_harness_request",
                new=self.harness_request,
            ),
        )
        for active_patch in self.patches:
            active_patch.start()
        workspace.ensure_workspace("user-1", "session-1")
        self.owner = SimpleNamespace(id="user-1")
        self.other = SimpleNamespace(id="user-2")

    async def asyncTearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def _config() -> mcp_router.MCPServerConfig:
        return mcp_router.MCPServerConfig(
            name="fixture",
            session_id="session-1",
            url="https://mcp.example.invalid",
        )

    async def test_pending_session_blocks_all_user_mcp_control_plane_access(
        self,
    ):
        operation_id = "a" * 64
        workspace.claim_session_pending_fence(
            "user-1",
            "session-1",
            operation_id,
        )

        operations = (
            mcp_router.list_servers(
                session_id="session-1",
                user=self.owner,
            ),
            mcp_router.add_server(
                self._config(),
                user=self.owner,
            ),
            mcp_router.delete_server(
                "fixture",
                session_id="session-1",
                user=self.owner,
            ),
        )
        for operation in operations:
            with self.assertRaises(WorkspaceMutationLockError) as blocked:
                await operation
            self.assertEqual(
                "workspace_session_pending",
                blocked.exception.code,
            )
        self.harness_request.assert_not_awaited()

        workspace.clear_session_pending_fence(
            "user-1",
            "session-1",
            operation_id,
        )
        listed = await mcp_router.list_servers(
            session_id="session-1",
            user=self.owner,
        )
        added = await mcp_router.add_server(
            self._config(),
            user=self.owner,
        )
        removed = await mcp_router.delete_server(
            "fixture",
            session_id="session-1",
            user=self.owner,
        )
        self.assertTrue(listed["success"])
        self.assertTrue(added["success"])
        self.assertTrue(removed["success"])
        self.assertEqual(3, self.harness_request.await_count)

    async def test_wrong_owner_and_unknown_session_never_reach_harness(self):
        for user, session_id in (
            (self.other, "session-1"),
            (self.owner, "missing-session"),
        ):
            with self.assertRaises(HTTPException) as rejected:
                await mcp_router.list_servers(
                    session_id=session_id,
                    user=user,
                )
            self.assertEqual(404, rejected.exception.status_code)
        self.harness_request.assert_not_awaited()

    async def test_deletion_fence_wins_before_mcp_mutation_admission(self):
        async with skill_router._skill_install_lock(
            "user-1",
            "session-1",
        ):
            workspace.publish_session_deletion_tombstone(
                "user-1",
                "session-1",
            )
            mutation = asyncio.create_task(
                mcp_router.add_server(
                    self._config(),
                    user=self.owner,
                )
            )
            await asyncio.sleep(0)
            self.assertFalse(mutation.done())

        outcome = (await asyncio.gather(
            mutation,
            return_exceptions=True,
        ))[0]
        self.assertIsInstance(outcome, WorkspaceMutationLockError)
        self.assertEqual("workspace_session_deleted", outcome.code)
        self.harness_request.assert_not_awaited()

    async def test_default_user_scope_remains_independent_of_conversations(self):
        result = await mcp_router.add_server(
            mcp_router.MCPServerConfig(
                name="user-scope",
                url="https://mcp.example.invalid",
            ),
            user=self.owner,
        )
        self.assertTrue(result["success"])
        self.harness_request.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
