import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from claude_runner.server import RunControlRequest, RunManager, _atomic_json
from native_security.run_control import (
    read_run_control,
    request_path,
    write_run_control_receipt,
)


class ClaudeSupervisorRunControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.user_id = "1" * 32
        self.conversation_id = "2" * 32
        self.run_id = "3" * 32
        settings = SimpleNamespace(
            state_root=self.root / "supervisor",
            workspace_host_root=self.root / "sessions",
            max_concurrent_runs=1,
        )
        self.manager = RunManager(settings, SimpleNamespace())
        _atomic_json(self.manager._locator_path(self.run_id), {
            "schema": "chatds.claude-run-locator.v1",
            "run_id": self.run_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
        }, mode=0o600)
        self.run_dir = self.manager._run_dir(
            self.user_id, self.conversation_id, self.run_id
        )
        self.run_dir.mkdir(parents=True, mode=0o700)
        _atomic_json(
            self.run_dir / "status.json",
            {"status": "running", "phase": "native_execution"},
            mode=0o600,
        )
        (self.run_dir / "events.jsonl").touch(mode=0o600)

    async def asyncTearDown(self):
        self.manager._preflight_executor.shutdown(
            wait=False, cancel_futures=True
        )
        self.temporary.cleanup()

    async def _deliver(self, control_id: str):
        path = request_path(self.run_dir / "controls", control_id)
        deadline = asyncio.get_running_loop().time() + 2
        while not path.exists():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("Supervisor did not persist the control request")
            await asyncio.sleep(0.01)
        request = read_run_control(path)
        write_run_control_receipt(
            self.run_dir / "controls", request, status="delivered"
        )

    async def test_delivery_receipt_is_durable_and_exactly_idempotent(self):
        payload = RunControlRequest(
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            control_id="4" * 32,
            action="followup",
            text="Compare the renamed warehouse manifest.",
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
