import os
import unittest
from unittest.mock import AsyncMock, patch

import main as harness_main
from tools import browser as browser_tools
from tools import isolated_skill_executor
from tools import mcp_client
from tools import skill_process


class ExecutorStartupReapTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_reaps_unified_sandbox_once_before_serving(self) -> None:
        receipts = {
            "/session/executor.sock": {
                "runtime_profile": "session-sandbox-v1",
                "reaped_leases": 1,
                "worker_processes_empty": True,
            },
        }

        async def reap(*, socket_path: str, op_id=None):
            del op_id
            return receipts[socket_path]

        capabilities = {
            "valid": True,
            "runtime_identity": {
                "runtime_profile": "session-sandbox-v1",
            },
            "requirements": [],
            "commands": [],
            "environment_variables": [],
            "platform_groups": [],
        }
        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": "/session/executor.sock",
                    "SKILL_BROWSER_EXECUTOR_SOCKET": (
                        "/session/executor.sock"
                    ),
                },
            ),
            patch.object(
                isolated_skill_executor,
                "reap_isolated_executor_leases",
                side_effect=reap,
            ) as reaper,
            patch.object(
                isolated_skill_executor,
                "probe_isolated_runtime_capabilities",
                return_value=capabilities,
            ) as probe,
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
                self.assertEqual(1, reaper.await_count)
                self.assertEqual(1, probe.call_count)
                from tools.executor_slot_pool import get_executor_slot_pool

                pool = get_executor_slot_pool(
                    primary_socket="/session/executor.sock",
                )
                uncertain = await pool.acquire("transient")
                await uncertain.quarantine(
                    "executor_connect_unavailable"
                )
                recovered = await pool.acquire("transient")
                await recovered.release()
                self.assertEqual(2, reaper.await_count)
                self.assertEqual(2, probe.call_count)

        self.assertTrue(entered)
        self.assertEqual(
            {"/session/executor.sock"},
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
                "sufficient homogeneous healthy capacity",
            ):
                async with harness_main.lifespan(harness_main.app):
                    self.fail("startup must not yield after reap failure")

    async def test_lifespan_reaps_and_attests_every_pool_slot(self) -> None:
        paths = tuple(f"/pool/slot-{index}.sock" for index in range(4))

        async def reap(*, socket_path: str, op_id=None):
            del socket_path, op_id
            return {
                "runtime_profile": "session-sandbox-v1",
                "reaped_leases": 0,
            }

        capabilities = {
            "valid": True,
            "runtime_identity": {
                "runtime_profile": "session-sandbox-v1",
                "execution_runtime": "isolated_skill_executor",
            },
            "requirements": [],
            "commands": [],
            "environment_variables": [],
            "platform_groups": [],
        }
        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": paths[0],
                    "EXECUTOR_POOL_SOCKETS": ",".join(paths),
                    "SKILL_BROWSER_EXECUTOR_SOCKET": paths[0],
                },
            ),
            patch.object(
                isolated_skill_executor,
                "reap_isolated_executor_leases",
                side_effect=reap,
            ) as reaper,
            patch.object(
                isolated_skill_executor,
                "probe_isolated_runtime_capabilities",
                return_value=capabilities,
            ) as probe,
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
            async with harness_main.lifespan(harness_main.app):
                self.assertEqual(4, reaper.await_count)
                self.assertEqual(4, probe.call_count)

        self.assertEqual(
            set(paths),
            {
                call.kwargs["socket_path"]
                for call in reaper.await_args_list
            },
        )


if __name__ == "__main__":
    unittest.main()
