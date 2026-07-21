import asyncio
import unittest

from provider_admission import (
    ProviderAdmissionController,
    ProviderAdmissionLimits,
    ProviderAdmissionTimeout,
    estimate_admission_tokens,
    provider_identity,
)


class ProviderAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.controller = ProviderAdmissionController()

    async def _acquire(
        self,
        input_tokens: int,
        *,
        endpoint: str = "https://provider.example/v1",
        model: str = "model-a",
        output_tokens: int = 0,
        request_limit: int = 3,
        token_limit: int = 100,
        factor: float = 1.0,
        wait_timeout: float = 0.0,
        observer=None,
    ):
        return await self.controller.acquire(
            endpoint=endpoint,
            api_model=model,
            estimated_input_tokens=input_tokens,
            max_output_tokens=output_tokens,
            limits=ProviderAdmissionLimits(
                max_inflight_requests=request_limit,
                max_inflight_estimated_tokens=token_limit,
                estimate_safety_factor=factor,
                wait_timeout_seconds=wait_timeout,
            ),
            observer=observer,
        )

    async def test_weighted_capacity_admits_fitting_requests_then_wakes_fifo(self):
        first = await self._acquire(60)
        second = await self._acquire(40)
        third_task = asyncio.create_task(self._acquire(30))
        await asyncio.sleep(0)
        self.assertFalse(third_task.done())

        await second.release()
        third = await asyncio.wait_for(third_task, timeout=1)
        self.assertTrue(third.enabled)
        await first.release()
        await third.release()

    async def test_count_only_limit_works_without_token_budget(self):
        first = await self._acquire(1, request_limit=1, token_limit=0)
        second_task = asyncio.create_task(
            self._acquire(1, request_limit=1, token_limit=0)
        )
        await asyncio.sleep(0)
        self.assertFalse(second_task.done())
        await first.release()
        second = await asyncio.wait_for(second_task, timeout=1)
        await second.release()

    async def test_no_limits_is_compatibility_noop(self):
        events: list[str] = []

        async def observer(event_type, _payload):
            events.append(event_type)

        leases = await asyncio.gather(*[
            self._acquire(
                1_000_000,
                request_limit=0,
                token_limit=0,
                observer=observer,
            )
            for _ in range(8)
        ])
        self.assertTrue(all(not lease.enabled for lease in leases))
        self.assertEqual(events, [])
        await asyncio.gather(*(lease.release() for lease in leases))

    async def test_fifo_prevents_smaller_later_request_from_bypassing_head(self):
        acquired_tickets: list[int] = []

        async def observer(event_type, payload):
            if event_type == "acquired":
                acquired_tickets.append(payload["ticket"])

        active = await self._acquire(70, observer=observer)
        blocked_large = asyncio.create_task(self._acquire(50, observer=observer))
        await asyncio.sleep(0)
        blocked_small = asyncio.create_task(self._acquire(20, observer=observer))
        await asyncio.sleep(0)
        self.assertFalse(blocked_large.done())
        self.assertFalse(blocked_small.done())

        await active.release()
        large = await asyncio.wait_for(blocked_large, timeout=1)
        small = await asyncio.wait_for(blocked_small, timeout=1)
        self.assertEqual(acquired_tickets, [1, 2, 3])
        await large.release()
        await small.release()

    async def test_cancelled_head_is_removed_and_next_waiter_progresses(self):
        active = await self._acquire(90)
        cancelled = asyncio.create_task(self._acquire(50))
        await asyncio.sleep(0)
        next_task = asyncio.create_task(self._acquire(10))
        await asyncio.sleep(0)
        self.assertFalse(next_task.done())

        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        next_lease = await asyncio.wait_for(next_task, timeout=1)
        await next_lease.release()
        await active.release()

    async def test_active_context_exception_releases_capacity(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            async with await self._acquire(100):
                raise RuntimeError("synthetic")

        lease = await asyncio.wait_for(self._acquire(100), timeout=1)
        await lease.release()

    async def test_oversize_request_waits_for_exclusive_access(self):
        active = await self._acquire(20)
        oversize_task = asyncio.create_task(self._acquire(150))
        await asyncio.sleep(0)
        trailing_small = asyncio.create_task(self._acquire(10))
        await asyncio.sleep(0)
        self.assertFalse(oversize_task.done())
        self.assertFalse(trailing_small.done())

        await active.release()
        oversize = await asyncio.wait_for(oversize_task, timeout=1)
        await asyncio.sleep(0)
        self.assertFalse(trailing_small.done())
        await oversize.release()
        small = await asyncio.wait_for(trailing_small, timeout=1)
        await small.release()

    async def test_different_provider_keys_have_independent_capacity(self):
        first = await self._acquire(
            100,
            endpoint="https://one.example/v1",
            request_limit=1,
        )
        second = await asyncio.wait_for(
            self._acquire(
                100,
                endpoint="https://two.example/v1",
                request_limit=1,
            ),
            timeout=1,
        )
        await first.release()
        await second.release()

    async def test_same_endpoint_different_api_models_are_independent(self):
        first = await self._acquire(100, model="model-a", request_limit=1)
        second = await asyncio.wait_for(
            self._acquire(100, model="model-b", request_limit=1),
            timeout=1,
        )
        await first.release()
        await second.release()

    async def test_wait_timeout_removes_waiter_without_leaking_capacity(self):
        events: list[str] = []

        async def observer(event_type, _payload):
            events.append(event_type)

        active = await self._acquire(100)
        with self.assertRaises(ProviderAdmissionTimeout):
            await self._acquire(1, wait_timeout=0.01, observer=observer)
        self.assertEqual(events, ["queued", "timed_out"])
        await active.release()
        lease = await asyncio.wait_for(self._acquire(100), timeout=1)
        await lease.release()

    async def test_observer_payload_uses_hash_and_endpoint_secrets_are_removed(self):
        payloads: list[dict] = []
        event_types: list[str] = []

        async def observer(event_type, payload):
            event_types.append(event_type)
            payloads.append(payload)

        secret_endpoint = (
            "https://alice:secret@example.com:443/v1/?api_key=hidden#fragment"
        )
        lease = await self._acquire(
            10,
            endpoint=secret_endpoint,
            observer=observer,
        )
        await lease.release()

        serialized = repr(payloads)
        self.assertNotIn("alice", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("hidden", serialized)
        self.assertNotIn("example.com", serialized)
        self.assertEqual(
            provider_identity(secret_endpoint, "model-a").debug_hash,
            provider_identity("https://example.com/v1", "model-a").debug_hash,
        )
        self.assertEqual(
            {"queued", "acquired", "released"},
            set(event_types),
        )
        self.assertTrue(all("provider_key_sha256" in item for item in payloads))

    async def test_safety_factor_and_output_reserve_are_counted(self):
        self.assertEqual(
            estimate_admission_tokens(100, 20, safety_factor=1.10),
            130,
        )
        self.assertEqual(
            estimate_admission_tokens(100, 20, safety_factor=0.25),
            120,
        )


class ProviderAdmissionLoopIsolationTests(unittest.TestCase):
    def test_one_controller_does_not_reuse_condition_across_event_loops(self):
        controller = ProviderAdmissionController()

        async def once():
            lease = await controller.acquire(
                endpoint="https://provider.example/v1",
                api_model="model-a",
                estimated_input_tokens=10,
                max_output_tokens=1,
                limits=ProviderAdmissionLimits(
                    max_inflight_requests=1,
                    max_inflight_estimated_tokens=100,
                ),
            )
            await lease.release()

        asyncio.run(once())
        asyncio.run(once())
