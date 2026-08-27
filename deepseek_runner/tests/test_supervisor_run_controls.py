import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepseek_runner.server import (
    Manager,
    RunControlRequest,
    _atomic_json,
)
from native_security.run_control import (
    read_run_control,
    request_path,
    write_run_control_receipt,
)


class DeepSeekSupervisorRunControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.user_id = "5" * 32
        self.conversation_id = "6" * 32
        self.run_id = "7" * 32
        settings = SimpleNamespace(
            state_root=self.root / "supervisor",
            max_concurrent_runs=1,
        )
        self.manager = Manager(
            settings,
            SimpleNamespace(),
            state_host_root=self.root / "host-state",
        )
        _atomic_json(self.manager._locator(self.run_id), {
            "run_id": self.run_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
        })
        self.control = self.manager._control(
            self.user_id, self.conversation_id, self.run_id
        )
        self.control.mkdir(parents=True, mode=0o700)
        _atomic_json(
            self.control / "status.json",
            {"status": "running", "phase": "native_execution"},
        )
        (self.control / "events.jsonl").touch(mode=0o600)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def _deliver(self, control_id: str):
        path = request_path(self.control / "controls", control_id)
        deadline = asyncio.get_running_loop().time() + 2
        while not path.exists():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("Supervisor did not persist the control request")
            await asyncio.sleep(0.01)
        request = read_run_control(path)
        write_run_control_receipt(
            self.control / "controls", request, status="delivered"
        )

    async def test_delivery_receipt_is_durable_and_exactly_idempotent(self):
        payload = RunControlRequest(
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            control_id="8" * 32,
            action="steer",
            text="Prioritize the renamed gallery inventory.",
        )
        pending = asyncio.create_task(
            self.manager.control(self.run_id, payload)
        )
        await self._deliver(payload.control_id)
        receipt = await asyncio.wait_for(pending, timeout=2)
        self.assertEqual(receipt["status"], "delivered")
        self.assertFalse(receipt["idempotent"])

        replay = await self.manager.control(self.run_id, payload)
        self.assertEqual(replay["status"], "delivered")
        self.assertTrue(replay["idempotent"])


if __name__ == "__main__":
    unittest.main()
