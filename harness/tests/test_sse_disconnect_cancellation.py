import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import agent_loop
import main as harness_main


class SSEDisconnectCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        agent_loop.set_harness_service_shutdown_started(False)

    async def test_asgi_disconnect_cancels_run_producer_and_records_terminal(self):
        entered = asyncio.Event()
        producer_closed = asyncio.Event()
        trace: list[dict] = []
        observed_skill_registry = None

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
            session_skill_registry=None,
            **_kwargs,
        ):
            nonlocal observed_skill_registry
            observed_skill_registry = session_skill_registry
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
            session_skill_registry=[{
                "name": "root",
                "scope": "session",
                "bundle_id": "a" * 64,
                "bundle_role": "primary",
                "bundle_root_name": "root",
            }],
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
        self.assertEqual("root", observed_skill_registry[0]["name"])
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

    @staticmethod
    async def _response_text(response) -> str:
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(
                chunk.decode("utf-8")
                if isinstance(chunk, bytes)
                else str(chunk)
            )
        return "".join(chunks)

    @staticmethod
    def _agent_events(stream_text: str) -> list[dict]:
        events: list[dict] = []
        for line in stream_text.splitlines():
            if not line.startswith("data: {"):
                continue
            payload = json.loads(line[len("data: "):])
            delta = payload["choices"][0]["delta"]
            if isinstance(delta.get("agent_event"), dict):
                events.append(delta["agent_event"])
        return events

    async def test_wrapped_internal_exception_emits_one_safe_authoritative_terminal(self):
        trace: list[dict] = []

        @agent_loop._emit_run_cancelled_on_cancellation
        async def broken_run_stream(
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
            source="chat",
            event_sink=None,
            **_kwargs,
        ):
            started = {
                "type": "agent_event",
                "event_type": "run.started",
                "run_id": run_id,
                "root_run_id": root_run_id or run_id,
                "parent_run_id": parent_run_id,
                "agent_kind": agent_kind,
                "agent_name": agent_name or agent_kind,
                "depth": depth,
                "workspace_scope": workspace_scope,
                "seq": 1,
                "payload": {},
            }
            if event_sink is not None:
                await event_sink(started)
            yield started
            raise RuntimeError("SECRET provider payload must never escape")

        response = harness_main._streaming_response(
            "model",
            [{"role": "user", "content": "run"}],
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
        with (
            patch.object(harness_main, "run_stream", broken_run_stream),
            patch.object(agent_loop.settings, "agent_debug_trace", True),
            patch.object(
                agent_loop,
                "_append_workspace_debug_event",
                side_effect=lambda _u, _s, event: trace.append(event),
            ),
            patch(
                "tools.browser.close_browser_run",
                AsyncMock(return_value=None),
            ),
            patch(
                "tools.skill_process.cleanup_skill_process_root",
                AsyncMock(return_value=None),
            ),
        ):
            body = await self._response_text(response)

        failed = [
            event for event in self._agent_events(body)
            if event.get("event_type") == "run.failed"
        ]
        self.assertEqual(1, len(failed))
        self.assertTrue(failed[0]["payload"]["authoritative"])
        self.assertEqual(
            "unhandled_harness_exception",
            failed[0]["payload"]["terminal_reason"],
        )
        self.assertEqual("RuntimeError", failed[0]["payload"]["exception_class"])
        self.assertEqual(
            1,
            sum(event.get("event_type") == "run.failed" for event in trace),
        )
        self.assertIn("data: [DONE]", body)
        self.assertNotIn("SECRET", body)

    async def test_wrapped_normal_eof_emits_missing_terminal_failure(self):
        trace: list[dict] = []

        @agent_loop._emit_run_cancelled_on_cancellation
        async def early_return_run_stream(
            model_id,
            messages,
            run_id=None,
            root_run_id=None,
            parent_run_id=None,
            agent_kind="primary",
            agent_name=None,
            depth=0,
            workspace_scope="shared_session",
            source="chat",
            event_sink=None,
            **_kwargs,
        ):
            started = {
                "type": "agent_event",
                "event_type": "run.started",
                "run_id": run_id,
                "root_run_id": root_run_id or run_id,
                "parent_run_id": parent_run_id,
                "agent_kind": agent_kind,
                "agent_name": agent_name or agent_kind,
                "depth": depth,
                "workspace_scope": workspace_scope,
                "seq": 1,
                "payload": {},
            }
            if event_sink is not None:
                await event_sink(started)
            yield started
            return

        with (
            patch.object(agent_loop.settings, "agent_debug_trace", True),
            patch.object(
                agent_loop,
                "_append_workspace_debug_event",
                side_effect=lambda _u, _s, event: trace.append(event),
            ),
            patch(
                "tools.browser.close_browser_run",
                AsyncMock(return_value=None),
            ),
            patch(
                "tools.skill_process.cleanup_skill_process_root",
                AsyncMock(return_value=None),
            ),
        ):
            events = [
                event
                async for event in early_return_run_stream(
                    "model",
                    [{"role": "user", "content": "run"}],
                    run_id="eof-run",
                    root_run_id="eof-run",
                )
            ]

        failed = [
            event for event in events
            if event.get("event_type") == "run.failed"
        ]
        self.assertEqual(1, len(failed))
        self.assertEqual(2, failed[0]["seq"])
        self.assertEqual(
            "missing_terminal_event",
            failed[0]["payload"]["terminal_reason"],
        )
        self.assertEqual(
            "harness_lifecycle_error",
            failed[0]["payload"]["failure_class"],
        )
        self.assertEqual(
            1,
            sum(event.get("event_type") == "run.failed" for event in trace),
        )

    async def test_wrapped_pre_start_empty_return_preserves_compatibility(self):
        @agent_loop._emit_run_cancelled_on_cancellation
        async def empty_run_stream(
            model_id,
            messages,
            **_kwargs,
        ):
            if False:  # pragma: no cover - retain async-generator shape
                yield {}
            return

        with (
            patch(
                "tools.browser.close_browser_run",
                AsyncMock(return_value=None),
            ),
            patch(
                "tools.skill_process.cleanup_skill_process_root",
                AsyncMock(return_value=None),
            ),
        ):
            events = [
                event
                async for event in empty_run_stream(
                    "model",
                    [{"role": "user", "content": "compat"}],
                )
            ]

        self.assertEqual([], events)

    async def test_sse_producer_fallback_converts_unwrapped_exception_to_terminal(self):
        async def unwrapped_broken_run_stream(
            model_id,
            messages,
            enabled_tools=None,
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
            started = {
                "type": "agent_event",
                "event_type": "run.started",
                "run_id": run_id,
                "root_run_id": root_run_id or run_id,
                "parent_run_id": parent_run_id,
                "agent_kind": agent_kind,
                "agent_name": agent_name or agent_kind,
                "depth": depth,
                "workspace_scope": workspace_scope,
                "seq": 4,
                "payload": {},
            }
            if event_sink is not None:
                await event_sink(started)
            yield started
            raise NameError("loaded_packages should not leak to the client")

        response = harness_main._streaming_response(
            "model",
            [{"role": "user", "content": "run"}],
            [],
            "user",
            "session",
            {"base_url": "http://provider", "api_model": "model"},
            [],
            "chat",
            run_metadata={
                "run_id": "fallback-run",
                "root_run_id": "fallback-run",
                "agent_kind": "primary",
            },
            event_schema="chatds.agent.v2",
        )
        with patch.object(
            harness_main,
            "run_stream",
            unwrapped_broken_run_stream,
        ):
            body = await self._response_text(response)

        failed = [
            event for event in self._agent_events(body)
            if event.get("event_type") == "run.failed"
        ]
        self.assertEqual(1, len(failed))
        self.assertEqual(5, failed[0]["seq"])
        self.assertTrue(failed[0]["payload"]["authoritative"])
        self.assertEqual("NameError", failed[0]["payload"]["exception_class"])
        self.assertIn("data: [DONE]", body)
        self.assertNotIn("loaded_packages", body)

    async def test_sse_producer_fallback_converts_unwrapped_normal_eof(self):
        async def unwrapped_early_return_run_stream(
            model_id,
            messages,
            enabled_tools=None,
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
            started = {
                "type": "agent_event",
                "event_type": "run.started",
                "run_id": run_id,
                "root_run_id": root_run_id or run_id,
                "parent_run_id": parent_run_id,
                "agent_kind": agent_kind,
                "agent_name": agent_name or agent_kind,
                "depth": depth,
                "workspace_scope": workspace_scope,
                "seq": 7,
                "payload": {},
            }
            if event_sink is not None:
                await event_sink(started)
            yield started
            return

        response = harness_main._streaming_response(
            "model",
            [{"role": "user", "content": "run"}],
            [],
            "user",
            "session",
            {"base_url": "http://provider", "api_model": "model"},
            [],
            "chat",
            run_metadata={
                "run_id": "fallback-eof-run",
                "root_run_id": "fallback-eof-run",
                "agent_kind": "primary",
            },
            event_schema="chatds.agent.v2",
        )
        with patch.object(
            harness_main,
            "run_stream",
            unwrapped_early_return_run_stream,
        ):
            body = await self._response_text(response)

        failed = [
            event for event in self._agent_events(body)
            if event.get("event_type") == "run.failed"
        ]
        self.assertEqual(1, len(failed))
        self.assertEqual(8, failed[0]["seq"])
        self.assertEqual(
            "missing_terminal_event",
            failed[0]["payload"]["terminal_reason"],
        )
        self.assertEqual(
            "harness_lifecycle_error",
            failed[0]["payload"]["failure_class"],
        )
        self.assertIn("data: [DONE]", body)

    async def test_wrapper_injects_one_identity_when_caller_omits_metadata(self):
        @agent_loop._emit_run_cancelled_on_cancellation
        async def identity_probe(
            model_id,
            messages,
            run_id=None,
            root_run_id=None,
            _browser_run_scope_id=None,
            **_kwargs,
        ):
            self.assertTrue(run_id)
            self.assertEqual(run_id, root_run_id)
            self.assertEqual(run_id, _browser_run_scope_id)
            yield {
                "type": "agent_event",
                "event_type": "run.started",
                "run_id": run_id,
                "root_run_id": root_run_id,
                "parent_run_id": None,
                "agent_kind": "primary",
                "agent_name": "primary",
                "depth": 0,
                "workspace_scope": "shared_session",
                "seq": 1,
                "payload": {},
            }
            raise RuntimeError("private failure text")

        with (
            patch(
                "tools.browser.close_browser_run",
                AsyncMock(return_value=None),
            ),
            patch(
                "tools.skill_process.cleanup_skill_process_root",
                AsyncMock(return_value=None),
            ),
        ):
            events = [
                event
                async for event in identity_probe(
                    "model",
                    [{"role": "user", "content": "run"}],
                )
            ]

        lifecycle = [
            event for event in events
            if event.get("type") == "agent_event"
        ]
        self.assertEqual(
            ["run.started", "run.failed"],
            [event["event_type"] for event in lifecycle],
        )
        self.assertEqual(
            1,
            len({event["run_id"] for event in lifecycle}),
        )
        self.assertEqual(2, lifecycle[-1]["seq"])
        self.assertTrue(lifecycle[-1]["payload"]["failure_stack"])
        self.assertNotIn("private failure text", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
