from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tools import isolated_skill_executor as executor_client
from tools.executor_slot_pool import (
    ExecutorSlotPool,
    ExecutorSlotPoolError,
    configured_executor_socket_paths,
    executor_pool_identity_sha256,
    get_executor_slot_pool,
    reset_executor_slot_pool_registry_for_tests,
)


class ExecutorSlotPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_admission_keeps_one_transient_slot(self) -> None:
        pool = ExecutorSlotPool(
            tuple(f"/pool/slot-{index}.sock" for index in range(4))
        )
        persistent = [
            await pool.acquire("persistent")
            for _ in range(3)
        ]
        fourth = asyncio.create_task(pool.acquire("persistent"))
        await asyncio.sleep(0)
        self.assertFalse(fourth.done())

        transient = await asyncio.wait_for(
            pool.acquire("transient"),
            timeout=0.2,
        )
        self.assertNotIn(
            transient.socket_path,
            {reservation.socket_path for reservation in persistent},
        )
        await transient.release()
        await asyncio.sleep(0)
        self.assertFalse(fourth.done())

        await persistent.pop(0).release()
        admitted = await asyncio.wait_for(fourth, timeout=0.2)
        persistent.append(admitted)
        for reservation in persistent:
            await reservation.release()

    async def test_fifo_waiter_cancellation_does_not_consume_slot(self) -> None:
        pool = ExecutorSlotPool(("/pool/a.sock", "/pool/b.sock"))
        occupied = [
            await pool.acquire("transient"),
            await pool.acquire("transient"),
        ]
        cancelled = asyncio.create_task(pool.acquire("transient"))
        survivor = asyncio.create_task(pool.acquire("transient"))
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled

        await occupied.pop(0).release()
        admitted = await asyncio.wait_for(survivor, timeout=0.2)
        await admitted.release()
        await occupied.pop().release()

        snapshot = await pool.snapshot()
        self.assertEqual(
            {"free"},
            {str(item["state"]) for item in snapshot},
        )

    async def test_quarantined_slot_is_not_reused(self) -> None:
        pool = ExecutorSlotPool(("/pool/a.sock", "/pool/b.sock"))
        uncertain = await pool.acquire("transient")
        quarantined_path = uncertain.socket_path
        await uncertain.quarantine("response_lost_after_write")

        healthy = await asyncio.wait_for(
            pool.acquire("transient"),
            timeout=0.2,
        )
        self.assertNotEqual(quarantined_path, healthy.socket_path)
        await healthy.release()
        snapshot = await pool.snapshot()
        state_by_path = {
            str(item["socket_path"]): str(item["state"])
            for item in snapshot
        }
        self.assertEqual("quarantined", state_by_path[quarantined_path])

    async def test_terminal_lease_error_releases_or_quarantines_before_forget(
        self,
    ) -> None:
        pool = ExecutorSlotPool(("/pool/a.sock", "/pool/b.sock"))

        def lease_for(reservation):
            return executor_client.IsolatedProcessLease(
                handle="pl2_" + "a" * 32 + "_" + "b" * 32,
                skill_sha256="c" * 64,
                script_sha256="d" * 64,
                entrypoint="scripts/task.py",
                invocation_mode="cli",
                class_name=None,
                factory_name=None,
                _owner_scope=executor_client.create_process_owner_scope(
                    user_id="user",
                    session_id="session",
                    root_run_id="run",
                ),
                _workspace=Path("/workspace"),
                _socket_path=reservation.socket_path,
                _baseline={},
                _slot_reservation=reservation,
            )

        released_reservation = await pool.acquire("persistent")
        released_path = released_reservation.socket_path
        released_lease = lease_for(released_reservation)
        self.assertTrue(
            await executor_client.finalize_terminal_process_lease_error(
                released_lease,
                executor_client.IsolatedSkillExecutorError(
                    "lease_lost",
                    "executor lease is gone",
                ),
            )
        )
        self.assertIsNone(released_lease._slot_reservation)
        released_state = {
            item["socket_path"]: item["state"]
            for item in await pool.snapshot()
        }
        self.assertEqual("free", released_state[released_path])

        quarantine_reservation = await pool.acquire("persistent")
        quarantine_path = quarantine_reservation.socket_path
        quarantine_lease = lease_for(quarantine_reservation)
        self.assertTrue(
            await executor_client.finalize_terminal_process_lease_error(
                quarantine_lease,
                executor_client.IsolatedSkillExecutorError(
                    "worker_containment_failed",
                    "executor retained quarantined state",
                    terminal_lease_state="quarantined",
                ),
            )
        )
        self.assertIsNone(quarantine_lease._slot_reservation)
        quarantined_state = {
            item["socket_path"]: item["state"]
            for item in await pool.snapshot()
        }
        self.assertEqual(
            "quarantined",
            quarantined_state[quarantine_path],
        )

    async def test_terminal_containment_quarantine_rejoins_exact_cohort(
        self,
    ) -> None:
        path = "/pool/a.sock"
        digest = "a" * 64
        pool = ExecutorSlotPool((path,))
        await pool.apply_startup_attestations({path: digest})
        reprobe_entered = asyncio.Event()
        allow_reprobe = asyncio.Event()

        async def reprobe(socket_path: str) -> str:
            self.assertEqual(path, socket_path)
            reprobe_entered.set()
            await allow_reprobe.wait()
            return digest

        pool.configure_reprobe_handler(reprobe)
        reservation = await pool.acquire("persistent")
        lease = executor_client.IsolatedProcessLease(
            handle="pl2_" + "a" * 32 + "_" + "b" * 32,
            skill_sha256="c" * 64,
            script_sha256="d" * 64,
            entrypoint="scripts/task.py",
            invocation_mode="cli",
            class_name=None,
            factory_name=None,
            _owner_scope=executor_client.create_process_owner_scope(
                user_id="user",
                session_id="session",
                root_run_id="run",
            ),
            _workspace=Path("/workspace"),
            _socket_path=reservation.socket_path,
            _baseline={},
            _slot_reservation=reservation,
        )

        finalized = await executor_client.finalize_terminal_process_lease_error(
            lease,
            executor_client.IsolatedSkillExecutorError(
                "worker_containment_failed",
                "executor retained quarantined state",
                terminal_lease_state="quarantined",
            ),
        )
        await asyncio.wait_for(reprobe_entered.wait(), timeout=0.2)
        reprobe_task = next(iter(pool._reprobe_tasks.values()))
        allow_reprobe.set()
        await asyncio.wait_for(asyncio.shield(reprobe_task), timeout=0.2)

        self.assertTrue(finalized)
        self.assertIsNone(lease._slot_reservation)
        self.assertEqual("free", (await pool.snapshot())[0]["state"])

    async def test_waiter_fails_when_every_candidate_becomes_quarantined(
        self,
    ) -> None:
        pool = ExecutorSlotPool(("/pool/a.sock", "/pool/b.sock"))
        occupied = [
            await pool.acquire("transient"),
            await pool.acquire("transient"),
        ]
        waiter = asyncio.create_task(pool.acquire("transient"))
        await asyncio.sleep(0)

        await occupied[0].quarantine("response_lost")
        await occupied[1].quarantine("response_lost")

        with self.assertRaises(ExecutorSlotPoolError) as caught:
            await asyncio.wait_for(waiter, timeout=0.2)
        self.assertEqual("executor_pool_unavailable", caught.exception.code)

    async def test_waiting_admission_cancel_wins_before_slot_grant(
        self,
    ) -> None:
        pool = ExecutorSlotPool(("/pool/a.sock",))
        occupied = await pool.acquire("transient")
        cancel_event = asyncio.Event()
        waiter = asyncio.create_task(
            pool.acquire("persistent", cancel_event=cancel_event)
        )
        await asyncio.sleep(0)

        cancel_event.set()
        await occupied.release()

        with self.assertRaises(ExecutorSlotPoolError) as caught:
            await asyncio.wait_for(waiter, timeout=0.2)
        self.assertEqual(
            "executor_admission_cancelled",
            caught.exception.code,
        )
        self.assertEqual("free", (await pool.snapshot())[0]["state"])

    async def test_slot_grant_wins_before_late_admission_cancel(
        self,
    ) -> None:
        pool = ExecutorSlotPool(("/pool/a.sock",))
        occupied = await pool.acquire("transient")
        cancel_event = asyncio.Event()
        waiter = asyncio.create_task(
            pool.acquire("persistent", cancel_event=cancel_event)
        )
        await asyncio.sleep(0)

        await occupied.release()
        admitted = await asyncio.wait_for(waiter, timeout=0.2)
        cancel_event.set()
        await asyncio.sleep(0)

        self.assertFalse(admitted.terminal)
        self.assertEqual("leased", (await pool.snapshot())[0]["state"])
        await admitted.release()

    async def test_cancelled_release_finishes_terminal_slot_transition(
        self,
    ) -> None:
        pool = ExecutorSlotPool(("/pool/a.sock",))
        reservation = await pool.acquire("persistent")
        await pool._lock.acquire()
        releasing = asyncio.create_task(reservation.release())
        await asyncio.sleep(0)
        releasing.cancel()
        pool._lock.release()

        with self.assertRaises(asyncio.CancelledError):
            await releasing
        self.assertTrue(reservation.terminal)
        self.assertEqual("free", (await pool.snapshot())[0]["state"])

    async def test_reservation_token_mismatch_fails_closed(self) -> None:
        pool = ExecutorSlotPool(("/pool/a.sock",))
        reservation = await pool.acquire("transient")
        authoritative_token = reservation._token
        reservation._token = "stale-reservation-token"

        with self.assertRaises(ExecutorSlotPoolError) as caught:
            await reservation.release()

        self.assertEqual(
            "executor_pool_reservation_mismatch",
            caught.exception.code,
        )
        self.assertFalse(reservation.terminal)
        self.assertEqual("reserved", (await pool.snapshot())[0]["state"])

        reservation._token = authoritative_token
        await reservation.release()
        self.assertTrue(reservation.terminal)

    async def test_quarantined_slot_rejoins_exact_startup_cohort(
        self,
    ) -> None:
        path = "/pool/a.sock"
        digest = "a" * 64
        pool = ExecutorSlotPool((path,))
        await pool.apply_startup_attestations({path: digest})
        reprobe = AsyncMock(return_value=digest)
        pool.configure_reprobe_handler(reprobe)

        uncertain = await pool.acquire("transient")
        await uncertain.quarantine("executor_connect_unavailable")
        recovered = await pool.acquire("transient")

        self.assertEqual(path, recovered.socket_path)
        reprobe.assert_awaited_once_with(path)
        self.assertEqual("reserved", (await pool.snapshot())[0]["state"])
        await recovered.release()

    async def test_reprobe_cannot_be_configured_without_startup_cohort(
        self,
    ) -> None:
        pool = ExecutorSlotPool(("/pool/a.sock",))

        async def reprobe(_socket_path: str) -> str:
            return "a" * 64

        with self.assertRaises(ExecutorSlotPoolError) as caught:
            pool.configure_reprobe_handler(reprobe)
        self.assertEqual(
            "executor_pool_reprobe_configuration_race",
            caught.exception.code,
        )

    async def test_quarantine_boundary_starts_reprobe_without_admission(
        self,
    ) -> None:
        path = "/pool/a.sock"
        digest = "a" * 64
        pool = ExecutorSlotPool((path,))
        await pool.apply_startup_attestations({path: digest})
        reprobe_entered = asyncio.Event()
        allow_reprobe = asyncio.Event()

        async def reprobe(_socket_path: str) -> str:
            reprobe_entered.set()
            await allow_reprobe.wait()
            return digest

        pool.configure_reprobe_handler(reprobe)
        uncertain = await pool.acquire("transient")
        await uncertain.quarantine("executor_connect_unavailable")
        await asyncio.wait_for(reprobe_entered.wait(), timeout=0.2)

        self.assertEqual((), pool.probe_socket_paths())
        reprobe_task = next(iter(pool._reprobe_tasks.values()))
        allow_reprobe.set()
        await asyncio.wait_for(asyncio.shield(reprobe_task), timeout=0.2)
        self.assertEqual((path,), pool.probe_socket_paths())

    async def test_heterogeneous_reprobe_cannot_rejoin_cohort(self) -> None:
        path = "/pool/a.sock"
        pool = ExecutorSlotPool((path,))
        await pool.apply_startup_attestations({path: "a" * 64})
        reprobe = AsyncMock(return_value="b" * 64)
        pool.configure_reprobe_handler(reprobe)

        uncertain = await pool.acquire("transient")
        await uncertain.quarantine("executor_connect_unavailable")
        with self.assertRaises(ExecutorSlotPoolError) as caught:
            await pool.acquire("transient")

        self.assertEqual("executor_pool_unavailable", caught.exception.code)
        snapshot = (await pool.snapshot())[0]
        self.assertEqual("quarantined", snapshot["state"])
        self.assertEqual(
            "executor_reprobe_attestation_mismatch",
            snapshot["quarantine_reason"],
        )
        self.assertFalse(snapshot["quarantine_recoverable"])

        with self.assertRaises(ExecutorSlotPoolError):
            await pool.acquire("transient")
        self.assertEqual(1, reprobe.await_count)

    async def test_failed_reprobe_uses_bounded_backoff(self) -> None:
        path = "/pool/a.sock"
        pool = ExecutorSlotPool((path,))
        await pool.apply_startup_attestations({path: "a" * 64})
        reprobe = AsyncMock(side_effect=OSError("fixture unavailable"))
        pool.configure_reprobe_handler(reprobe)

        uncertain = await pool.acquire("transient")
        await uncertain.quarantine("executor_connect_unavailable")
        for _ in range(2):
            with self.assertRaises(ExecutorSlotPoolError) as caught:
                await pool.acquire("transient")
            self.assertEqual(
                "executor_pool_unavailable",
                caught.exception.code,
            )

        self.assertEqual(1, reprobe.await_count)
        snapshot = (await pool.snapshot())[0]
        self.assertTrue(snapshot["quarantine_recoverable"])
        self.assertEqual(1, snapshot["reprobe_failure_count"])

    async def test_concurrent_admissions_share_one_bounded_reprobe(
        self,
    ) -> None:
        path = "/pool/a.sock"
        digest = "a" * 64
        pool = ExecutorSlotPool((path,))
        await pool.apply_startup_attestations({path: digest})
        reprobe_entered = asyncio.Event()
        allow_reprobe = asyncio.Event()
        reprobe_calls = 0

        async def reprobe(socket_path: str) -> str:
            nonlocal reprobe_calls
            self.assertEqual(path, socket_path)
            reprobe_calls += 1
            reprobe_entered.set()
            await allow_reprobe.wait()
            return digest

        pool.configure_reprobe_handler(reprobe)
        uncertain = await pool.acquire("transient")
        await uncertain.quarantine("executor_connect_unavailable")

        first_task = asyncio.create_task(pool.acquire("transient"))
        await asyncio.wait_for(reprobe_entered.wait(), timeout=0.2)
        second_task = asyncio.create_task(pool.acquire("transient"))
        await asyncio.sleep(0)
        self.assertEqual(1, reprobe_calls)
        allow_reprobe.set()

        done, pending = await asyncio.wait(
            {first_task, second_task},
            timeout=0.2,
            return_when=asyncio.FIRST_COMPLETED,
        )
        self.assertEqual(1, len(done))
        self.assertEqual(1, len(pending))
        first_reservation = done.pop().result()
        await first_reservation.release()
        second_reservation = await asyncio.wait_for(
            pending.pop(),
            timeout=0.2,
        )
        await second_reservation.release()
        self.assertEqual(1, reprobe_calls)

    async def test_healthy_capacity_does_not_wait_for_reprobe(self) -> None:
        paths = ("/pool/a.sock", "/pool/b.sock")
        digest = "a" * 64
        pool = ExecutorSlotPool(paths)
        await pool.apply_startup_attestations({
            path: digest for path in paths
        })
        reprobe_entered = asyncio.Event()
        allow_reprobe = asyncio.Event()

        async def reprobe(_socket_path: str) -> str:
            reprobe_entered.set()
            await allow_reprobe.wait()
            return digest

        pool.configure_reprobe_handler(reprobe)
        uncertain = await pool.acquire("transient")
        quarantined_path = uncertain.socket_path
        await uncertain.quarantine("executor_connect_unavailable")

        healthy = await asyncio.wait_for(
            pool.acquire("transient"),
            timeout=0.2,
        )
        self.assertNotEqual(quarantined_path, healthy.socket_path)
        await asyncio.wait_for(reprobe_entered.wait(), timeout=0.2)
        reprobe_task = next(iter(pool._reprobe_tasks.values()))
        allow_reprobe.set()
        await asyncio.wait_for(asyncio.shield(reprobe_task), timeout=0.2)
        await healthy.release()
        self.assertFalse(pool._reprobe_tasks)
        self.assertEqual(
            {"free"},
            {item["state"] for item in await pool.snapshot()},
        )

    async def test_startup_keeps_largest_homogeneous_cohort(self) -> None:
        paths = tuple(f"/pool/slot-{index}.sock" for index in range(4))
        pool = ExecutorSlotPool(paths)
        result = await pool.apply_startup_attestations({
            paths[0]: "a" * 64,
            paths[1]: "a" * 64,
            paths[2]: "a" * 64,
            paths[3]: "b" * 64,
        })
        self.assertEqual(3, result["healthy_count"])
        self.assertEqual(1, result["quarantined_count"])
        snapshot = await pool.snapshot()
        self.assertEqual(
            ["free", "free", "free", "quarantined"],
            [item["state"] for item in snapshot],
        )

    async def test_startup_rejects_equal_attestation_cohorts(self) -> None:
        paths = tuple(f"/pool/slot-{index}.sock" for index in range(4))
        pool = ExecutorSlotPool(paths)
        with self.assertRaises(ExecutorSlotPoolError) as caught:
            await pool.apply_startup_attestations({
                paths[0]: "a" * 64,
                paths[1]: "a" * 64,
                paths[2]: "b" * 64,
                paths[3]: "b" * 64,
            })
        self.assertEqual(
            "executor_attestation_ambiguous",
            caught.exception.code,
        )
        self.assertEqual((), pool.probe_socket_paths())
        self.assertEqual(
            {"quarantined"},
            {item["state"] for item in await pool.snapshot()},
        )

    async def test_startup_rejects_non_sha_attestation(self) -> None:
        pool = ExecutorSlotPool(("/pool/a.sock",))
        with self.assertRaises(ExecutorSlotPoolError) as caught:
            await pool.apply_startup_attestations({
                "/pool/a.sock": "not-a-sha",
            })
        self.assertEqual(
            "executor_attestation_invalid",
            caught.exception.code,
        )


