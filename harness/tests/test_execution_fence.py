from __future__ import annotations

import asyncio
import unittest

from tools.execution_fence import ChildExecutionFence


class ExecutionFenceResourceRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_closer_is_retained_for_exact_retry(self) -> None:
        fence = ChildExecutionFence()
        calls = 0

        async def closer() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("first attempt failed")

        fence.register_resource(
            fence.generation,
            label="lease",
            closer=closer,
        )
        fence.revoke("test")

        first = await fence.close_registered_resources(
            grace_seconds=0.1,
        )
        second = await fence.close_registered_resources(
            grace_seconds=0.1,
        )
        empty = await fence.close_registered_resources(
            grace_seconds=0.1,
        )

        self.assertEqual(1, first.resource_count)
        self.assertEqual(0, first.acknowledged_resource_count)
        self.assertEqual(1, first.unacknowledged_resource_count)
        self.assertEqual(1, second.resource_count)
        self.assertEqual(1, second.acknowledged_resource_count)
        self.assertEqual(0, second.unacknowledged_resource_count)
        self.assertEqual(0, empty.resource_count)
        self.assertEqual(2, calls)

    async def test_timed_out_cooperative_closer_is_retained_for_retry(
        self,
    ) -> None:
        fence = ChildExecutionFence()
        calls = 0

        async def closer() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()

        fence.register_resource(
            fence.generation,
            label="browser",
            closer=closer,
        )
        fence.revoke("test")

        first = await fence.close_registered_resources(
            grace_seconds=0.001,
        )
        second = await fence.close_registered_resources(
            grace_seconds=0.1,
        )

        self.assertEqual(1, first.resource_count)
        self.assertEqual(0, first.acknowledged_resource_count)
        self.assertEqual(1, first.unacknowledged_resource_count)
        self.assertEqual(1, second.resource_count)
        self.assertEqual(1, second.acknowledged_resource_count)
        self.assertEqual(2, calls)

    async def test_concurrent_close_calls_share_one_runtime_attempt(
        self,
    ) -> None:
        fence = ChildExecutionFence()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def closer() -> None:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()

        fence.register_resource(
            fence.generation,
            label="process",
            closer=closer,
        )
        fence.revoke("test")

        first_task = asyncio.create_task(
            fence.close_registered_resources(grace_seconds=1.0)
        )
        await entered.wait()
        second_task = asyncio.create_task(
            fence.close_registered_resources(grace_seconds=1.0)
        )
        await asyncio.sleep(0)
        release.set()
        first, second = await asyncio.gather(first_task, second_task)

        self.assertEqual(1, calls)
        self.assertEqual(1, first.resource_count)
        self.assertEqual(1, second.resource_count)
        self.assertEqual(1, first.acknowledged_resource_count)
        self.assertEqual(1, second.acknowledged_resource_count)
        self.assertEqual(0, first.unacknowledged_resource_count)
        self.assertEqual(0, second.unacknowledged_resource_count)

    async def test_cancelled_waiter_does_not_add_a_spurious_retry_hop(
        self,
    ) -> None:
        fence = ChildExecutionFence()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def closer() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("failed after waiter cancellation")

        fence.register_resource(
            fence.generation,
            label="lease",
            closer=closer,
        )
        fence.revoke("test")
        abandoned = asyncio.create_task(
            fence.close_registered_resources(grace_seconds=1.0)
        )
        await entered.wait()
        abandoned.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await abandoned
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        retried = await fence.close_registered_resources(
            grace_seconds=0.1,
        )

        self.assertEqual(2, calls)
        self.assertEqual(1, retried.resource_count)
        self.assertEqual(1, retried.acknowledged_resource_count)
        self.assertEqual(0, retried.unacknowledged_resource_count)


if __name__ == "__main__":
    unittest.main()
