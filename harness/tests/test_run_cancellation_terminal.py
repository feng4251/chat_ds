import asyncio
import inspect
import unittest
from unittest.mock import patch

import agent_loop
from tools.context import ToolContext
from tools import delegation


def _event(event_type: str, run_id: str, seq: int, payload=None) -> dict:
    return {
        "type": "agent_event",
        "event_type": event_type,
        "run_id": run_id,
        "root_run_id": run_id,
        "parent_run_id": None,
        "agent_kind": "primary",
        "agent_name": "primary",
        "depth": 0,
        "workspace_scope": "shared_session",
        "seq": seq,
        "payload": payload or {},
    }


class RunCancellationTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_writes_terminal_before_sink_and_reraises(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []
        trace: list[dict] = []
        sink_events: list[dict] = []

        async def failing_sink(event: dict) -> None:
            order.append("sink")
            sink_events.append(event)
            raise RuntimeError("disconnected sink")

        @agent_loop._emit_run_cancelled_on_cancellation
        async def fake_stream(
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
        ):
            yield _event("run.started", run_id, 1)
            yield _event(
                "debug.llm.finish",
                run_id,
                2,
                {
                    "api_usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 7,
                        "total_tokens": 18,
                    }
                },
            )
            entered.set()
            await release.wait()

        async def consume() -> None:
            async for _event_value in fake_stream(
                "model",
                [],
                user_id="user",
                session_id="session",
                run_id="root-run",
                root_run_id="root-run",
                event_sink=failing_sink,
            ):
                pass

        def append_trace(_user_id, _session_id, event):
            order.append("trace")
            trace.append(event)

        with (
            patch.object(agent_loop.settings, "agent_debug_trace", True),
            patch.object(
                agent_loop,
                "_append_workspace_debug_event",
                side_effect=append_trace,
            ),
        ):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(order, ["trace", "sink"])
        self.assertEqual(len(trace), 1)
        self.assertEqual(len(sink_events), 1)
        cancelled = trace[0]
        self.assertEqual(cancelled["event_type"], "run.cancelled")
        self.assertEqual(cancelled["seq"], 3)
        self.assertEqual(
            cancelled["payload"],
            {
                "finish_reason": "task_cancelled",
                "terminal_reason": "task_cancelled",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                },
            },
        )

    async def test_terminal_vs_cancel_race_does_not_add_second_terminal(self):
        terminal_yielded = asyncio.Event()
        release = asyncio.Event()
        trace: list[dict] = []

        @agent_loop._emit_run_cancelled_on_cancellation
        async def terminal_then_wait(
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
        ):
            yield _event("run.completed", run_id, 1)
            terminal_yielded.set()
            await release.wait()

        async def consume() -> None:
            async for _event_value in terminal_then_wait(
                "model",
                [],
                user_id="user",
                session_id="session",
                run_id="root-run",
            ):
                pass

        with (
            patch.object(agent_loop.settings, "agent_debug_trace", True),
            patch.object(
                agent_loop,
                "_append_workspace_debug_event",
                side_effect=lambda _u, _s, event: trace.append(event),
            ),
        ):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(terminal_yielded.wait(), timeout=1)
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(trace, [])

    async def test_explicit_generator_close_records_cancelled_terminal(self):
        trace: list[dict] = []
        sink_events: list[dict] = []

        async def event_sink(event: dict) -> None:
            sink_events.append(event)

        @agent_loop._emit_run_cancelled_on_cancellation
        async def closeable_stream(
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
        ):
            yield _event("run.started", run_id, 1)
            await asyncio.Event().wait()

        stream = closeable_stream(
            "model",
            [],
            user_id="user",
            session_id="session",
            run_id="root-run",
            root_run_id="root-run",
            event_sink=event_sink,
        )
        started = await anext(stream)
        self.assertEqual(started["event_type"], "run.started")
        with (
            patch.object(agent_loop.settings, "agent_debug_trace", True),
            patch.object(
                agent_loop,
                "_append_workspace_debug_event",
                side_effect=lambda _u, _s, event: trace.append(event),
            ),
        ):
            await stream.aclose()

        self.assertEqual(
            [event["event_type"] for event in trace],
            ["run.cancelled"],
        )
        self.assertEqual(
            [event["event_type"] for event in sink_events],
            ["run.cancelled"],
        )
        self.assertEqual(trace[0]["seq"], 2)

    async def test_explicit_close_after_completion_is_not_cancelled(self):
        trace: list[dict] = []

        @agent_loop._emit_run_cancelled_on_cancellation
        async def completed_stream(
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
        ):
            yield _event("run.completed", run_id, 1)
            await asyncio.Event().wait()

        stream = completed_stream(
            "model",
            [],
            user_id="user",
            session_id="session",
            run_id="root-run",
        )
        terminal = await anext(stream)
        self.assertEqual(terminal["event_type"], "run.completed")
        with (
            patch.object(agent_loop.settings, "agent_debug_trace", True),
            patch.object(
                agent_loop,
                "_append_workspace_debug_event",
                side_effect=lambda _u, _s, event: trace.append(event),
            ),
        ):
            await stream.aclose()
        self.assertEqual(trace, [])

    async def test_provider_pump_is_closed_before_cancel_returns(self):
        entered = asyncio.Event()
        provider_closed = asyncio.Event()

        async def provider_iterator():
            try:
                entered.set()
                await asyncio.Event().wait()
                yield {"content": "unreachable"}
            finally:
                # Model an async httpx response-context exit rather than a
                # synchronous flag assignment.
                await asyncio.sleep(0)
                provider_closed.set()

        async def consume() -> None:
            async for _item in agent_loop._aiter_with_timeout(
                provider_iterator(),
                timeout_seconds=60,
            ):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(
            provider_closed.is_set(),
            "active provider iterator outlived the cancelled run",
        )

    async def test_cancel_while_normal_terminal_sink_is_blocked_records_cancel(self):
        sink_entered = asyncio.Event()
        release_sink = asyncio.Event()
        trace: list[dict] = []
        sink_events: list[dict] = []

        async def blocking_sink(event: dict) -> None:
            sink_events.append(event)
            if event["event_type"] == "run.completed":
                sink_entered.set()
                await release_sink.wait()

        @agent_loop._emit_run_cancelled_on_cancellation
        async def terminal_at_sink(
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
        ):
            completed = _event("run.completed", run_id, 1)
            await event_sink(completed)
            # This models emit_agent_event's lifecycle append point: it must
            # never be reached if cancellation wins at the sink await.
            agent_loop._append_workspace_debug_event(
                user_id,
                session_id,
                completed,
            )
            yield completed

        async def consume() -> None:
            async for _event_value in terminal_at_sink(
                "model",
                [],
                user_id="user",
                session_id="session",
                run_id="root-run",
                event_sink=blocking_sink,
            ):
                pass

        with (
            patch.object(agent_loop.settings, "agent_debug_trace", True),
            patch.object(
                agent_loop,
                "_append_workspace_debug_event",
                side_effect=lambda _u, _s, event: trace.append(event),
            ),
        ):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(sink_entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(
            [event["event_type"] for event in trace],
            ["run.cancelled"],
        )
        self.assertEqual(
            [event["event_type"] for event in sink_events],
            ["run.completed", "run.cancelled"],
        )

    async def test_parent_cancel_records_only_active_preload_child(self):
        preload_entered = asyncio.Event()
        release_preload = asyncio.Event()
        completed_children: list[int] = []
        trace: list[dict] = []
        sink_events: list[dict] = []

        async def event_sink(event: dict) -> None:
            sink_events.append(event)

        async def fake_child(
            task,
            context,
            index,
            *,
            parallel_child=False,
            child_run_id=None,
        ):
            if index == 0:
                completed_children.append(index)
                return {"index": index, "status": "completed"}
            preload_entered.set()
            await release_preload.wait()
            return {"index": index, "status": "completed"}

        context = ToolContext(
            user_id="user",
            session_id="session",
            model_id="model",
            provider_config={
                "base_url": "http://example",
                "api_model": "model",
                "context_length": 303_872,
            },
            enabled_tools=("read_file",),
            run_id="root-run",
            root_run_id="root-run",
            event_sink=event_sink,
        )

        with (
            patch.object(delegation, "_run_child", side_effect=fake_child),
            patch.object(delegation.settings, "agent_debug_trace", True),
            patch.object(
                agent_loop,
                "_append_workspace_debug_event",
                side_effect=lambda _u, _s, event: trace.append(event),
            ),
        ):
            parent = asyncio.create_task(delegation.delegate_task(
                tasks=[
                    {"goal": "already complete", "tools": ["read_file"]},
                    {"goal": "preload waits", "tools": ["read_file"]},
                ],
                context=context,
            ))
            await asyncio.wait_for(preload_entered.wait(), timeout=1)
            await asyncio.sleep(0)
            parent.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await parent

        self.assertEqual(completed_children, [0])
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["event_type"], "run.cancelled")
        self.assertEqual(trace[0]["payload"]["terminal_reason"], "task_cancelled")
        self.assertEqual(
            [
                event["event_type"]
                for event in sink_events
                if event["event_type"].startswith("run.")
            ],
            ["run.cancelled"],
        )

    async def test_inner_stream_cancel_is_forwarded_once(self):
        stream_entered = asyncio.Event()
        release_stream = asyncio.Event()
        trace: list[dict] = []
        sink_events: list[dict] = []

        async def event_sink(event: dict) -> None:
            sink_events.append(event)

        @agent_loop._emit_run_cancelled_on_cancellation
        async def blocked_run_stream(
            model_id,
            messages,
            enabled_tools=None,
            user_id="default",
            session_id="default",
            timeout=600.0,
            max_iterations=60,
            max_tokens=None,
            provider_override=None,
            fallback_overrides=None,
            source="chat",
            enabled_user_skills=None,
            run_id=None,
            root_run_id=None,
            parent_run_id=None,
            agent_kind="primary",
            agent_name=None,
            depth=0,
            workspace_scope="shared_session",
            event_schema="chatds.agent.v2",
            event_sink=None,
            **_kwargs,
        ):
            stream_entered.set()
            await release_stream.wait()
            if False:  # pragma: no cover - keeps this an async generator
                yield {}

        context = ToolContext(
            user_id="user",
            session_id="session",
            model_id="model",
            provider_config={
                "base_url": "http://example",
                "api_model": "model",
                "context_length": 303_872,
            },
            enabled_tools=("read_file",),
            run_id="root-run",
            root_run_id="root-run",
            event_sink=event_sink,
        )
        tracker = delegation._ChildDispatchReceiptTracker()

        with (
            patch.object(agent_loop, "run_stream", blocked_run_stream),
            patch.object(agent_loop.settings, "agent_debug_trace", True),
            patch.object(delegation.settings, "agent_debug_trace", True),
            patch.object(
                agent_loop,
                "_append_workspace_debug_event",
                side_effect=lambda _u, _s, event: trace.append(event),
            ),
        ):
            child = asyncio.create_task(
                delegation._run_child_with_dispatch_receipts(
                    {
                        "goal": "wait in provider stream",
                        "tools": ["read_file"],
                    },
                    context,
                    0,
                    tracker,
                )
            )
            await asyncio.wait_for(stream_entered.wait(), timeout=1)
            child.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await child

        trace_cancelled = [
            event for event in trace
            if event.get("event_type") == "run.cancelled"
        ]
        sink_cancelled = [
            event for event in sink_events
            if event.get("event_type") == "run.cancelled"
        ]
        self.assertEqual(len(trace_cancelled), 1)
        self.assertEqual(len(sink_cancelled), 1)
        self.assertEqual(
            trace_cancelled[0]["run_id"],
            sink_cancelled[0]["run_id"],
        )
        self.assertTrue(sink_cancelled[0]["payload"]["authoritative"])

    async def test_root_cancel_cascades_to_three_active_children_once(self):
        all_children_entered = asyncio.Event()
        entered_children = 0
        trace: list[dict] = []
        sink_events: list[dict] = []

        async def event_sink(event: dict) -> None:
            sink_events.append(event)

        async def blocked_child(
            task,
            context,
            index,
            *,
            parallel_child=False,
        ):
            nonlocal entered_children
            entered_children += 1
            if entered_children == 3:
                all_children_entered.set()
            await asyncio.Event().wait()
            return {"index": index, "status": "completed"}

        @agent_loop._emit_run_cancelled_on_cancellation
        async def root_stream(
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
        ):
            context = ToolContext(
                user_id=user_id,
                session_id=session_id,
                model_id=model_id,
                provider_config={
                    "base_url": "http://provider",
                    "api_model": model_id,
                    "context_length": 303_872,
                },
                enabled_tools=("read_file",),
                run_id=run_id,
                root_run_id=root_run_id or run_id,
                event_sink=event_sink,
            )
            await delegation.delegate_task(
                tasks=[
                    {"goal": f"wait-{index}", "tools": ["read_file"]}
                    for index in range(3)
                ],
                context=context,
            )
            if False:  # pragma: no cover - keep this an async generator
                yield {}

        async def consume() -> None:
            async for _event_value in root_stream(
                "model",
                [],
                user_id="user",
                session_id="session",
                run_id="root-run",
                root_run_id="root-run",
                event_sink=event_sink,
            ):
                pass

        with (
            patch.object(delegation, "_run_child", side_effect=blocked_child),
            patch.object(agent_loop.settings, "agent_debug_trace", True),
            patch.object(delegation.settings, "agent_debug_trace", True),
            patch.object(
                agent_loop,
                "_append_workspace_debug_event",
                side_effect=lambda _u, _s, event: trace.append(event),
            ),
        ):
            root_task = asyncio.create_task(consume())
            await asyncio.wait_for(all_children_entered.wait(), timeout=1)
            root_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await root_task

        cancelled = [
            event for event in trace
            if event.get("event_type") == "run.cancelled"
        ]
        self.assertEqual(len(cancelled), 4)
        by_run = {str(event.get("run_id")): event for event in cancelled}
        self.assertEqual(len(by_run), 4)
        self.assertIn("root-run", by_run)
        child_events = [
            event for run, event in by_run.items() if run != "root-run"
        ]
        self.assertEqual(len(child_events), 3)
        self.assertTrue(all(
            event.get("parent_run_id") == "root-run"
            and event.get("root_run_id") == "root-run"
            and event.get("agent_kind") == "delegate"
            for event in child_events
        ))
        sink_cancelled = [
            event for event in sink_events
            if event.get("event_type") == "run.cancelled"
        ]
        self.assertEqual(
            {event["run_id"] for event in sink_cancelled},
            set(by_run),
        )
        self.assertEqual(len(sink_cancelled), 4)

    def test_public_run_stream_signature_is_preserved(self):
        parameters = inspect.signature(agent_loop.run_stream).parameters
        self.assertIn("run_id", parameters)
        self.assertIn("event_sink", parameters)
        self.assertTrue(hasattr(agent_loop.run_stream, "__wrapped__"))


if __name__ == "__main__":
    unittest.main()
