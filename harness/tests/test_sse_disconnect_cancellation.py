import asyncio
import unittest
from unittest.mock import patch

import agent_loop
import main as harness_main


class SSEDisconnectCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_asgi_disconnect_cancels_run_producer_and_records_terminal(self):
        entered = asyncio.Event()
        producer_closed = asyncio.Event()
        trace: list[dict] = []

        @agent_loop._emit_run_cancelled_on_cancellation
        async def blocked_run_stream(
            model_id,
            messages,
            enabled_tools=None,
            user_id="default",
            session_id="default",
            run_id=None,
            root_run_id=None,
            parent_run_id=None,
            agent_kind="primary",
            agent_name=None,
            depth=0,
            workspace_scope="shared_session",
            event_sink=None,
            **_kwargs,
        ):
            try:
                entered.set()
                await asyncio.Event().wait()
                if False:  # pragma: no cover - keep this an async generator
                    yield {}
            finally:
                producer_closed.set()

        response = harness_main._streaming_response(
            "model",
            [{"role": "user", "content": "wait"}],
            [],
            "user",
            "session",
            {"base_url": "http://provider", "api_model": "model"},
            [],
            "chat",
            run_metadata={
                "run_id": "root-run",
                "root_run_id": "root-run",
                "agent_kind": "primary",
            },
            event_schema="chatds.agent.v2",
        )

        receive_count = 0

        async def receive() -> dict:
            nonlocal receive_count
            receive_count += 1
            await asyncio.wait_for(entered.wait(), timeout=1)
            return {"type": "http.disconnect"}

        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        }

        with (
            patch.object(harness_main, "run_stream", blocked_run_stream),
            patch.object(agent_loop.settings, "agent_debug_trace", True),
            patch.object(
                agent_loop,
                "_append_workspace_debug_event",
                side_effect=lambda _u, _s, event: trace.append(event),
            ),
        ):
            await asyncio.wait_for(response(scope, receive, send), timeout=2)

        self.assertGreaterEqual(receive_count, 1)
        self.assertTrue(producer_closed.is_set())
        cancelled = [
            event for event in trace
            if event.get("event_type") == "run.cancelled"
        ]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["run_id"], "root-run")
        self.assertEqual(
            cancelled[0]["payload"]["terminal_reason"],
            "task_cancelled",
        )
        self.assertTrue(
            any(message.get("type") == "http.response.start" for message in sent)
        )


if __name__ == "__main__":
    unittest.main()
