import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.responses import FileResponse

from routers import workspace_router


class WorkspaceDownloadContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_authorizes_conversation_before_resolving_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "report.md").write_text("fixture", encoding="utf-8")
            conversation_guard = AsyncMock(
                side_effect=HTTPException(404, "Conversation not found")
            )
            workspace_resolver = AsyncMock(return_value=workspace)
            with (
                patch.object(
                    workspace_router,
                    "_conversation",
                    conversation_guard,
                ),
                patch.object(
                    workspace_router,
                    "ensure_workspace_async",
                    workspace_resolver,
                ),
            ):
                with self.assertRaises(HTTPException) as raised:
                    await workspace_router.raw_workspace_file(
                        "session-b",
                        "report.md",
                        user=SimpleNamespace(id="user-a"),
                        db=object(),
                    )
        self.assertEqual(raised.exception.status_code, 404)
        workspace_resolver.assert_not_awaited()

    async def test_download_serves_only_a_regular_file_inside_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            report = workspace / "nested" / "report.md"
            report.parent.mkdir()
            report.write_text("fixture", encoding="utf-8")
            with (
                patch.object(
                    workspace_router,
                    "_conversation",
                    AsyncMock(return_value=object()),
                ),
                patch.object(
                    workspace_router,
                    "ensure_workspace_async",
                    AsyncMock(return_value=workspace),
                ),
            ):
                response = await workspace_router.raw_workspace_file(
                    "session-a",
                    "nested/report.md",
                    user=SimpleNamespace(id="user-a"),
                    db=object(),
                )
                with self.assertRaises(HTTPException) as raised:
                    await workspace_router.raw_workspace_file(
                        "session-a",
                        "../outside.txt",
                        user=SimpleNamespace(id="user-a"),
                        db=object(),
                    )
        self.assertIsInstance(response, FileResponse)
        self.assertEqual(Path(response.path), report)
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
