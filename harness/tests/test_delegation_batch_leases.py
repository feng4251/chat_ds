import asyncio
import json
import unittest
from unittest.mock import patch

import agent_loop
from agent_loop import _tool_outcome_is_completed, _tool_outcome_summary
from tools.context import ToolContext
from tools.delegation import (
    _ACTIVE_CHILD_DISPATCH_RECEIPTS,
    _semantic_agent_name,
    delegate_task,
)
from tools.execution_fence import ChildExecutionFence
from tools.registry import dispatch as registry_dispatch, registry


def _context(*, event_sink=None) -> ToolContext:
    return ToolContext(
        user_id="u",
        session_id="s",
        model_id="model",
        provider_config={
            "base_url": "http://example",
            "api_model": "model",
            "context_length": 303_872,
        },
        enabled_tools=("read_file",),
        run_id="parent",
        root_run_id="root",
        event_sink=event_sink,
        tool_operation_id="6ac9f610-8694-4df4-b44b-501777d6b75a",
    )


class DelegationSemanticIdentityTests(unittest.TestCase):
    def test_declared_semantics_replace_scheduler_fallback(self):
        self.assertEqual(
            "Safety evidence reviewer",
            _semantic_agent_name(
                {
                    "agent_name": "delegate-2",
                    "role": "Safety evidence reviewer",
                    "worker_id": "worker-safety",
                    "step_id": "safety",
                },
                1,
            ),
        )
        self.assertEqual(
            "worker-safety",
            _semantic_agent_name({"worker_id": "worker-safety"}, 0),
        )
        self.assertEqual(
            "conflict-resolution",
            _semantic_agent_name({"step_id": "conflict-resolution"}, 4),
        )
        self.assertEqual(
            "Delegated task",
            _semantic_agent_name({}, 8),
        )

    def test_execution_fence_rejects_synchronous_resource_closer(self):
        fence = ChildExecutionFence()
        with self.assertRaisesRegex(TypeError, "async callable"):
            fence.register_resource(
                fence.generation,
                label="must-not-block-event-loop",
                closer=lambda: None,
            )


class DelegationProgressLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_material_progress_renews_soft_lease(self):
        async def progressing_child(
            task,
            context,
            index,
            *,
            parallel_child=False,
        ):
            tracker = _ACTIVE_CHILD_DISPATCH_RECEIPTS.get()
            self.assertIsNotNone(tracker)
            for _ in range(4):
                await asyncio.sleep(0.012)
                tracker.record_runtime_progress("agent.reasoning_delta")
            return {"index": index, "status": "completed"}

        with (
            patch(
                "tools.delegation._run_child",
                side_effect=progressing_child,
            ),
            patch(
                "tools.delegation.settings.delegation_batch_timeout_seconds",
                0.02,
            ),
            patch(
                "tools.delegation.settings.delegation_batch_hard_timeout_seconds",
                0.2,
            ),
        ):
            payload = json.loads(await delegate_task(
                tasks=[{
                    "goal": "long productive work",
                    "worker_id": "productive-worker",
                }],
                context=_context(),
            ))

        self.assertEqual("completed", payload["status"])
        result = payload["results"][0]
        self.assertEqual("productive-worker", result["agent_name"])
        self.assertEqual(1, result["delegation_slot"])
        self.assertEqual(
            "6ac9f610-8694-4df4-b44b-501777d6b75a",
            result["delegation_batch_id"],
        )

    async def test_provider_admission_wait_is_exempt_from_soft_lease(self):
        async def admission_waiting_child(
            task,
            context,
            index,
            *,
            parallel_child=False,
        ):
            tracker = _ACTIVE_CHILD_DISPATCH_RECEIPTS.get()
            self.assertIsNotNone(tracker)
            tracker.record_runtime_progress("provider_admission.queued")
            await asyncio.sleep(0.05)
            tracker.record_runtime_progress("provider_admission.acquired")
            return {"index": index, "status": "completed"}

        with (
            patch(
                "tools.delegation._run_child",
                side_effect=admission_waiting_child,
            ),
            patch(
                "tools.delegation.settings.delegation_batch_timeout_seconds",
                0.01,
            ),
            patch(
                "tools.delegation.settings.delegation_batch_hard_timeout_seconds",
                0.2,
            ),
        ):
            payload = json.loads(await delegate_task(
                goal="wait for provider capacity",
                context=_context(),
            ))

        self.assertEqual("completed", payload["status"])

    async def test_hard_cap_stops_child_despite_continuous_progress(self):
        cancellation_events: list[dict] = []

        async def event_sink(event: dict) -> None:
            if event.get("event_type") == "run.cancelled":
                cancellation_events.append(event)

        async def endless_progress(
            task,
            context,
            index,
            *,
            parallel_child=False,
        ):
            tracker = _ACTIVE_CHILD_DISPATCH_RECEIPTS.get()
            self.assertIsNotNone(tracker)
            while True:
                tracker.record_runtime_progress("agent.reasoning_delta")
                await asyncio.sleep(0.005)

        with (
            patch(
                "tools.delegation._run_child",
                side_effect=endless_progress,
            ),
            patch(
                "tools.delegation.settings.delegation_batch_timeout_seconds",
                0.02,
            ),
            patch(
                "tools.delegation.settings.delegation_batch_hard_timeout_seconds",
                0.05,
            ),
        ):
            payload = json.loads(await delegate_task(
                tasks=[{
                    "goal": "bounded productive work",
                    "step_id": "bounded-worker",
                }],
                context=_context(event_sink=event_sink),
            ))

        result = payload["results"][0]
        self.assertEqual("error", payload["status"])
        self.assertEqual(
            "hard_cap",
            result["delegation_timeout"]["deadline_kind"],
        )
        self.assertEqual(
            "parent_delegate_batch_hard_cap",
            result["delegation_timeout"]["cancellation_source"],
        )
        self.assertEqual(1, len(cancellation_events))
        cancellation = cancellation_events[0]["payload"]
        self.assertEqual(
            "parent_delegate_batch_hard_cap",
            cancellation["cancellation_source"],
        )
        self.assertEqual(
            "delegated_child_timeout",
            cancellation["finish_reason"],
        )
        self.assertEqual("hard_cap", cancellation["deadline_kind"])
        self.assertEqual(
            payload["delegation_batch_id"],
            cancellation["delegation_batch_id"],
        )

    async def test_parent_deadline_attribution_reaches_inner_run_terminal(self):
        stream_entered = asyncio.Event()
        cancellation_events: list[dict] = []

        async def event_sink(event: dict) -> None:
            if event.get("event_type") == "run.cancelled":
                cancellation_events.append(event)

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
            _cancellation_attribution=None,
            _runtime_progress_sink=None,
            **_kwargs,
        ):
            stream_entered.set()
            await asyncio.Event().wait()
            if False:  # pragma: no cover - keep this an async generator
                yield {}

        with (
            patch("agent_loop.run_stream", blocked_run_stream),
            patch(
                "tools.delegation.settings.delegation_batch_timeout_seconds",
                0.02,
            ),
            patch(
                "tools.delegation.settings.delegation_batch_hard_timeout_seconds",
                0.2,
            ),
            patch.object(agent_loop.settings, "agent_debug_trace", False),
        ):
            delegated = asyncio.create_task(delegate_task(
                goal="wait inside the provider stream",
                tools=["read_file"],
                context=_context(event_sink=event_sink),
            ))
            await asyncio.wait_for(stream_entered.wait(), timeout=1)
            payload = json.loads(await delegated)

        self.assertEqual("error", payload["status"])
        self.assertEqual(1, len(cancellation_events))
        cancellation = cancellation_events[0]["payload"]
        self.assertEqual(
            "parent_delegate_batch_soft_no_progress",
            cancellation["cancellation_source"],
        )
        self.assertEqual(
            "delegated_child_timeout",
            cancellation["terminal_reason"],
        )
        self.assertEqual(
            "delegated_child_timeout",
            cancellation["finish_reason"],
        )
        self.assertEqual("soft_no_progress", cancellation["deadline_kind"])
        self.assertTrue(cancellation["retryable"])

    async def test_child_internal_timeout_is_not_a_batch_deadline(self):
        async def internally_timed_out_child(
            task,
            context,
            index,
            *,
            parallel_child=False,
        ):
            raise asyncio.TimeoutError(
                "fixture child-owned operation timed out"
            )

        with (
            patch(
                "tools.delegation._run_child",
                side_effect=internally_timed_out_child,
            ),
            patch(
                "tools.delegation.settings.delegation_batch_timeout_seconds",
                1.0,
            ),
            patch(
                "tools.delegation.settings.delegation_batch_hard_timeout_seconds",
                2.0,
            ),
        ):
            payload = json.loads(await delegate_task(
                tasks=[{
                    "goal": "exercise a child-owned bounded operation",
                    "step_id": "internal-timeout-worker",
                }],
                context=_context(),
            ))

        self.assertEqual("error", payload["status"])
        result = payload["results"][0]
        self.assertNotIn("delegation_timeout", result)
        self.assertEqual(
            "delegated_child_exception",
            result["terminal_reason"],
        )
        self.assertEqual(
            "child_internal_exception",
            result["failure_class"],
        )
        self.assertTrue(result["retryable"])
        self.assertIn("TimeoutError", result["error"])

    async def test_cancel_resistant_child_cannot_dispatch_after_fence_revocation(
        self,
    ):
        second_call_result: dict = {}
        second_handler_calls = 0

        async def forbidden_handler(**_kwargs):
            nonlocal second_handler_calls
            second_handler_calls += 1
            return json.dumps({"status": "unexpected"})

        async def cancellation_resistant_child(
            task,
            context,
            index,
            *,
            parallel_child=False,
        ):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raw = await registry_dispatch(
                    "read_file",
                    {"filepath": "must-not-run.md"},
                    context=context,
                )
                second_call_result.update(json.loads(raw))
                return {"index": index, "status": "completed"}

        read_entry = registry._tools["read_file"]
        original_handler = read_entry.handler
        try:
            read_entry.handler = forbidden_handler
            with (
                patch(
                    "tools.delegation._run_child",
                    side_effect=cancellation_resistant_child,
                ),
                patch(
                    "tools.delegation.settings.delegation_batch_timeout_seconds",
                    1.0,
                ),
                patch(
                    "tools.delegation.settings.delegation_batch_hard_timeout_seconds",
                    0.02,
                ),
                patch(
                    "tools.delegation.settings.delegation_cancellation_grace_seconds",
                    0.05,
                ),
            ):
                payload = json.loads(await delegate_task(
                    goal="attempt stale authority after cancellation",
                    context=_context(),
                ))
        finally:
            read_entry.handler = original_handler

        self.assertEqual(0, second_handler_calls)
        self.assertEqual(
            "execution_authority_revoked",
            second_call_result.get("reason"),
        )
        result = payload["results"][0]
        self.assertEqual("error", result["status"])
        self.assertTrue(
            result["delegation_timeout"]["fence_coverage_proven"]
        )
        self.assertTrue(
            result["delegation_timeout"]["cancellation_acknowledged"]
        )

    async def test_unacknowledged_child_returns_bounded_uncertain_result(self):
        release = asyncio.Event()
        resisted = asyncio.Event()

        async def cancellation_resistant_child(
            task,
            context,
            index,
            *,
            parallel_child=False,
        ):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                resisted.set()
                await release.wait()
                return {"index": index, "status": "completed"}

        try:
            with (
                patch(
                    "tools.delegation._run_child",
                    side_effect=cancellation_resistant_child,
                ),
                patch(
                    "tools.delegation.settings.delegation_batch_timeout_seconds",
                    1.0,
                ),
                patch(
                    "tools.delegation.settings.delegation_batch_hard_timeout_seconds",
                    0.02,
                ),
                patch(
                    "tools.delegation.settings.delegation_cancellation_grace_seconds",
                    0.01,
                ),
            ):
                payload = json.loads(
                    await asyncio.wait_for(
                        delegate_task(
                            goal="resist cancellation beyond teardown grace",
                            context=_context(),
                        ),
                        timeout=0.25,
                    )
                )

            self.assertTrue(resisted.is_set())
            result = payload["results"][0]
            self.assertTrue(result["cancellation_unacknowledged"])
            self.assertTrue(result["side_effect_state_uncertain"])
            self.assertEqual(
                "cancellation_unacknowledged",
                result["terminal_reason"],
            )
            self.assertEqual(
                "side_effect_state_uncertain",
                result["failure_class"],
            )
            self.assertFalse(result["retryable"])
        finally:
            release.set()
            await asyncio.sleep(0)


class DelegationToolOutcomeTests(unittest.TestCase):
    def test_degraded_and_partial_envelopes_are_not_plain_success(self):
        degraded, degraded_detail = _tool_outcome_summary(
            json.dumps({
                "status": "completed_degraded",
                "completed_count": 3,
                "degraded_completed_count": 1,
                "task_count": 3,
            }),
            tool_name="delegate_task",
        )
        partial, partial_detail = _tool_outcome_summary(
            json.dumps({
                "status": "partial",
                "completed_count": 2,
                "task_count": 3,
                "retryable_failed_step_ids": ["third"],
            }),
            tool_name="delegate_task",
        )

        self.assertEqual("degraded", degraded)
        self.assertTrue(_tool_outcome_is_completed(degraded))
        self.assertIn("degraded_completed_count=1", degraded_detail)
        self.assertEqual("partial", partial)
        self.assertFalse(_tool_outcome_is_completed(partial))
        self.assertIn("completed_count=2", partial_detail)


if __name__ == "__main__":
    unittest.main()
