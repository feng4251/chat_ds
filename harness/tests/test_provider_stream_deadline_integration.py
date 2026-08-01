import asyncio
import unittest

from agent_loop import (
    _aiter_with_timeout,
    _provider_material_progress_chars,
)
from provider_stream_deadline import (
    MaterialProgressLease,
    ProviderStreamDeadlineExceeded,
    build_provider_stream_deadline_plan,
)
from tools.context import ToolContext
from tools.execution_fence import (
    ChildExecutionFence,
    ExecutionAuthorityRevoked,
)


def _short_plan(
    *,
    initial: float = 0.03,
    grace: float = 0.04,
    hard: float = 0.20,
    output_tokens: int = 20,
):
    return build_provider_stream_deadline_plan(
        estimated_input_tokens=0,
        max_output_tokens=output_tokens,
        initial_lease_seconds=initial,
        progress_grace_seconds=grace,
        configured_hard_cap_seconds=hard,
        caller_hard_cap_seconds=None,
        input_planning_tokens_per_second=1_000_000.0,
        output_planning_tokens_per_second=100.0,
        planning_safety_factor=1.0,
        fixed_overhead_seconds=0.001,
    )


class ProviderStreamDeadlineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_budget_allows_silent_reasoning_past_floor(self):
        loop = asyncio.get_running_loop()
        plan = _short_plan(
            initial=0.02,
            grace=0.02,
            hard=0.20,
            output_tokens=10,
        )
        lease = MaterialProgressLease.start(plan, now=loop.time())

        async def silent_then_terminal_provider():
            # No transport/material progress arrives before the first result.
            # The request-specific budget (~0.10s) must protect this healthy
            # silent reasoning period from the 0.02s configured floor.
            await asyncio.sleep(0.05)
            yield {"terminal": True}

        values = [
            value
            async for value in _aiter_with_timeout(
                silent_then_terminal_provider(),
                timeout_seconds=plan.planned_deadline_seconds,
                material_progress_lease=lease,
            )
        ]

        self.assertEqual([{"terminal": True}], values)
        self.assertGreater(plan.initial_lease_seconds, 0.05)

    async def test_buffered_material_progress_renews_without_outer_yields(self):
        loop = asyncio.get_running_loop()
        plan = _short_plan(
            initial=0.10,
            grace=0.20,
            hard=1.0,
            output_tokens=1,
        )
        lease = MaterialProgressLease.start(plan, now=loop.time())
        provider_entered = asyncio.Event()
        release_provider = asyncio.Event()

        async def buffered_provider():
            # Model a delegated transactional turn: provider fragments arrive,
            # but no content crosses the outer iterator until the turn closes.
            provider_entered.set()
            await release_provider.wait()
            lease.observe_material_progress(
                4,
                now=(
                    lease.started_at
                    + plan.planned_deadline_seconds
                    + 0.01
                ),
            )
            yield {"terminal": True}

        async def collect():
            return [
                value
                async for value in _aiter_with_timeout(
                    buffered_provider(),
                    # With a material-progress lease this is only the
                    # compatibility fallback.  A second fixed deadline must
                    # not cap the renewable lease.
                    timeout_seconds=0.000_001,
                    material_progress_lease=lease,
                )
            ]

        collect_task = asyncio.create_task(collect())
        await asyncio.wait_for(provider_entered.wait(), timeout=1)
        release_provider.set()
        values = await asyncio.wait_for(collect_task, timeout=1)

        self.assertEqual([{"terminal": True}], values)
        self.assertGreater(lease.renewal_count, 0)
        self.assertGreater(
            lease.deadline,
            lease.started_at + plan.planned_deadline_seconds,
        )

    async def test_without_a_lease_fixed_timeout_remains_authoritative(self):
        provider_closed = asyncio.Event()

        async def blocked_provider():
            try:
                await asyncio.Event().wait()
            finally:
                provider_closed.set()
            yield {"unreachable": True}

        with self.assertRaises(asyncio.TimeoutError):
            async for _ in _aiter_with_timeout(
                blocked_provider(),
                timeout_seconds=0.01,
            ):
                pass

        self.assertTrue(provider_closed.is_set())

    async def test_empty_frames_do_not_renew_initial_lease(self):
        loop = asyncio.get_running_loop()
        plan = _short_plan(initial=0.02, grace=0.04, output_tokens=1)
        lease = MaterialProgressLease.start(plan, now=loop.time())
        provider_closed = asyncio.Event()

        async def empty_provider():
            try:
                while True:
                    # Empty/usage-only provider frames are deliberately not
                    # reported to the material-progress lease.
                    await asyncio.sleep(0.005)
            finally:
                provider_closed.set()
            yield {"unreachable": True}

        with self.assertRaises(ProviderStreamDeadlineExceeded) as raised:
            async for _ in _aiter_with_timeout(
                empty_provider(),
                timeout_seconds=plan.planned_deadline_seconds,
                material_progress_lease=lease,
            ):
                pass

        self.assertTrue(provider_closed.is_set())
        self.assertEqual("initial_lease", raised.exception.metrics["deadline_kind"])
        self.assertEqual(0, raised.exception.metrics["renewal_count"])

    async def test_continuous_progress_still_closes_at_planned_cap(self):
        loop = asyncio.get_running_loop()
        plan = _short_plan(
            initial=0.02,
            grace=0.04,
            hard=0.08,
            output_tokens=100,
        )
        lease = MaterialProgressLease.start(plan, now=loop.time())
        provider_closed = asyncio.Event()

        async def endless_material_provider():
            try:
                while True:
                    lease.observe_material_progress(1, now=loop.time())
                    await asyncio.sleep(0.005)
            finally:
                provider_closed.set()
            yield {"unreachable": True}

        with self.assertRaises(ProviderStreamDeadlineExceeded) as raised:
            async for _ in _aiter_with_timeout(
                endless_material_provider(),
                timeout_seconds=plan.planned_deadline_seconds,
                material_progress_lease=lease,
            ):
                pass

        self.assertTrue(provider_closed.is_set())
        self.assertEqual(
            "configured_or_caller_hard_cap",
            raised.exception.metrics["deadline_kind"],
        )
        self.assertGreater(raised.exception.metrics["material_progress_chars"], 0)

    async def test_revoked_child_cannot_keep_publishing_provider_items(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        provider_closed = asyncio.Event()
        fence = ChildExecutionFence()
        context = ToolContext(
            user_id="u",
            session_id="s",
            model_id="m",
            provider_config={},
            execution_fence=fence,
            execution_fence_generation=fence.generation,
        )

        async def provider():
            try:
                entered.set()
                await release.wait()
                for index in range(10_000):
                    yield {"index": index}
            finally:
                provider_closed.set()

        async def consume():
            async for _ in _aiter_with_timeout(
                provider(),
                timeout_seconds=1,
                execution_context=context,
            ):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), timeout=1)
        fence.revoke("fixture_deadline")
        release.set()
        with self.assertRaises(ExecutionAuthorityRevoked):
            await asyncio.wait_for(task, timeout=0.5)
        self.assertTrue(provider_closed.is_set())

    def test_only_text_reasoning_and_native_tool_strings_are_material(self):
        self.assertEqual(
            9,
            _provider_material_progress_chars(
                content="ab",
                reasoning="x",
                tool_calls=[{
                    "id": "id",
                    "function": {"name": "fn", "arguments": "{}"},
                }],
            ),
        )
        self.assertEqual(
            0,
            _provider_material_progress_chars(
                content="",
                reasoning="",
                tool_calls=[],
            ),
        )


if __name__ == "__main__":
    unittest.main()
