from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import agent_loop


class BrowserRunLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_scope_is_shared_with_tool_context_and_final_cleanup(self) -> None:
        observed_scope: list[str] = []

        @agent_loop._emit_run_cancelled_on_cancellation
        async def one_event_stream(
            user_id="default",
            session_id="default",
            run_id=None,
            _browser_run_scope_id=None,
            **_kwargs,
        ):
            observed_scope.append(str(_browser_run_scope_id or ""))
            yield {"type": "done", "finish_reason": "stop"}

        cleanup = AsyncMock()
        with patch("tools.browser.close_browser_run", cleanup):
            events = [
                event
                async for event in one_event_stream(
                    user_id="user",
                    session_id="session",
                )
            ]

        self.assertEqual([{"type": "done", "finish_reason": "stop"}], events)
        self.assertEqual(1, len(observed_scope))
        self.assertTrue(observed_scope[0])
        cleanup.assert_awaited_once_with(
            "user",
            "session",
            observed_scope[0],
        )

    async def test_durable_run_id_is_the_cleanup_scope(self) -> None:
        @agent_loop._emit_run_cancelled_on_cancellation
        async def one_event_stream(
            user_id="default",
            session_id="default",
            run_id=None,
            _browser_run_scope_id=None,
            **_kwargs,
        ):
            yield {"type": "done", "finish_reason": "stop"}

        cleanup = AsyncMock()
        with patch("tools.browser.close_browser_run", cleanup):
            _ = [
                event
                async for event in one_event_stream(
                    user_id="user",
                    session_id="session",
                    run_id="durable-run",
                )
            ]
        cleanup.assert_awaited_once_with("user", "session", "durable-run")


if __name__ == "__main__":
    unittest.main()
