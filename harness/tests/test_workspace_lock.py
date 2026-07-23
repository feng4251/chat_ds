import multiprocessing
import os
import queue
import stat
import tempfile
import unittest
from pathlib import Path

from tools.workspace_lock import (
    WorkspaceMutationLockError,
    workspace_mutation_guard,
)


def _acquire_workspace_lock(workspace: str, events) -> None:
    events.put("waiting")
    with workspace_mutation_guard(Path(workspace)):
        events.put("acquired")


class WorkspaceMutationLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.session = Path(self._temporary.name) / "session"
        self.workspace = self.session / "workspace"
        self.workspace.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_lock_is_private_and_outside_model_workspace(self) -> None:
        with workspace_mutation_guard(self.workspace):
            lock_path = self.session / ".chatds-workspace-mutation.lock"
            self.assertTrue(lock_path.is_file())
            self.assertFalse((self.workspace / lock_path.name).exists())
            self.assertEqual(
                0,
                stat.S_IMODE(lock_path.stat().st_mode) & 0o077,
            )

    def test_symlink_lock_is_rejected(self) -> None:
        target = self.session / "attacker-controlled"
        target.write_text("x", encoding="utf-8")
        (self.session / ".chatds-workspace-mutation.lock").symlink_to(target)

        with self.assertRaises(WorkspaceMutationLockError):
            with workspace_mutation_guard(self.workspace):
                self.fail("unsafe lock must not be entered")

    @unittest.skipUnless(os.name == "posix", "requires POSIX flock")
    def test_lock_serializes_independent_processes(self) -> None:
        context = multiprocessing.get_context("fork")
        events = context.Queue()
        process = None
        with workspace_mutation_guard(self.workspace):
            process = context.Process(
                target=_acquire_workspace_lock,
                args=(str(self.workspace), events),
            )
            process.start()
            self.assertEqual("waiting", events.get(timeout=2))
            with self.assertRaises(queue.Empty):
                events.get(timeout=0.2)

        self.assertEqual("acquired", events.get(timeout=2))
        process.join(timeout=2)
        self.assertFalse(process.is_alive())
        self.assertEqual(0, process.exitcode)


if __name__ == "__main__":
    unittest.main()
