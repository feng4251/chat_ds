import asyncio
import threading
import unittest

from deepseek_runner.server import _wait_for_container_exit


class _WarehouseContainer:
    def __init__(self) -> None:
        self.release = threading.Event()

    def wait(self):
        self.release.wait(timeout=2)
        return {"StatusCode": 0}


class SupervisorLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unbounded_holdout_waits_for_native_completion(self):
        container = _WarehouseContainer()
        task = asyncio.create_task(_wait_for_container_exit(container, None))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())

        container.release.set()
        self.assertEqual(
            await asyncio.wait_for(task, timeout=2),
            {"StatusCode": 0},
        )

    async def test_explicit_finite_policy_remains_available(self):
        container = _WarehouseContainer()
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await _wait_for_container_exit(container, 0.01)
        finally:
            container.release.set()


if __name__ == "__main__":
    unittest.main()
