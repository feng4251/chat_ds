import os
import unittest
from unittest.mock import AsyncMock, patch

import main as harness_main
from tools import browser as browser_tools
from tools import isolated_skill_executor
from tools import mcp_client
from tools import skill_process


class ExecutorStartupReapTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_reaps_each_configured_profile_before_serving(self) -> None:
        receipts = {
            "/base/executor.sock": {
                "runtime_profile": "base-v1",
                "reaped_leases": 1,
            },
            "/browser/executor.sock": {
                "runtime_profile": "browser-automation-v1",
                "reaped_leases": 0,
            },
        }

        async def reap(*, socket_path: str, op_id=None):
            del op_id
            return receipts[socket_path]

        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": "/base/executor.sock",
                    "SKILL_BROWSER_EXECUTOR_SOCKET": (
                        "/browser/executor.sock"
                    ),
                },
            ),
            patch.object(
                isolated_skill_executor,
                "reap_isolated_executor_leases",
                side_effect=reap,
            ) as reaper,
            patch.object(
                mcp_client,
                "get_active_mcp_sessions",
                return_value=[],
            ),
            patch.object(
                browser_tools,
                "close_all_browser_sessions",
                AsyncMock(),
            ),
            patch.object(
                skill_process,
                "close_all_skill_processes",
                AsyncMock(return_value={}),
            ),
        ):
            entered = False
            async with harness_main.lifespan(harness_main.app):
                entered = True
                self.assertEqual(2, reaper.await_count)

        self.assertTrue(entered)
        self.assertEqual(
            {"/base/executor.sock", "/browser/executor.sock"},
            {
                call.kwargs["socket_path"]
                for call in reaper.await_args_list
            },
        )

    async def test_lifespan_fails_closed_when_configured_reap_fails(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": "/base/executor.sock",
                    "SKILL_BROWSER_EXECUTOR_SOCKET": "",
                },
            ),
            patch.object(
                isolated_skill_executor,
                "reap_isolated_executor_leases",
                AsyncMock(side_effect=RuntimeError("fixture unavailable")),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "fixture unavailable",
            ):
                async with harness_main.lifespan(harness_main.app):
                    self.fail("startup must not yield after reap failure")


if __name__ == "__main__":
    unittest.main()
