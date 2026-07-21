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
    async def test_buffered_material_progress_renews_without_outer_yields(self):
        loop = asyncio.get_running_loop()
        plan = _short_plan()
        lease = MaterialProgressLease.start(plan, now=loop.time())

        async def buffered_provider():
            # Model a delegated transactional turn: provider fragments arrive,
            # but no content crosses the outer iterator until the turn closes.
            for _ in range(6):
                await asyncio.sleep(0.015)
                lease.observe_material_progress(4, now=loop.time())
            yield {"terminal": True}

        values = [
            value
            async for value in _aiter_with_timeout(
                buffered_provider(),
                timeout_seconds=plan.planned_deadline_seconds,
                material_progress_lease=lease,
            )
        ]

        self.assertEqual([{"terminal": True}], values)
        self.assertGreater(lease.renewal_count, 0)
        self.assertGreater(loop.time() - lease.started_at, plan.initial_lease_seconds)

    async def test_empty_frames_do_not_renew_initial_lease(self):
        loop = asyncio.get_running_loop()
        plan = _short_plan(initial=0.02, grace=0.04)
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
