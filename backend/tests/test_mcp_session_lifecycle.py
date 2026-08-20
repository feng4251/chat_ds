import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import session_lifecycle
import workspace
from models import Base, Conversation, MCPServerRegistration, User
from routers import mcp_router, skill_router
from workspace_lock import WorkspaceMutationLockError


class MCPSessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_unsupported_timeout_is_rejected_instead_of_silently_ignored(self):
        with self.assertRaises(ValidationError):
            mcp_router.MCPServerConfig(
                name="laboratory-reader",
                url="https://laboratory.example.invalid/mcp",
                transport="http",
                timeout=7,
            )

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
                    engine_id="claude_code",
                ),
            ])
            await db.commit()
        self.patches = (
            patch.object(workspace, "WORKSPACE_ROOT", self.root / "workspace"),
            patch.object(
                session_lifecycle,
                "async_session",
                self.sessions,
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
            url="https://warehouse.example.invalid/mcp",
            transport="http",
        )

    async def test_pending_session_blocks_all_mcp_control_plane_access(self):
        operation_id = "a" * 64
        workspace.claim_session_pending_fence(
            "user-1",
            "session-1",
            operation_id,
        )

        async with self.sessions() as db:
            operations = (
                mcp_router.list_servers(
                    session_id="session-1",
                    user=self.owner,
                    db=db,
                ),
                mcp_router.add_server(
                    self._config(),
                    user=self.owner,
                    db=db,
                ),
                mcp_router.delete_server(
                    "fixture",
                    session_id="session-1",
                    user=self.owner,
                    db=db,
                ),
            )
            for operation in operations:
                with self.assertRaises(WorkspaceMutationLockError) as blocked:
                    await operation
                self.assertEqual(
                    "workspace_session_pending",
                    blocked.exception.code,
                )

        workspace.clear_session_pending_fence(
            "user-1",
            "session-1",
            operation_id,
        )
        async with self.sessions() as db:
            added = await mcp_router.add_server(
                self._config(), user=self.owner, db=db
            )
        async with self.sessions() as db:
            listed = await mcp_router.list_servers(
                session_id="session-1", user=self.owner, db=db
            )
        async with self.sessions() as db:
            removed = await mcp_router.delete_server(
                "fixture", session_id="session-1", user=self.owner, db=db
            )
        self.assertTrue(added["success"])
        self.assertEqual("isolated_per_turn", listed["servers"][0]["connection_lifecycle"])
        self.assertTrue(removed["removed"])

    async def test_wrong_owner_and_unknown_session_never_read_registry(self):
        for user, session_id in (
            (self.other, "session-1"),
            (self.owner, "missing-session"),
        ):
            async with self.sessions() as db:
                with self.assertRaises(HTTPException) as rejected:
                    await mcp_router.list_servers(
                        session_id=session_id,
                        user=user,
                        db=db,
                    )
            self.assertEqual(404, rejected.exception.status_code)

    async def test_deletion_fence_wins_before_mcp_mutation_admission(self):
        async with skill_router._skill_install_lock(
            "user-1",
            "session-1",
        ):
            workspace.publish_session_deletion_tombstone(
                "user-1",
                "session-1",
            )
            async with self.sessions() as db:
                mutation = asyncio.create_task(
                    mcp_router.add_server(
                        self._config(),
                        user=self.owner,
                        db=db,
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

    async def test_user_scope_is_durable_and_session_override_is_exact(self):
        async with self.sessions() as db:
            await mcp_router.add_server(
                mcp_router.MCPServerConfig(
                    name="inventory",
                    url="https://global.example.invalid/mcp",
                ),
                user=self.owner,
                db=db,
            )
        async with self.sessions() as db:
            await mcp_router.add_server(
                mcp_router.MCPServerConfig(
                    name="inventory",
                    session_id="session-1",
                    command="python",
                    args=["/workspace/inventory.py"],
                    transport="stdio",
                ),
                user=self.owner,
                db=db,
            )
        async with self.sessions() as db:
            listed = await mcp_router.list_servers(
                session_id="session-1", user=self.owner, db=db
            )
            rows = list((await db.execute(
                select(MCPServerRegistration).order_by(
                    MCPServerRegistration.session_id
                )
            )).scalars().all())
        self.assertEqual(2, len(rows))
        self.assertEqual([
            {
                "name": "inventory",
                "transport": "stdio",
                "scope": "session",
                "connected": False,
                "connection_lifecycle": "isolated_per_turn",
                "tool_count": None,
            }
        ], listed["servers"])


if __name__ == "__main__":
    unittest.main()
