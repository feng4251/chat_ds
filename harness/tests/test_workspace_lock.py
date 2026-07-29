import asyncio
import importlib.util
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
    WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT_ENV,
    WORKSPACE_MUTATION_LOCK_ROOT_ENV,
    WorkspaceMutationLockError,
    require_session_workspace_active,
    run_sync_cancellation_safe,
    run_workspace_mutation_async,
    workspace_mutation_guard,
    workspace_mutation_lock_path,
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
        self._lock_environment = patch.dict(
            os.environ,
            {
                WORKSPACE_MUTATION_LOCK_ROOT_ENV: "",
                WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT_ENV: "0",
            },
        )
        self._lock_environment.start()
        self._temporary = tempfile.TemporaryDirectory()
        self.session = Path(self._temporary.name) / "session"
        self.workspace = self.session / "workspace"
        self.workspace.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temporary.cleanup()
        self._lock_environment.stop()

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

    def test_configured_local_lock_path_has_stable_exact_identity(self) -> None:
        lock_parent = Path(self._temporary.name) / "lock-plane"
        lock_parent.mkdir(mode=0o700)
        lock_root = lock_parent / "locks"
        first = (
            Path(self._temporary.name)
            / "storage-a"
            / "user-a"
            / "session-b"
            / "workspace"
        )
        second = (
            Path(self._temporary.name)
            / "storage-b"
            / "user-a"
            / "session-b"
            / "workspace"
        )
        other = (
            Path(self._temporary.name)
            / "storage-b"
            / "user-a"
            / "session-c"
            / "workspace"
        )
        for workspace in (first, second, other):
            workspace.mkdir(parents=True)

        with patch.dict(
            os.environ,
            {WORKSPACE_MUTATION_LOCK_ROOT_ENV: str(lock_root)},
        ):
            first_path = workspace_mutation_lock_path(first)
            self.assertEqual(first_path, workspace_mutation_lock_path(second))
            self.assertNotEqual(first_path, workspace_mutation_lock_path(other))
            self.assertEqual(
                "v1-04d251bd57ca5b4835d6235a31a7df283c3135afe1b77a12de0293efa2affe69.lock",
                first_path.name,
            )
            self.assertEqual(lock_root, first_path.parent)
            self.assertEqual(0o700, stat.S_IMODE(lock_root.stat().st_mode))

            with workspace_mutation_guard(first):
                with workspace_mutation_guard(first):
                    self.assertTrue(first_path.is_file())

            lock_stat = first_path.stat()
            self.assertEqual(0o600, stat.S_IMODE(lock_stat.st_mode))
            self.assertEqual(1, lock_stat.st_nlink)
            self.assertEqual(os.geteuid(), lock_stat.st_uid)
            self.assertFalse(
                (first.parent / ".chatds-workspace-mutation.lock").exists()
            )

    def test_configured_root_fails_closed_without_shared_parent(self) -> None:
        missing_parent = (
            Path(self._temporary.name) / "not-mounted" / "locks"
        )
        with (
            patch.dict(
                os.environ,
                {WORKSPACE_MUTATION_LOCK_ROOT_ENV: str(missing_parent)},
            ),
            self.assertRaises(WorkspaceMutationLockError) as raised,
        ):
            workspace_mutation_lock_path(self.workspace)
        self.assertEqual("workspace_lock_unsafe", raised.exception.code)
        self.assertFalse(missing_parent.parent.exists())

    def test_mountpoint_policy_is_strict_and_fail_closed(self) -> None:
        lock_parent = Path(self._temporary.name) / "lock-plane"
        lock_parent.mkdir(mode=0o700)
        lock_root = lock_parent / "locks"

        for value in ("true", "yes", ""):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {
                        WORKSPACE_MUTATION_LOCK_ROOT_ENV: str(lock_root),
                        WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT_ENV: value,
                    },
                ),
                self.assertRaises(WorkspaceMutationLockError),
            ):
                workspace_mutation_lock_path(self.workspace)

        with (
            patch.dict(
                os.environ,
                {
                    WORKSPACE_MUTATION_LOCK_ROOT_ENV: "",
                    WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT_ENV: "1",
                },
            ),
            self.assertRaises(WorkspaceMutationLockError),
        ):
            workspace_mutation_lock_path(self.workspace)

        with (
            patch.dict(
                os.environ,
                {
                    WORKSPACE_MUTATION_LOCK_ROOT_ENV: str(lock_root),
                    WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT_ENV: "1",
                },
            ),
            patch("tools.workspace_lock.os.path.ismount", return_value=True),
        ):
            self.assertEqual(
                lock_root,
                workspace_mutation_lock_path(self.workspace).parent,
            )

        with (
            patch.dict(
                os.environ,
                {
                    WORKSPACE_MUTATION_LOCK_ROOT_ENV: str(lock_root),
                    WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT_ENV: "1",
                },
            ),
            patch("tools.workspace_lock.os.path.ismount", return_value=False),
            self.assertRaises(WorkspaceMutationLockError),
        ):
            workspace_mutation_lock_path(self.workspace)

    def test_configured_root_and_lock_object_validation(self) -> None:
        lock_parent = Path(self._temporary.name) / "lock-plane"
        lock_parent.mkdir(mode=0o700)
        lock_root = lock_parent / "locks"
        lock_root.mkdir(mode=0o755)
        with (
            patch.dict(
                os.environ,
                {WORKSPACE_MUTATION_LOCK_ROOT_ENV: str(lock_root)},
            ),
            self.assertRaises(WorkspaceMutationLockError),
        ):
            workspace_mutation_lock_path(self.workspace)

        lock_root.chmod(0o700)
        with patch.dict(
            os.environ,
            {WORKSPACE_MUTATION_LOCK_ROOT_ENV: str(lock_root)},
        ):
            lock_path = workspace_mutation_lock_path(self.workspace)
            lock_path.write_text("unsafe", encoding="utf-8")
            lock_path.chmod(0o640)
            with self.assertRaises(WorkspaceMutationLockError):
                with workspace_mutation_guard(self.workspace):
                    self.fail("unsafe lock must not be entered")

    def test_backend_and_harness_lock_path_protocol_match(self) -> None:
        backend_path = (
            Path(__file__).resolve().parents[2]
            / "backend"
            / "workspace_lock.py"
        )
        specification = importlib.util.spec_from_file_location(
            "backend_workspace_lock_parity",
            backend_path,
        )
        self.assertIsNotNone(specification)
        self.assertIsNotNone(specification.loader)
        backend_lock = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(backend_lock)

        lock_parent = Path(self._temporary.name) / "lock-plane"
        lock_parent.mkdir(mode=0o700)
        lock_root = lock_parent / "locks"
        workspace = (
            Path(self._temporary.name)
            / "storage"
            / "u\u0308ser"
            / "se\u0301ssion"
            / "workspace"
        )
        workspace.mkdir(parents=True)
        with patch.dict(
            os.environ,
            {WORKSPACE_MUTATION_LOCK_ROOT_ENV: str(lock_root)},
        ):
            self.assertEqual(
                WORKSPACE_MUTATION_LOCK_ROOT_ENV,
                backend_lock.WORKSPACE_MUTATION_LOCK_ROOT_ENV,
            )
            self.assertEqual(
                "WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT",
                backend_lock.WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT_ENV,
            )
            self.assertEqual(
                workspace_mutation_lock_path(workspace),
                backend_lock.workspace_mutation_lock_path(workspace),
            )
            with workspace_mutation_guard(workspace):
                with self.assertRaises(
                    backend_lock.WorkspaceMutationLockError
                ) as blocked:
                    with backend_lock.workspace_mutation_guard(
                        workspace,
                        timeout_seconds=0,
                    ):
                        self.fail("Backend must contend on the same lock inode")
            self.assertEqual(
                "workspace_lock_timeout",
                blocked.exception.code,
            )
            with backend_lock.workspace_mutation_guard(
                workspace,
                timeout_seconds=0.1,
            ):
                pass

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
        lock_parent = Path(self._temporary.name) / "lock-plane"
        lock_parent.mkdir(mode=0o700)
        with patch.dict(
            os.environ,
            {
                WORKSPACE_MUTATION_LOCK_ROOT_ENV: str(
                    lock_parent / "locks"
                )
            },
        ):
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
        self._lock_environment = patch.dict(
            os.environ,
            {
                WORKSPACE_MUTATION_LOCK_ROOT_ENV: "",
                WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT_ENV: "0",
            },
        )
        self._lock_environment.start()
        self._temporary = tempfile.TemporaryDirectory()
        self.session = Path(self._temporary.name) / "user" / "session"
        self.workspace = self.session / "workspace"
        self.workspace.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temporary.cleanup()
        self._lock_environment.stop()

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