class ExecutorPoolConfigurationTests(unittest.TestCase):
    def test_ordered_pool_configuration_and_digest_bind_every_member(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {
                "EXECUTOR_SOCKET": "/pool/one.sock",
                "EXECUTOR_POOL_SOCKETS": (
                    "/pool/one.sock,/pool/two.sock,/pool/three.sock"
                ),
            },
        ):
            paths = configured_executor_socket_paths()
        self.assertEqual(
            (
                "/pool/one.sock",
                "/pool/two.sock",
                "/pool/three.sock",
            ),
            paths,
        )
        first = executor_pool_identity_sha256(
            paths,
            runtime_profile="session-sandbox-v1",
        )
        second = executor_pool_identity_sha256(
            tuple(reversed(paths)),
            runtime_profile="session-sandbox-v1",
        )
        self.assertNotEqual(first, second)


class _FakeReader:
    def __init__(self) -> None:
        self.line: bytes | None = None
        self.error: BaseException | None = None

    async def readline(self) -> bytes:
        if self.error is not None:
            raise self.error
        return self.line or b""


class _FakeWriter:
    def __init__(self, reader: _FakeReader, response_kind: str) -> None:
        self.reader = reader
        self.response_kind = response_kind
        self.closed = False

    def write(self, encoded: bytes) -> None:
        request = json.loads(encoded)
        if self.response_kind == "transport_lost":
            self.reader.error = OSError("fixture response lost")
            return
        status = (
            "error"
            if self.response_kind
            in {"worker_busy", "worker_containment_failed"}
            else "success"
        )
        if "kind" not in request:
            response: dict[str, object] = {
                "status": status,
                "output": "legacy-ok\n",
                "exit_code": 0,
                "network": "disabled",
            }
        else:
            response = {
                "protocol_version": executor_client.PROTOCOL_VERSION,
                "kind": "session_code_result",
                "request_id": request["request_id"],
                "status": status,
                "artifacts": [],
            }
        if self.response_kind == "worker_busy":
            response.update({
                "error_code": "worker_busy",
                "error": "fixture slot still occupied",
            })
        elif self.response_kind == "worker_containment_failed":
            response.update({
                "error_code": "worker_containment_failed",
                "error": "fixture controller tree survived cleanup",
            })
        self.reader.line = (
            json.dumps(response, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class PooledExecutorClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        reset_executor_slot_pool_registry_for_tests()
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)

    async def asyncTearDown(self) -> None:
        reset_executor_slot_pool_registry_for_tests()
        self.tempdir.cleanup()

    async def test_single_socket_registry_serializes_without_pool_env(
        self,
    ) -> None:
        path = "/legacy/executor.sock"
        with patch.dict(
            os.environ,
            {
                "EXECUTOR_SOCKET": path,
                "EXECUTOR_POOL_SOCKETS": "",
            },
        ):
            first_pool = get_executor_slot_pool(primary_socket=path)
            second_pool = get_executor_slot_pool(primary_socket=path)
            self.assertIs(first_pool, second_pool)

            occupied = await first_pool.acquire("transient")
            waiting = asyncio.create_task(
                second_pool.acquire("transient")
            )
            await asyncio.sleep(0)
            self.assertFalse(waiting.done())

            await occupied.release()
            admitted = await asyncio.wait_for(waiting, timeout=0.2)
            await admitted.release()

        self.assertEqual(
            "free",
            (await first_pool.snapshot())[0]["state"],
        )

    async def test_single_socket_client_calls_never_overlap_without_pool_env(
        self,
    ) -> None:
        path = "/legacy/executor.sock"
        first_connected = asyncio.Event()
        allow_first_response = asyncio.Event()
        connection_calls: list[str] = []

        class BlockingReader(_FakeReader):
            async def readline(self) -> bytes:
                await allow_first_response.wait()
                return await super().readline()

        async def open_connection(socket_path: str, *, limit: int):
            del limit
            connection_calls.append(socket_path)
            if len(connection_calls) == 1:
                reader: _FakeReader = BlockingReader()
                first_connected.set()
            else:
                reader = _FakeReader()
            return reader, _FakeWriter(reader, "success")

        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": path,
                    "EXECUTOR_POOL_SOCKETS": "",
                },
            ),
            patch.object(
                executor_client.asyncio,
                "open_unix_connection",
                side_effect=open_connection,
            ),
        ):
            first = asyncio.create_task(
                executor_client.execute_isolated_session_code(
                    workspace=self.workspace,
                    code="print('first')",
                    socket_path=path,
                )
            )
            await asyncio.wait_for(first_connected.wait(), timeout=0.2)
            second = asyncio.create_task(
                executor_client.execute_isolated_session_code(
                    workspace=self.workspace,
                    code="print('second')",
                    socket_path=path,
                )
            )
            await asyncio.sleep(0)
            self.assertEqual([path], connection_calls)

            allow_first_response.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, second),
                timeout=0.4,
            )

        self.assertEqual(["success", "success"], [
            result["status"] for result in results
        ])
        self.assertEqual([path, path], connection_calls)

    async def test_legacy_compute_uses_parallel_pool_slots_without_overlap(
        self,
    ) -> None:
        paths = ("/pool/a.sock", "/pool/b.sock")
        first_connected = asyncio.Event()
        allow_first_response = asyncio.Event()
        connection_calls: list[str] = []

        class BlockingReader(_FakeReader):
            async def readline(self) -> bytes:
                await allow_first_response.wait()
                return await super().readline()

        async def open_connection(path: str, *, limit: int):
            del limit
            connection_calls.append(path)
            if len(connection_calls) == 1:
                reader: _FakeReader = BlockingReader()
                first_connected.set()
            else:
                reader = _FakeReader()
            return reader, _FakeWriter(reader, "success")

        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": paths[0],
                    "EXECUTOR_POOL_SOCKETS": ",".join(paths),
                },
            ),
            patch.object(
                executor_client.asyncio,
                "open_unix_connection",
                side_effect=open_connection,
            ),
        ):
            first = asyncio.create_task(
                executor_client.execute_isolated_legacy_code(
                    code="print('first')",
                    socket_path=paths[0],
                )
            )
            await asyncio.wait_for(first_connected.wait(), timeout=0.2)
            second = asyncio.create_task(
                executor_client.execute_isolated_legacy_code(
                    code="print('second')",
                    socket_path=paths[0],
                )
            )
            await asyncio.sleep(0)
            self.assertEqual(list(paths), connection_calls)
            self.assertEqual("success", (await second)["status"])
            allow_first_response.set()
            self.assertEqual(
                "success",
                (await asyncio.wait_for(first, timeout=0.2))["status"],
            )

        self.assertEqual(list(paths), connection_calls)

    async def test_legacy_compute_shares_fair_admission_with_process_lane(
        self,
    ) -> None:
        paths = ("/pool/a.sock", "/pool/b.sock")
        connection_calls: list[str] = []

        async def open_connection(path: str, *, limit: int):
            del limit
            connection_calls.append(path)
            reader = _FakeReader()
            return reader, _FakeWriter(reader, "success")

        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": paths[0],
                    "EXECUTOR_POOL_SOCKETS": ",".join(paths),
                },
            ),
            patch.object(
                executor_client.asyncio,
                "open_unix_connection",
                side_effect=open_connection,
            ),
        ):
            pool = get_executor_slot_pool(primary_socket=paths[0])
            persistent = await pool.acquire("persistent")
            self.assertEqual(paths[0], persistent.socket_path)
            result = await executor_client.execute_isolated_legacy_code(
                code="print('transient')",
                socket_path=paths[0],
            )
            await persistent.release()
            snapshot = await pool.snapshot()

        self.assertEqual("success", result["status"])
        self.assertEqual([paths[1]], connection_calls)
        self.assertEqual(
            {"free"},
            {str(item["state"]) for item in snapshot},
        )

    async def test_typed_worker_busy_moves_one_shot_to_another_slot(
        self,
    ) -> None:
        paths = ("/pool/a.sock", "/pool/b.sock")
        calls: list[str] = []

        async def open_connection(path: str, *, limit: int):
            del limit
            calls.append(path)
            reader = _FakeReader()
            writer = _FakeWriter(
                reader,
                "worker_busy" if len(calls) == 1 else "success",
            )
            return reader, writer

        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": paths[0],
                    "EXECUTOR_POOL_SOCKETS": ",".join(paths),
                },
            ),
            patch.object(
                executor_client.asyncio,
                "open_unix_connection",
                side_effect=open_connection,
            ),
        ):
            result = await executor_client.execute_isolated_session_code(
                workspace=self.workspace,
                code="print('ok')",
                socket_path=paths[0],
            )
            pool = get_executor_slot_pool(primary_socket=paths[0])
            snapshot = await pool.snapshot()

        self.assertEqual("success", result["status"])
        self.assertEqual(list(paths), calls)
        self.assertEqual(
            ["quarantined", "free"],
            [item["state"] for item in snapshot],
        )

    async def test_post_write_transport_loss_quarantines_without_replay(
        self,
    ) -> None:
        paths = ("/pool/a.sock", "/pool/b.sock")
        calls: list[str] = []

        async def open_connection(path: str, *, limit: int):
            del limit
            calls.append(path)
            reader = _FakeReader()
            return reader, _FakeWriter(reader, "transport_lost")

        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": paths[0],
                    "EXECUTOR_POOL_SOCKETS": ",".join(paths),
                },
            ),
            patch.object(
                executor_client.asyncio,
                "open_unix_connection",
                side_effect=open_connection,
            ),
        ):
            with self.assertRaises(
                executor_client.IsolatedSkillExecutorError
            ) as caught:
                await executor_client.execute_isolated_session_code(
                    workspace=self.workspace,
                    code="print('unknown')",
                    socket_path=paths[0],
                )
            pool = get_executor_slot_pool(primary_socket=paths[0])
            snapshot = await pool.snapshot()

        self.assertEqual("executor_unavailable", caught.exception.code)
        self.assertTrue(caught.exception.dispatch_unknown)
        self.assertEqual([paths[0]], calls)
        self.assertEqual("quarantined", snapshot[0]["state"])
        self.assertEqual("free", snapshot[1]["state"])

    async def test_post_response_close_cancellation_drains_and_releases_slot(
        self,
    ) -> None:
        path = "/pool/a.sock"
        close_entered = asyncio.Event()
        allow_close = asyncio.Event()
        close_finished = asyncio.Event()

        class BlockingCloseWriter(_FakeWriter):
            async def wait_closed(self) -> None:
                close_entered.set()
                await allow_close.wait()
                close_finished.set()

        async def open_connection(socket_path: str, *, limit: int):
            del limit
            self.assertEqual(path, socket_path)
            reader = _FakeReader()
            return reader, BlockingCloseWriter(reader, "success")

        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": path,
                    "EXECUTOR_POOL_SOCKETS": path,
                },
            ),
            patch.object(
                executor_client.asyncio,
                "open_unix_connection",
                side_effect=open_connection,
            ),
        ):
            execution = asyncio.create_task(
                executor_client.execute_isolated_session_code(
                    workspace=self.workspace,
                    code="print('terminal receipt')",
                    socket_path=path,
                )
            )
            await asyncio.wait_for(close_entered.wait(), timeout=0.2)
            pool = get_executor_slot_pool(primary_socket=path)
            self.assertEqual(
                "reserved",
                (await pool.snapshot())[0]["state"],
            )

            execution.cancel()
            await asyncio.sleep(0)
            self.assertFalse(execution.done())
            allow_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(execution, timeout=0.2)

            self.assertTrue(close_finished.is_set())
            self.assertEqual(
                "free",
                (await pool.snapshot())[0]["state"],
            )
            replacement = await asyncio.wait_for(
                pool.acquire("transient"),
                timeout=0.2,
            )
            await replacement.release()

    async def test_unknown_response_cancellation_quarantines_and_reprobes(
        self,
    ) -> None:
        path = "/pool/a.sock"
        response_waiting = asyncio.Event()
        reprobe_entered = asyncio.Event()
        allow_reprobe = asyncio.Event()
        connection_calls = 0
        digest = "a" * 64

        class PendingReader(_FakeReader):
            async def readline(self) -> bytes:
                response_waiting.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        class PendingWriter:
            def __init__(self) -> None:
                self.closed = False

            def write(self, _encoded: bytes) -> None:
                return None

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

        async def open_connection(socket_path: str, *, limit: int):
            nonlocal connection_calls
            del limit
            self.assertEqual(path, socket_path)
            connection_calls += 1
            return PendingReader(), PendingWriter()

        async def reprobe(socket_path: str) -> str:
            self.assertEqual(path, socket_path)
            reprobe_entered.set()
            await allow_reprobe.wait()
            return digest

        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": path,
                    "EXECUTOR_POOL_SOCKETS": path,
                },
            ),
            patch.object(
                executor_client.asyncio,
                "open_unix_connection",
                side_effect=open_connection,
            ),
        ):
            pool = get_executor_slot_pool(primary_socket=path)
            await pool.apply_startup_attestations({path: digest})
            pool.configure_reprobe_handler(reprobe)
            execution = asyncio.create_task(
                executor_client.execute_isolated_session_code(
                    workspace=self.workspace,
                    code="print('unknown receipt')",
                    socket_path=path,
                )
            )
            await asyncio.wait_for(response_waiting.wait(), timeout=0.2)
            execution.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(execution, timeout=0.2)

            await asyncio.wait_for(reprobe_entered.wait(), timeout=0.2)
            snapshot = await pool.snapshot()
            self.assertEqual(
                "reaping",
                snapshot[0]["state"],
            )
            self.assertEqual(
                "executor_reprobe_in_progress",
                snapshot[0]["quarantine_reason"],
            )
            self.assertEqual(1, connection_calls)

            reprobe_task = next(iter(pool._reprobe_tasks.values()))
            allow_reprobe.set()
            await asyncio.wait_for(
                asyncio.shield(reprobe_task),
                timeout=0.2,
            )
            self.assertEqual(
                "free",
                (await pool.snapshot())[0]["state"],
            )

    async def test_typed_one_shot_containment_failure_quarantines_without_replay(
        self,
    ) -> None:
        paths = ("/pool/a.sock", "/pool/b.sock")
        calls: list[str] = []

        async def open_connection(path: str, *, limit: int):
            del limit
            calls.append(path)
            reader = _FakeReader()
            return reader, _FakeWriter(
                reader,
                "worker_containment_failed",
            )

        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": paths[0],
                    "EXECUTOR_POOL_SOCKETS": ",".join(paths),
                },
            ),
            patch.object(
                executor_client.asyncio,
                "open_unix_connection",
                side_effect=open_connection,
            ),
        ):
            with self.assertRaises(
                executor_client.IsolatedSkillExecutorError
            ) as caught:
                await executor_client.execute_isolated_session_code(
                    workspace=self.workspace,
                    code="print('already ran')",
                    socket_path=paths[0],
                )
            pool = get_executor_slot_pool(primary_socket=paths[0])
            snapshot = await pool.snapshot()

        self.assertEqual(
            "worker_containment_failed",
            caught.exception.code,
        )
        self.assertTrue(caught.exception.dispatch_unknown)
        self.assertEqual([paths[0]], calls)
        self.assertEqual("quarantined", snapshot[0]["state"])
        self.assertEqual("free", snapshot[1]["state"])

    async def test_connect_failure_before_write_can_move_to_next_slot(
        self,
    ) -> None:
        paths = ("/pool/a.sock", "/pool/b.sock")
        calls: list[str] = []

        async def open_connection(path: str, *, limit: int):
            del limit
            calls.append(path)
            if len(calls) == 1:
                raise FileNotFoundError("fixture socket absent")
            reader = _FakeReader()
            return reader, _FakeWriter(reader, "success")

        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": paths[0],
                    "EXECUTOR_POOL_SOCKETS": ",".join(paths),
                },
            ),
            patch.object(
                executor_client.asyncio,
                "open_unix_connection",
                side_effect=open_connection,
            ),
        ):
            result = await executor_client.execute_isolated_session_code(
                workspace=self.workspace,
                code="print('ok')",
                socket_path=paths[0],
            )

        self.assertEqual("success", result["status"])
        self.assertEqual(list(paths), calls)

    async def test_process_lease_operations_remain_affine_to_open_slot(
        self,
    ) -> None:
        paths = ("/pool/a.sock", "/pool/b.sock")
        skill = self.workspace / "skill"
        process_workspace = self.workspace / "workspace"
        skill.mkdir()
        process_workspace.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: pool-fixture\n---\n",
            encoding="utf-8",
        )
        (skill / "run.py").write_text("print('ok')\n", encoding="utf-8")
        scope = executor_client.create_process_owner_scope(
            user_id="pool-user",
            session_id="pool-session",
            root_run_id="pool-run",
        )
        responses = AsyncMock(side_effect=[
            ({
                "status": "success",
                "lease_handle": "pl2_" + "a" * 32 + "_" + "b" * 32,
            }, []),
            ({"status": "success", "state": "running"}, []),
        ])
        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": paths[0],
                    "EXECUTOR_POOL_SOCKETS": ",".join(paths),
                    "EXECUTOR_V2_AUTH_TOKEN": "x" * 64,
                },
            ),
            patch.object(
                executor_client,
                "_exchange_process_request",
                responses,
            ),
        ):
            lease, _ = await executor_client.open_isolated_process_lease(
                owner_scope=scope,
                skill_root=skill,
                workspace=process_workspace,
                entrypoint="run.py",
                socket_path=paths[0],
            )
            await executor_client.start_isolated_process_lease(lease)
            self.assertEqual(paths[0], lease._socket_path)
            self.assertIsNotNone(lease._slot_reservation)
            self.assertEqual(
                [paths[0], paths[0]],
                [
                    call.kwargs["socket_path"]
                    for call in responses.await_args_list
                ],
            )
            await lease._slot_reservation.release()
            lease._slot_reservation = None

    async def test_process_open_forwards_waiting_admission_cancel_event(
        self,
    ) -> None:
        paths = ("/pool/a.sock", "/pool/b.sock")
        skill = self.workspace / "cancel-skill"
        process_workspace = self.workspace / "cancel-workspace"
        skill.mkdir()
        process_workspace.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: pool-cancel-fixture\n---\n",
            encoding="utf-8",
        )
        (skill / "run.py").write_text(
            "print('must not dispatch')\n",
            encoding="utf-8",
        )
        scope = executor_client.create_process_owner_scope(
            user_id="pool-cancel-user",
            session_id="pool-cancel-session",
            root_run_id="pool-cancel-run",
        )
        admission_cancel = asyncio.Event()
        admission_cancel.set()
        exchange = AsyncMock()

        with (
            patch.dict(
                os.environ,
                {
                    "EXECUTOR_SOCKET": paths[0],
                    "EXECUTOR_POOL_SOCKETS": ",".join(paths),
                    "EXECUTOR_V2_AUTH_TOKEN": "x" * 64,
                },
            ),
            patch.object(
                executor_client,
                "_exchange_process_request",
                exchange,
            ),
        ):
            with self.assertRaises(
                executor_client.IsolatedSkillExecutorError
            ) as caught:
                await executor_client.open_isolated_process_lease(
                    owner_scope=scope,
                    skill_root=skill,
                    workspace=process_workspace,
                    entrypoint="run.py",
                    socket_path=paths[0],
                    admission_cancel_event=admission_cancel,
                )

        self.assertEqual(
            "executor_admission_cancelled",
            caught.exception.code,
        )
        exchange.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
