import asyncio
import multiprocessing
import os
import queue
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.tool_result_storage import persist_result_for_history
from tools import path_security
from tools.workspace_lock import (
    WorkspaceMutationLockError,
    require_session_workspace_active,
    run_sync_cancellation_safe,
    run_workspace_mutation_async,
    workspace_mutation_guard,
)


def _acquire_workspace_lock(workspace: str, events) -> None:
    events.put("waiting")
    with workspace_mutation_guard(Path(workspace)):
        events.put("acquired")


def _hold_workspace_lock(workspace: str, acquired, release) -> None:
    with workspace_mutation_guard(Path(workspace)):
        acquired.set()
        release.wait(timeout=5)


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

    def test_sandbox_creation_rejects_symlink_at_every_owned_level(
        self,
    ) -> None:
        for level in ("root", "user", "session", "workspace"):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as value:
                base = Path(value)
                outside = base / "outside"
                outside.mkdir()
                storage = base / "storage"
                if level == "root":
                    storage.symlink_to(outside, target_is_directory=True)
                else:
                    storage.mkdir()
                    if level == "user":
                        (storage / "user").symlink_to(
                            outside,
                            target_is_directory=True,
                        )
                    else:
                        user = storage / "user"
                        user.mkdir()
                        if level == "session":
                            (user / "session").symlink_to(
                                outside,
                                target_is_directory=True,
                            )
                        else:
                            session = user / "session"
                            session.mkdir()
                            (session / "workspace").symlink_to(
                                outside,
                                target_is_directory=True,
                            )
                with (
                    patch.object(path_security, "SANDBOX_ROOT", storage),
                    self.assertRaises(WorkspaceMutationLockError),
                ):
                    path_security.sandbox_dir(
                        "user",
                        "session",
                        sub="workspace",
                    )
                self.assertEqual([], list(outside.iterdir()))

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


class AsyncWorkspaceMutationLockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.session = Path(self._temporary.name) / "user" / "session"
        self.workspace = self.session / "workspace"
        self.workspace.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    async def _start_holder(self):
        context = multiprocessing.get_context("fork")
        acquired = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_workspace_lock,
            args=(str(self.workspace), acquired, release),
        )
        process.start()
        self.assertTrue(
            await asyncio.to_thread(acquired.wait, 2),
            "holder failed to acquire workspace lock",
        )
        return process, release

    async def test_async_wait_keeps_ticker_responsive_and_times_out_typed(
        self,
    ) -> None:
        process, release = await self._start_holder()
        ticks = 0
        running = True

        async def ticker() -> None:
            nonlocal ticks
            while running:
                ticks += 1
                await asyncio.sleep(0.005)

        ticker_task = asyncio.create_task(ticker())
        try:
            with (
                patch.dict(
                    os.environ,
                    {"WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS": "0.12"},
                ),
                self.assertRaises(WorkspaceMutationLockError) as raised,
            ):
                await run_workspace_mutation_async(
                    self.workspace,
                    lambda: None,
                )
            self.assertEqual("workspace_lock_timeout", raised.exception.code)
        finally:
            running = False
            await ticker_task
            release.set()
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        self.assertEqual(0, process.exitcode)
        self.assertGreaterEqual(ticks, 8)

    async def test_async_contention_flood_does_not_stall_event_loop(
        self,
    ) -> None:
        process, release = await self._start_holder()
        ticks = 0
        running = True

        async def ticker() -> None:
            nonlocal ticks
            while running:
                ticks += 1
                await asyncio.sleep(0.005)

        ticker_task = asyncio.create_task(ticker())
        try:
            with patch.dict(
                os.environ,
                {"WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS": "0.1"},
            ):
                results = await asyncio.gather(
                    *(
                        run_workspace_mutation_async(
                            self.workspace,
                            lambda: None,
                        )
                        for _ in range(12)
                    ),
                    return_exceptions=True,
                )
            self.assertTrue(all(
                isinstance(result, WorkspaceMutationLockError)
                and result.code == "workspace_lock_timeout"
                for result in results
            ))
        finally:
            running = False
            await ticker_task
            release.set()
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        self.assertEqual(0, process.exitcode)
        self.assertGreaterEqual(ticks, 6)

    async def test_cancellation_is_redelivered_after_worker_releases_lock(
        self,
    ) -> None:
        started = threading.Event()
        finished = threading.Event()

        def mutation() -> None:
            started.set()
            time.sleep(0.08)
            (self.workspace / "finished.txt").write_text(
                "done",
                encoding="utf-8",
            )
            finished.set()

        task = asyncio.create_task(
            run_workspace_mutation_async(self.workspace, mutation)
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
        self.assertEqual(
            "done",
            (self.workspace / "finished.txt").read_text(encoding="utf-8"),
        )
        with workspace_mutation_guard(self.workspace, timeout_seconds=0.05):
            pass

    async def test_post_wait_tombstone_rejects_without_dispatch(self) -> None:
        dispatched = False

        def mark_dispatched() -> None:
            nonlocal dispatched
            dispatched = True

        tombstone = (
            self.session.parent
            / ".chatds-session-tombstones"
            / f"{self.session.name}.deleted"
        )
        tombstone.parent.mkdir(mode=0o700)
        with workspace_mutation_guard(self.workspace):
            task = asyncio.create_task(
                run_workspace_mutation_async(
                    self.workspace,
                    mark_dispatched,
                    timeout_seconds=1,
                )
            )
            await asyncio.sleep(0.03)
            tombstone.write_text(
                "chatds-session-deletion-v1\n",
                encoding="utf-8",
            )
            tombstone.chmod(0o600)

        with self.assertRaises(WorkspaceMutationLockError) as raised:
            await task
        self.assertEqual("workspace_session_deleted", raised.exception.code)
        self.assertFalse(dispatched)

    async def test_pending_lifecycle_fence_rejects_access_and_dispatch(
        self,
    ) -> None:
        dispatched = False

        def mark_dispatched() -> None:
            nonlocal dispatched
            dispatched = True

        pending = (
            self.session.parent
            / ".chatds-session-pending"
            / f"{self.session.name}.pending"
        )
        pending.parent.mkdir(mode=0o700)
        with workspace_mutation_guard(self.workspace):
            task = asyncio.create_task(
                run_workspace_mutation_async(
                    self.workspace,
                    mark_dispatched,
                    timeout_seconds=1,
                )
            )
            await asyncio.sleep(0.03)
            pending.write_text(
                "chatds-session-lifecycle-pending-v1\n"
                f"operation_id={'a' * 64}\n",
                encoding="ascii",
            )
            pending.chmod(0o600)

        with self.assertRaises(WorkspaceMutationLockError) as raised:
            await task
        self.assertEqual("workspace_session_pending", raised.exception.code)
        self.assertFalse(dispatched)
        with self.assertRaises(WorkspaceMutationLockError) as direct:
            require_session_workspace_active(self.workspace)
        self.assertEqual("workspace_session_pending", direct.exception.code)

    async def test_result_persistence_waiter_cannot_commit_after_delete(
        self,
    ) -> None:
        tombstone = (
            self.session.parent
            / ".chatds-session-tombstones"
            / f"{self.session.name}.deleted"
        )
        with (
            patch(
                "tools.path_security.SANDBOX_ROOT",
                self.session.parents[1],
            ),
            workspace_mutation_guard(self.workspace),
        ):
            task = asyncio.create_task(
                run_sync_cancellation_safe(
                    lambda: persist_result_for_history(
                        "bounded child output",
                        "delegate_worker",
                        user_id=self.session.parent.name,
                        session_id=self.session.name,
                    )
                )
            )
            await asyncio.sleep(0.03)
            tombstone.parent.mkdir(mode=0o700)
            tombstone.write_text(
                "chatds-session-deletion-v1\n",
                encoding="utf-8",
            )
            tombstone.chmod(0o600)

        with self.assertRaises(WorkspaceMutationLockError) as raised:
            await task
        self.assertEqual("workspace_session_deleted", raised.exception.code)
        self.assertFalse((self.session / "results").exists())


if __name__ == "__main__":
    unittest.main()
